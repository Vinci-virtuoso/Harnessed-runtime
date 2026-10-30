"""The metering proxy: one loopback listener per ``openrouter`` delegation (FR-113, decision 63).

An ``openrouter`` harness is a child process that bills the operator's OpenRouter key. It is never
handed that key (MUST rule 33). It gets ``base_url_var`` = ``http://127.0.0.1:<port>/api/v1`` and
``key_var`` = :data:`PLACEHOLDER_KEY`, and this module stands between it and OpenRouter: it swaps
the placeholder for ``Authorization: Bearer <key>``, adds ``session_id`` (the session) and ``user``
(the entry) to the JSON body, and forwards through ``http.client`` to the upstream the caller names
(``quirks.base_url_for("openrouter")`` in production, a loopback stub in tests).

**The response passes byte for byte.** Status and headers less the hop-by-hop ones, then the body
copied with ``read1()`` and flushed per chunk, so a stream reaches the child as it arrives. A
:class:`StreamTee` reads the same bytes on the side and parses the SSE ``data:`` lines for the
content, the tool calls and the final chunk's ``usage``, which is where OpenRouter reports
``usage.cost``.

**One lock serializes forward-then-journal.** A chat completion is forwarded, relayed and written
as one ``agent_turns`` row (``source = "proxy"``, ``cost_kind = "reported"`` when a cost came back,
null and never ``0`` when none did) before the next request is forwarded. A client that goes away
mid-stream still leaves a row: what arrived, with ``complete = 0``.

**The generation lookup is a backstop, never the meter** (FR-114). Only a turn whose final chunk
carried no ``usage.cost`` starts :func:`backstop`: a daemon thread with its own connection that
asks ``GET /api/v1/generation?id=`` at :data:`BACKSTOP_AT_S` and sets the cost only where it is
still null. A lookup that never answers, or a process that exits first, leaves the turn not
metered; nothing here ever writes ``0`` for a figure it did not get.

**Threads, never async.** A stdlib ``ThreadingHTTPServer`` on a daemon thread, bound to
``127.0.0.1`` port 0; exiting :func:`proxy` closes the listener. SQLite connections do not cross
threads, so the journal opens its own connection to the store's file for each turn.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlsplit

from harnessed import clock
from harnessed.acp import turns
from harnessed.acp.config import AgentSpec
from harnessed.errors import StorageError
from harnessed.memory import db

__all__ = ["BACKSTOP_AT_S", "PLACEHOLDER_KEY", "MeteringProxy", "StreamTee", "backstop", "proxy"]

#: What a child's ``key_var`` holds. Never a credential; the proxy replaces it on every request.
PLACEHOLDER_KEY: Final = "harnessed-metering-proxy"

#: The path prefix a child's base URL carries, and OpenRouter's own (``/api/v1``).
_PREFIX: Final = "/api/v1"
#: The one request shape that is a model turn and so is journaled.
_TURN_SUFFIX: Final = "/chat/completions"

#: Headers that describe one connection, not the message (RFC 9110 section 7.6.1), plus the ones
#: the proxy sets itself.
_HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
_REPLACED: Final = frozenset({"host", "authorization", "content-length", "accept-encoding"})

_CHUNK: Final = 1 << 16
_UPSTREAM_TIMEOUT_S: Final = 300.0
#: How long leaving the context waits for a turn still in flight to be journaled.
_DRAIN_S: Final = 5.0
_DONE: Final = b"[DONE]"
_CURRENCY: Final = "USD"
#: When the generation lookup tries, in seconds after the turn was journaled (decision 63). The
#: spike saw a 404 at 1.5 s and the figure at 20 s.
BACKSTOP_AT_S: Final = (20.0, 40.0, 60.0)
_GENERATION: Final = "/generation?id="
_LOOKUP_TIMEOUT_S: Final = 30.0
_REPORTED: Final = "reported"


# ---- the tee: what a turn said, read from the bytes on their way past ---------------------------


@dataclass
class _Call:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class StreamTee:
    """Parses a chat completion - an SSE stream or one JSON body - from chunks cut anywhere."""

    _pending: bytes = b""
    _raw: bytearray = field(default_factory=bytearray)
    _sse: bool = False
    _done: bool = False
    _text: list[str] = field(default_factory=list)
    _calls: dict[int, _Call] = field(default_factory=dict)
    _usage: Mapping[str, Any] | None = None
    _model: str | None = None
    _generation_id: str | None = None

    def feed(self, chunk: bytes) -> None:
        """Take the next bytes exactly as they arrived; every complete line is read at once."""
        self._raw.extend(chunk)
        *lines, self._pending = (self._pending + chunk).split(b"\n")
        for line in lines:
            self._line(line.rstrip(b"\r"))

    def turn(self, *, request: str | None, disconnected: bool) -> turns.AgentTurn:
        """The turn as it stands. Complete only when the stream ended properly and was all sent."""
        if not self._sse:
            self._whole_body()
        cost = _cost(self._usage)
        usage = self._usage or {}
        return turns.AgentTurn(
            request=request,
            response="".join(self._text),
            tool_calls=tuple(
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for _, call in sorted(self._calls.items())
            ),
            model=self._model,
            cost=cost,
            currency=None if cost is None else _CURRENCY,
            cost_kind=None if cost is None else _REPORTED,
            complete=self._done and not disconnected,
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
            cached_input_tokens=_int(_nested(usage, "prompt_tokens_details", "cached_tokens")),
            reasoning_tokens=_int(_nested(usage, "completion_tokens_details", "reasoning_tokens")),
            generation_id=self._generation_id,
        )

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        self._sse = True
        payload = line[len(b"data:") :].strip()
        if payload == _DONE:
            self._done = True
            return
        chunk = _json(payload)
        if chunk is not None:
            self._chunk(chunk, delta_key="delta")

    def _whole_body(self) -> None:
        """A non-streamed completion: one JSON object whose choices carry ``message``."""
        body = _json(bytes(self._raw))
        if body is None:
            return
        self._chunk(body, delta_key="message")
        self._done = True

    def _chunk(self, chunk: Mapping[str, Any], *, delta_key: str) -> None:
        if isinstance(chunk.get("id"), str) and self._generation_id is None:
            self._generation_id = chunk["id"]
        if isinstance(chunk.get("model"), str):
            self._model = chunk["model"]
        if isinstance(chunk.get("usage"), Mapping):
            self._usage = chunk["usage"]
        for choice in chunk.get("choices") or ():
            delta = choice.get(delta_key) if isinstance(choice, Mapping) else None
            if not isinstance(delta, Mapping):
                continue
            if isinstance(delta.get("content"), str):
                self._text.append(delta["content"])
            for position, piece in enumerate(delta.get("tool_calls") or ()):
                if isinstance(piece, Mapping):
                    self._call(position, piece)

    def _call(self, position: int, piece: Mapping[str, Any]) -> None:
        index = piece.get("index")
        call = self._calls.setdefault(index if isinstance(index, int) else position, _Call())
        if isinstance(piece.get("id"), str) and piece["id"]:
            call.id = piece["id"]
        function = piece.get("function")
        if isinstance(function, Mapping):
            if isinstance(function.get("name"), str) and function["name"]:
                call.name = function["name"]
            if isinstance(function.get("arguments"), str):
                call.arguments += function["arguments"]


def _json(raw: bytes) -> Mapping[str, Any] | None:
    """``raw`` as a JSON object, numbers kept exact (a float would round a reported cost)."""
    try:
        value = json.loads(raw, parse_float=Decimal)
    except ValueError:
        return None
    return value if isinstance(value, Mapping) else None


def _nested(usage: Mapping[str, Any], outer: str, inner: str) -> object:
    details = usage.get(outer)
    return details.get(inner) if isinstance(details, Mapping) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cost(usage: Mapping[str, Any] | None) -> Decimal | None:
    """The final chunk's ``usage.cost`` exactly as reported, or ``None`` - never a zero."""
    if usage is None:
        return None
    raw = usage.get("cost")
    if isinstance(raw, bool) or not isinstance(raw, int | Decimal | str):
        return None
    try:
        cost = Decimal(str(raw))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() else None


# ---- the proxy ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Journal:
    """Where a proxied turn is written: the store's file, and what its row carries."""

    database: Path
    run_id: str
    message_seq: int | None
    hop: int
    task_hash: str | None


class MeteringProxy:
    """A running proxy: its loopback address, and what a child must be handed to use it."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        key: str,
        upstream: str,
        session_id: str | None,
        journal: _Journal | None,
        wait: Callable[[float], None] = clock.sleep,
    ) -> None:
        self._spec = spec
        self._upstream = upstream
        self._wait = wait
        #: Every generation lookup this proxy started, one per turn that came back unpriced.
        self.backstops: list[threading.Thread] = []
        self._key = key
        parts = urlsplit(upstream)
        self._scheme = parts.scheme
        self._netloc = parts.netloc
        self._base_path = parts.path.rstrip("/")
        self._session_id = session_id
        self._journal = journal
        self._lock = threading.Lock()
        self._idle = threading.Condition()
        self._in_flight = 0
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.meter = self
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="harnessed-metering-proxy", daemon=True
        )

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def base_url(self) -> str:
        """What the child's ``base_url_var`` holds."""
        return f"http://127.0.0.1:{self.address[1]}{_PREFIX}"

    def child_env(self) -> dict[str, str]:
        """The entry's two variables: this proxy's URL and a placeholder, never the key."""
        env: dict[str, str] = {}
        if self._spec.base_url_var:
            env[self._spec.base_url_var] = self.base_url
        if self._spec.key_var:
            env[self._spec.key_var] = PLACEHOLDER_KEY
        return env

    def wait_idle(self, timeout: float) -> bool:
        """Whether every request in flight has been relayed and journaled within ``timeout``."""
        with self._idle:
            return self._idle.wait_for(lambda: self._in_flight == 0, timeout)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        """Let a turn in flight finish its journal, then stop serving and close the listener."""
        self.wait_idle(_DRAIN_S)
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(_DRAIN_S)

    # ---- one request ----------------------------------------------------------------------------

    def forward(self, handler: _Handler, method: str) -> None:
        with self._idle:
            self._in_flight += 1
        try:
            self._forward(handler, method)
        finally:
            with self._idle:
                self._in_flight -= 1
                self._idle.notify_all()

    def _forward(self, handler: _Handler, method: str) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = self._tagged(raw)
        is_turn = method == "POST" and handler.path.split("?")[0].endswith(_TURN_SUFFIX)
        with self._lock:
            upstream = self._connection()
            try:
                upstream.request(
                    method, self._target(handler.path), body=body, headers=self._headers(handler)
                )
                response = upstream.getresponse()
            except OSError as exc:
                upstream.close()
                self._refuse(handler, exc)
                return
            try:
                tee = StreamTee()
                disconnected = _relay(handler, response, tee)
            finally:
                upstream.close()
            if is_turn and 200 <= response.status < 300:
                request = body.decode("utf-8", "replace")
                self._record(tee.turn(request=request, disconnected=disconnected))

    def _tagged(self, raw: bytes) -> bytes:
        """The child's JSON body with ``session_id`` and ``user`` set; anything else as it came."""
        try:
            parsed = json.loads(raw, parse_float=Decimal) if raw else None
        except ValueError:
            return raw
        if not isinstance(parsed, dict):
            return raw
        if self._session_id is not None:
            parsed["session_id"] = self._session_id
        parsed["user"] = self._spec.name
        return json.dumps(parsed, ensure_ascii=False, default=str).encode("utf-8")

    def _target(self, path: str) -> str:
        rest = path[len(_PREFIX) :] if path.startswith(_PREFIX) else path
        return f"{self._base_path}{rest}"

    def _headers(self, handler: _Handler) -> dict[str, str]:
        headers = {
            name: value
            for name, value in handler.headers.items()
            if name.lower() not in _HOP_BY_HOP and name.lower() not in _REPLACED
        }
        headers["Authorization"] = f"Bearer {self._key}"
        headers["Accept-Encoding"] = "identity"
        return headers

    def _connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._netloc, timeout=_UPSTREAM_TIMEOUT_S)
        return http.client.HTTPConnection(self._netloc, timeout=_UPSTREAM_TIMEOUT_S)

    def _refuse(self, handler: _Handler, exc: OSError) -> None:
        """The upstream could not be reached: a 502 naming the class, never the key or the URL."""
        body = json.dumps(
            {"error": {"message": f"the metering proxy could not reach OpenRouter ({_name(exc)})"}}
        ).encode("utf-8")
        with contextlib.suppress(OSError):
            handler.send_response_only(502)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

    def _record(self, turn: turns.AgentTurn) -> None:
        journal = self._journal
        if journal is None:
            return
        with contextlib.closing(db.connect(journal.database)) as conn:
            turns.record(
                conn,
                journal.run_id,
                (turn,),
                entry=self._spec.name,
                billing=self._spec.billing,
                source="proxy",
                message_seq=journal.message_seq,
                hop=journal.hop,
                task_hash=journal.task_hash,
            )
        if turn.cost is None and turn.generation_id:
            self.backstops.append(
                backstop(
                    journal.database,
                    run_id=journal.run_id,
                    generation_id=turn.generation_id,
                    key=self._key,
                    upstream=self._upstream,
                    wait=self._wait,
                )
            )


def backstop(
    database: Path,
    *,
    run_id: str,
    generation_id: str,
    key: str,
    upstream: str,
    wait: Callable[[float], None] = clock.sleep,
    at: tuple[float, ...] = BACKSTOP_AT_S,
) -> threading.Thread:
    """Start the generation lookup for one unpriced turn, on a daemon thread, and return it.

    Each try waits until its time in ``at``, asks OpenRouter for the generation, and stops at the
    first figure, which is written only where the turn's cost is still null. A try that fails in
    any way - a 404, an unreachable upstream, a body without a figure - is a try that did not
    answer, and after the last one the turn stays not metered.
    """
    thread = threading.Thread(
        target=_look_up,
        args=(database, run_id, generation_id, key, upstream, wait, at),
        name="harnessed-generation-backstop",
        daemon=True,
    )
    thread.start()
    return thread


def _look_up(
    database: Path,
    run_id: str,
    generation_id: str,
    key: str,
    upstream: str,
    wait: Callable[[float], None],
    at: tuple[float, ...],
) -> None:
    waited = 0.0
    for when in at:
        wait(when - waited)
        waited = when
        cost = _generation_cost(upstream, generation_id, key)
        if cost is None:
            continue
        with contextlib.closing(db.connect(database)) as conn:
            turns.settle_cost(conn, run_id, generation_id, cost, _CURRENCY)
        return


def _generation_cost(upstream: str, generation_id: str, key: str) -> Decimal | None:
    """The generation's ``data.total_cost``, or ``None`` for any answer that is not a figure."""
    parts = urlsplit(upstream)
    connection: http.client.HTTPConnection = (
        http.client.HTTPSConnection(parts.netloc, timeout=_LOOKUP_TIMEOUT_S)
        if parts.scheme == "https"
        else http.client.HTTPConnection(parts.netloc, timeout=_LOOKUP_TIMEOUT_S)
    )
    path = f"{parts.path.rstrip('/')}{_GENERATION}{quote(generation_id, safe='')}"
    try:
        with contextlib.closing(connection):
            connection.request("GET", path, headers={"Authorization": f"Bearer {key}"})
            response = connection.getresponse()
            body = response.read()
    except OSError:
        return None
    if response.status != 200:
        return None
    parsed = _json(body)
    data = parsed.get("data") if parsed is not None else None
    if not isinstance(data, Mapping):
        return None
    return _cost({"cost": data.get("total_cost")})


def _relay(handler: _Handler, response: http.client.HTTPResponse, tee: StreamTee) -> bool:
    """Copy the response to the child as it arrives; ``True`` when the child went away."""
    try:
        handler.send_response_only(response.status, response.reason)
        for name, value in response.getheaders():
            if name.lower() not in _HOP_BY_HOP:
                handler.send_header(name, value)
        handler.send_header("Connection", "close")
        handler.end_headers()
    except OSError:
        return True
    while chunk := response.read1(_CHUNK):
        tee.feed(chunk)
        try:
            handler.wfile.write(chunk)
            handler.wfile.flush()
        except OSError:
            return True
    return False


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    meter: MeteringProxy


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_POST(self) -> None:
        self.server.meter.forward(self, "POST")

    def do_GET(self) -> None:
        self.server.meter.forward(self, "GET")

    def log_message(self, format: str, *args: object) -> None:
        """Silent: a request line is the child's business, and stderr is the operator's."""


def _name(exc: OSError) -> str:
    return type(exc).__name__


def _database_of(store: sqlite3.Connection) -> Path:
    """The file behind ``store``; the proxy's own threads open their own connection to it."""
    for _, name, file in store.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    raise StorageError("the metering proxy journals to a database file, and this store has none")


@contextlib.contextmanager
def proxy(
    spec: AgentSpec,
    *,
    key: str,
    upstream: str,
    session_id: str | None = None,
    store: sqlite3.Connection | None = None,
    message_seq: int | None = None,
    hop: int = 0,
    task_hash: str | None = None,
    wait: Callable[[float], None] = clock.sleep,
) -> Iterator[MeteringProxy]:
    """Run a metering proxy for one ``openrouter`` delegation, and close it when the block ends.

    Turns are journaled under ``session_id`` when both it and ``store`` are given, and not at all
    otherwise. ``wait`` is how a turn's generation lookup pauses; a test injects one.
    """
    journal = (
        None
        if store is None or session_id is None
        else _Journal(_database_of(store), session_id, message_seq, hop, task_hash)
    )
    running = MeteringProxy(
        spec, key=key, upstream=upstream, session_id=session_id, journal=journal, wait=wait
    )
    running.start()
    try:
        yield running
    finally:
        running.close()
