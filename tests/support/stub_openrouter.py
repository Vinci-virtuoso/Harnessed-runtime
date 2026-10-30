"""A loopback stand-in for OpenRouter that replays recorded streams byte for byte (FR-113).

The metering proxy's tests never leave the machine (MUST rule 18): this server binds
``127.0.0.1`` on port 0, which the socket guard admits as loopback, and answers each request with
the next recorded ``.sse`` body from ``tests/fixtures/wire/openrouter/``. It keeps what every
request carried - path, headers and the parsed JSON body - so a test can assert what the proxy
sent upstream, and it can call ``on_request`` as each request arrives, before answering, which is
how a test observes what was journaled before the next turn was forwarded.

The body goes out one SSE event at a time, flushed, with ``delay_s`` between events, so a client
that disconnects mid-stream is a real mid-stream disconnect rather than a race against one write.

A ``GET`` is a generation lookup (FR-114's backstop): it is kept like any request and answered
with the next of ``generations`` (status, body), the last repeated; with none, every lookup is a
404, which is what OpenRouter says before a generation's figure is ready.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

__all__ = ["FIXTURES", "Received", "StubOpenRouter", "events_of", "serve"]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wire" / "openrouter"


@dataclass(frozen=True, slots=True)
class Received:
    """One request as the stub saw it."""

    method: str
    path: str
    headers: dict[str, str]
    body: Any


def events_of(recorded: bytes) -> list[bytes]:
    """``recorded`` cut after each blank line, so the pieces join back to exactly the input."""
    pieces = recorded.split(b"\n\n")
    events = [piece + b"\n\n" for piece in pieces[:-1]]
    if pieces[-1]:
        events.append(pieces[-1])
    return events


@dataclass
class StubOpenRouter:
    """The running stub: its base URL, and every request it has received."""

    port: int
    received: list[Received] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"


@contextmanager
def serve(
    streams: Sequence[bytes],
    *,
    delay_s: float = 0.0,
    on_request: Callable[[], None] | None = None,
    generations: Sequence[tuple[int, bytes]] = (),
    on_lookup: Callable[[], None] | None = None,
) -> Iterator[StubOpenRouter]:
    """Serve ``streams`` in order, one per request; the last is repeated once they run out.

    ``on_lookup`` runs as each generation lookup arrives, before it is answered.
    """
    lock = threading.Lock()
    served = [0]
    looked_up = [0]
    stub = StubOpenRouter(port=0)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                body: Any = json.loads(raw)
            except ValueError:
                body = raw.decode("utf-8", errors="replace")
            with lock:
                stub.received.append(
                    Received(
                        "POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body
                    )
                )
                stream = streams[min(served[0], len(streams) - 1)]
                served[0] += 1
            if on_request is not None:
                on_request()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                for event in events_of(stream):
                    self.wfile.write(event)
                    self.wfile.flush()
                    if delay_s:
                        time.sleep(delay_s)
            except OSError:
                return

        def do_GET(self) -> None:
            with lock:
                stub.received.append(
                    Received(
                        "GET", self.path, {k.lower(): v for k, v in self.headers.items()}, None
                    )
                )
                status, body = (
                    generations[min(looked_up[0], len(generations) - 1)]
                    if generations
                    else (404, b'{"error":{"message":"Generation not found","code":404}}')
                )
                looked_up[0] += 1
            if on_lookup is not None:
                on_lookup()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(OSError):
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    stub.port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
