"""FR-113, decision 63 - an ``openrouter`` agent's model traffic goes through a metering proxy.

Every claim is made against real sockets on loopback: the proxy (``acp/metering.py``) binds
``127.0.0.1`` on port 0, and ``tests/support/stub_openrouter.py`` stands in for OpenRouter on
another loopback port, replaying streams OpenRouter actually sent (``tests/fixtures/wire/
openrouter/``). The socket guard is unchanged and admits both (MUST rule 18). The one test that
reaches the real OpenRouter is ``@pytest.mark.live`` and never part of ``verify``.

Evidence for each criterion is written as text to ``evidence/172/`` (gitignored).
"""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import sqlite3
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from harnessed.acp import metering, turns
from harnessed.acp.config import TEMPLATES, AgentSpec
from harnessed.cli import routed
from harnessed.events.bus import EventBus
from harnessed.memory import db
from support import stub_openrouter
from support.stub_openrouter import FIXTURES

REPO_ROOT = Path(__file__).resolve().parents[1]
STUB_CLI = REPO_ROOT / "tests" / "support" / "stub_cli_agent.py"
EVIDENCE = REPO_ROOT / "evidence" / "172"

pytestmark = pytest.mark.usefixtures("no_child_coverage")

#: A seeded key: it must reach the stub upstream and never the child or the journal.
SEEDED_KEY = "sk-" + "or-v1-" + "a" * 48
SESSION = "run_metering_172"
ENTRY = "or-agent"

PONG = (FIXTURES / "pong.sse").read_bytes()
TOOL_CALL = (FIXTURES / "tool-call.sse").read_bytes()
PONG_REQUEST = json.loads((FIXTURES / "pong.request.json").read_text(encoding="utf-8"))
TOOL_REQUEST = json.loads((FIXTURES / "tool-call.request.json").read_text(encoding="utf-8"))


def write_evidence(name: str, text: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"{name}.txt").write_text(text.rstrip() + "\n", encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "harness.db")
    try:
        yield conn
    finally:
        conn.close()


def openrouter_spec(tmp_path: Path, **overrides: object) -> AgentSpec:
    fields: dict[str, object] = {
        "name": ENTRY,
        "command": sys.executable,
        "args": (str(STUB_CLI), "--task", "{task}"),
        "cwd": str(tmp_path),
        "kind": "cli",
        "billing": "openrouter",
        "base_url_var": "OR_AGENT_BASE_URL",
        "key_var": "OR_AGENT_API_KEY",
        "allow_write": True,
    }
    fields.update(overrides)
    return AgentSpec(**fields)  # type: ignore[arg-type]


def request_body(recorded: dict[str, object]) -> dict[str, object]:
    """The recorded request as a child would send it: no tags, the proxy adds those."""
    body = dict(recorded["request_body"])  # type: ignore[call-overload]
    body.pop("session_id", None)
    body.pop("user", None)
    return body


def stream_through(
    port: int, body: dict[str, object], *, stop_after: int | None = None
) -> tuple[int, bytes]:
    """POST ``body`` to the proxy the way a child would; read all of it, or ``stop_after`` bytes."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(
        "POST",
        "/api/v1/chat/completions",
        body=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {metering.PLACEHOLDER_KEY}",
            "Content-Type": "application/json",
        },
    )
    response = conn.getresponse()
    if stop_after is None:
        received = response.read()
    else:
        received = response.read(stop_after)
        # Close the socket outright: the proxy is still writing, and this is the disconnect.
        sock = conn.sock
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    conn.close()
    return response.status, received


def agent_turns(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM agent_turns ORDER BY idx"))
    finally:
        conn.row_factory = None


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


# ---- AC1: a loopback ephemeral port per delegation, and no listener after it ------------------


def test_the_proxy_binds_loopback_on_an_ephemeral_port_and_leaves_no_listener(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    spec = openrouter_spec(tmp_path)
    with stub_openrouter.serve([PONG]) as upstream:
        with metering.proxy(
            spec, key=SEEDED_KEY, upstream=upstream.base_url, session_id=SESSION, store=store
        ) as proxy:
            host, port = proxy.address
            assert host == "127.0.0.1"
            assert port > 0
            assert port != upstream.port
            assert proxy.base_url == f"http://127.0.0.1:{port}/api/v1"
            assert listening(port)
        assert not listening(port)
    write_evidence(
        "listener",
        f"bound {host}:{port} (ephemeral) for the delegation\n"
        f"listening after the context exited: {listening(port)}",
    )


def test_a_routed_openrouter_delegation_gets_the_proxy_and_a_placeholder_never_the_key(
    tmp_path: Path, store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``routed.run_agent`` wraps an ``openrouter`` entry: its child sees the loopback URL and the
    placeholder, never the real key, and the listener is gone once the delegation ends."""
    monkeypatch.setenv("HARNESSED_STUB_DUMP_ENV", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", SEEDED_KEY)
    spec = openrouter_spec(tmp_path)
    bus = EventBus(run_id=SESSION, conn=store)
    with stub_openrouter.serve([PONG]) as upstream:
        result = routed.run_agent(
            spec,
            "say pong",
            "none",
            home=tmp_path,
            bus=bus,
            store=store,
            openrouter_key=SEEDED_KEY,
            openrouter_url=upstream.base_url,
        )
    child = json.loads(result.summary)
    url = child["OR_AGENT_BASE_URL"]
    assert url.startswith("http://127.0.0.1:") and url.endswith("/api/v1")
    assert child["OR_AGENT_API_KEY"] == metering.PLACEHOLDER_KEY
    assert SEEDED_KEY not in result.summary
    port = int(url.removeprefix("http://127.0.0.1:").split("/")[0])
    assert not listening(port)
    write_evidence(
        "child-env",
        f"child OR_AGENT_BASE_URL = {url}\nchild OR_AGENT_API_KEY = {child['OR_AGENT_API_KEY']}\n"
        f"seeded key in the child's environment: {SEEDED_KEY in result.summary}\n"
        f"listening after the delegation: {listening(port)}",
    )


def test_an_oauth_entry_is_never_proxied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST rule 33's other half: a subscription entry gets no base URL at all."""
    monkeypatch.setenv("HARNESSED_STUB_DUMP_ENV", "1")
    spec = openrouter_spec(tmp_path, billing="oauth")
    result = routed.run_agent(
        spec, "say pong", "none", home=tmp_path, openrouter_key=SEEDED_KEY, openrouter_url="x"
    )
    child = json.loads(result.summary)
    assert "OR_AGENT_BASE_URL" not in child
    assert "OR_AGENT_API_KEY" not in child


def test_the_child_variables_are_only_the_ones_the_entry_names(tmp_path: Path) -> None:
    spec = openrouter_spec(tmp_path)
    with (
        stub_openrouter.serve([PONG]) as upstream,
        metering.proxy(spec, key=SEEDED_KEY, upstream=upstream.base_url) as proxy,
    ):
        assert proxy.child_env() == {
            "OR_AGENT_BASE_URL": proxy.base_url,
            "OR_AGENT_API_KEY": metering.PLACEHOLDER_KEY,
        }


# ---- AC2: byte-for-byte replay, the injected key and the two tags -----------------------------


def test_the_client_receives_the_recorded_bytes_and_upstream_gets_the_key_and_tags(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    spec = openrouter_spec(tmp_path)
    with (
        stub_openrouter.serve([PONG]) as upstream,
        metering.proxy(
            spec, key=SEEDED_KEY, upstream=upstream.base_url, session_id=SESSION, store=store
        ) as proxy,
    ):
        status, received = stream_through(proxy.address[1], request_body(PONG_REQUEST))
    assert status == 200
    assert received == PONG
    [sent] = upstream.received
    assert sent.path == "/api/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {SEEDED_KEY}"
    assert metering.PLACEHOLDER_KEY not in json.dumps(sent.headers)
    assert sent.body["session_id"] == SESSION
    assert sent.body["user"] == ENTRY
    assert sent.body["messages"] == PONG_REQUEST["request_body"]["messages"]  # type: ignore[index]
    write_evidence(
        "byte-comparison",
        f"recorded bytes: {len(PONG)}\nreceived bytes: {len(received)}\n"
        f"identical: {received == PONG}",
    )
    write_evidence(
        "upstream-tags",
        f"upstream Authorization carried the injected key: "
        f"{sent.headers['authorization'] == f'Bearer {SEEDED_KEY}'}\n"
        f"session_id = {sent.body['session_id']}\nuser = {sent.body['user']}\n"
        f"child sent Authorization: Bearer {metering.PLACEHOLDER_KEY}",
    )


# ---- AC3: one agent_turns row per turn, before the next is forwarded --------------------------


def test_each_turn_is_journaled_before_the_next_is_forwarded(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    spec = openrouter_spec(tmp_path)
    database = tmp_path / "harness.db"
    seen: list[int] = []

    def count_rows() -> None:
        check = sqlite3.connect(database)
        try:
            seen.append(int(check.execute("SELECT COUNT(*) FROM agent_turns").fetchone()[0]))
        finally:
            check.close()

    with (
        stub_openrouter.serve([PONG, TOOL_CALL], on_request=count_rows) as upstream,
        metering.proxy(
            spec,
            key=SEEDED_KEY,
            upstream=upstream.base_url,
            session_id=SESSION,
            store=store,
            message_seq=3,
            hop=1,
            task_hash="th_172",
        ) as proxy,
    ):
        stream_through(proxy.address[1], request_body(PONG_REQUEST))
        stream_through(proxy.address[1], request_body(TOOL_REQUEST))

    assert seen == [0, 1]
    first, second = agent_turns(store)
    assert first["run_id"] == SESSION and first["idx"] == 0 and second["idx"] == 1
    assert first["source"] == "proxy" and first["billing"] == "openrouter"
    assert first["entry"] == ENTRY and first["message_seq"] == 3 and first["hop"] == 1
    assert first["task_hash"] == "th_172"
    assert json.loads(first["request"])["messages"][0]["content"] == (
        "Reply with the single word pong."
    )
    assert first["response"] == "pong"
    assert first["tool_calls"] is None
    assert first["model"] == "openai/gpt-oss-20b"
    assert (first["input_tokens"], first["output_tokens"]) == (74, 34)
    assert (first["cached_input_tokens"], first["reasoning_tokens"]) == (0, 23)
    assert Decimal(first["cost"]) == Decimal("0.000004392")
    assert first["cost_nanos"] == 4392
    assert first["currency"] == "USD" and first["cost_kind"] == "reported"
    assert first["generation_id"] == "gen-1790478221-HZacAlHSAxlXrzy8ljL0"
    assert first["complete"] == 1

    [call] = json.loads(second["tool_calls"])
    assert call["name"] == "get_weather"
    assert json.loads(call["arguments"]) == {"city": "Paris"}
    assert call["id"].startswith("chatcmpl-tool-")
    assert Decimal(second["cost"]) == Decimal("0.00001038")
    assert second["cost_kind"] == "reported" and second["complete"] == 1
    for row in (first, second):
        assert SEEDED_KEY not in json.dumps(dict(row))
    write_evidence(
        "per-turn-rows",
        f"rows in agent_turns when each upstream request arrived: {seen}\n"
        + "\n".join(
            f"idx {row['idx']}: response={row['response']!r} tool_calls={row['tool_calls']} "
            f"tokens=({row['input_tokens']},{row['output_tokens']}) cost={row['cost']} "
            f"cost_kind={row['cost_kind']} complete={row['complete']}"
            for row in (first, second)
        ),
    )


# ---- AC4: a client disconnect still journals what arrived, incomplete -------------------------


def test_a_client_disconnect_mid_stream_journals_what_arrived_as_incomplete(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    spec = openrouter_spec(tmp_path)
    with (
        stub_openrouter.serve([PONG], delay_s=0.05) as upstream,
        metering.proxy(
            spec,
            key=SEEDED_KEY,
            upstream=upstream.base_url,
            session_id=SESSION,
            store=store,
            wait=no_wait,
        ) as proxy,
    ):
        _, received = stream_through(proxy.address[1], request_body(PONG_REQUEST), stop_after=200)
        assert proxy.wait_idle(timeout=10)
        for thread in proxy.backstops:
            thread.join(10)
    assert len(received) == 200
    [row] = agent_turns(store)
    assert row["complete"] == 0
    assert row["source"] == "proxy"
    assert row["cost"] is None and row["cost_kind"] is None
    assert row["generation_id"] == "gen-1790478221-HZacAlHSAxlXrzy8ljL0"
    write_evidence(
        "disconnect",
        f"client read {len(received)} bytes of {len(PONG)} and closed\n"
        f"journaled: complete={row['complete']} response={row['response']!r} "
        f"cost={row['cost']} cost_kind={row['cost_kind']}",
    )


def test_the_proxy_journals_nothing_without_a_store(tmp_path: Path) -> None:
    spec = openrouter_spec(tmp_path)
    with (
        stub_openrouter.serve([PONG]) as upstream,
        metering.proxy(spec, key=SEEDED_KEY, upstream=upstream.base_url) as proxy,
    ):
        status, received = stream_through(proxy.address[1], request_body(PONG_REQUEST))
    assert status == 200 and received == PONG


def test_the_tee_reads_a_stream_split_at_any_byte() -> None:
    """``read1`` hands back whatever arrived, so a ``data:`` line can straddle two chunks."""
    tee = metering.StreamTee()
    for index in range(0, len(TOOL_CALL), 7):
        tee.feed(TOOL_CALL[index : index + 7])
    turn = tee.turn(request=None, disconnected=False)
    assert turn.complete
    assert turn.tool_calls[0]["name"] == "get_weather"
    assert turn.cost == Decimal("0.00001038")
    assert turn.input_tokens == 122


# ---- ticket 173 (FR-114): the generation lookup is a delayed backstop, never the meter ---------

EVIDENCE_173 = REPO_ROOT / "evidence" / "173"
GENERATION_ID = "gen-1790478221-HZacAlHSAxlXrzy8ljL0"
#: ``pong.sse`` less its final ``usage.cost``: the same turn, as if OpenRouter had not priced it.
PONG_UNPRICED = PONG.replace(b'"cost":0.000004392,', b"")
#: ``GET /api/v1/generation?id=``'s answer once the figure is ready: ``data.total_cost``.
GENERATION = json.dumps(
    {"data": {"id": GENERATION_ID, "total_cost": 0.000004392, "model": "openai/gpt-oss-20b"}}
).encode("utf-8")
NOT_FOUND = (404, b'{"error":{"message":"Generation not found","code":404}}')


def no_wait(seconds: float) -> None:
    """The backstop's pause, skipped: no test sleeps the scheduled 20 s."""


class Waits:
    """An injected ``wait`` that records each pause instead of taking it."""

    def __init__(self) -> None:
        self.paused: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.paused.append(seconds)

    def at(self) -> list[float]:
        """When each try ran, in seconds after the turn was journaled."""
        return [sum(self.paused[: index + 1]) for index in range(len(self.paused))]


def write_evidence_173(name: str, text: str) -> None:
    EVIDENCE_173.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_173 / f"{name}.txt").write_text(text.rstrip() + "\n", encoding="utf-8")


def lookups(upstream: stub_openrouter.StubOpenRouter) -> list[stub_openrouter.Received]:
    return [request for request in upstream.received if request.method == "GET"]


def unpriced_turn(store: sqlite3.Connection) -> None:
    """One journaled proxy turn that came back with no ``usage.cost``."""
    tee = metering.StreamTee()
    tee.feed(PONG_UNPRICED)
    turns.record(
        store,
        SESSION,
        (tee.turn(request=None, disconnected=False),),
        entry=ENTRY,
        billing="openrouter",
        source="proxy",
        message_seq=None,
        hop=0,
        task_hash=None,
    )


def test_the_scheduled_tries_are_at_20_40_and_60_seconds() -> None:
    assert metering.BACKSTOP_AT_S == (20.0, 40.0, 60.0)


def test_a_turn_priced_inline_makes_no_generation_lookup(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    spec = openrouter_spec(tmp_path)
    waits = Waits()
    with (
        stub_openrouter.serve([PONG, TOOL_CALL], generations=[(200, GENERATION)]) as upstream,
        metering.proxy(
            spec,
            key=SEEDED_KEY,
            upstream=upstream.base_url,
            session_id=SESSION,
            store=store,
            wait=waits,
        ) as proxy,
    ):
        stream_through(proxy.address[1], request_body(PONG_REQUEST))
        stream_through(proxy.address[1], request_body(TOOL_REQUEST))
        assert proxy.wait_idle(timeout=10)
        started = list(proxy.backstops)
    rows = agent_turns(store)
    assert [row["cost_kind"] for row in rows] == ["reported", "reported"]
    assert started == []
    assert lookups(upstream) == []
    assert waits.paused == []
    write_evidence_173(
        "priced-inline",
        f"turns journaled with usage.cost: {len(rows)}\n"
        f"backstops started: {len(started)}\ngeneration lookups made: {len(lookups(upstream))}",
    )


def test_an_unpriced_turn_is_looked_up_until_the_figure_and_set_once(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    """Through the proxy: the turn is journaled not metered, then the backstop fills it in."""
    spec = openrouter_spec(tmp_path)
    waits = Waits()
    with (
        stub_openrouter.serve(
            [PONG_UNPRICED], generations=[NOT_FOUND, (200, GENERATION)]
        ) as upstream,
        metering.proxy(
            spec,
            key=SEEDED_KEY,
            upstream=upstream.base_url,
            session_id=SESSION,
            store=store,
            wait=waits,
        ) as proxy,
    ):
        status, received = stream_through(proxy.address[1], request_body(PONG_REQUEST))
        assert proxy.wait_idle(timeout=10)
        [thread] = proxy.backstops
        thread.join(10)
        assert not thread.is_alive()
    assert status == 200 and received == PONG_UNPRICED
    made = lookups(upstream)
    assert len(made) == 2
    for lookup in made:
        assert lookup.path == f"/api/v1/generation?id={GENERATION_ID}"
        assert lookup.headers["authorization"] == f"Bearer {SEEDED_KEY}"
    assert waits.at() == [20.0, 40.0]
    [row] = agent_turns(store)
    assert Decimal(row["cost"]) == Decimal("0.000004392")
    assert row["cost_nanos"] == 4392
    assert row["currency"] == "USD" and row["cost_kind"] == "reported"
    write_evidence_173(
        "lookup-then-figure",
        f"lookups made: {len(made)}, at {waits.at()} s after the turn\n"
        "answers: 404, then 200 with data.total_cost\n"
        f"row: cost={row['cost']} cost_nanos={row['cost_nanos']} cost_kind={row['cost_kind']}",
    )


def test_a_backstop_that_never_answers_leaves_the_turn_not_metered(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    unpriced_turn(store)
    waits = Waits()
    with stub_openrouter.serve([PONG]) as upstream:
        thread = metering.backstop(
            tmp_path / "harness.db",
            run_id=SESSION,
            generation_id=GENERATION_ID,
            key=SEEDED_KEY,
            upstream=upstream.base_url,
            wait=waits,
        )
        thread.join(10)
    assert not thread.is_alive() and thread.daemon
    assert len(lookups(upstream)) == 3
    assert waits.at() == [20.0, 40.0, 60.0]
    [row] = agent_turns(store)
    assert row["cost"] is None and row["cost_nanos"] is None and row["cost_kind"] is None
    zero = store.execute(
        "SELECT COUNT(*) FROM agent_turns WHERE cost = '0' OR cost_nanos = 0"
    ).fetchone()[0]
    assert zero == 0
    write_evidence_173(
        "never-answers",
        f"lookups made: {len(lookups(upstream))} at {waits.at()} s, every one 404\n"
        f"row: cost={row['cost']} cost_nanos={row['cost_nanos']} cost_kind={row['cost_kind']}\n"
        f"rows holding 0: {zero}",
    )


def test_an_unreachable_upstream_is_a_try_that_did_not_answer(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    unpriced_turn(store)
    with stub_openrouter.serve([PONG]) as upstream:
        gone = upstream.base_url
    thread = metering.backstop(
        tmp_path / "harness.db",
        run_id=SESSION,
        generation_id=GENERATION_ID,
        key=SEEDED_KEY,
        upstream=gone,
        wait=no_wait,
    )
    thread.join(10)
    assert not thread.is_alive()
    [row] = agent_turns(store)
    assert row["cost"] is None and row["cost_kind"] is None


def test_the_backstop_never_overwrites_a_cost_set_by_another_path(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    unpriced_turn(store)
    database = tmp_path / "harness.db"

    def priced_elsewhere() -> None:
        other = sqlite3.connect(database)
        try:
            other.execute(
                "UPDATE agent_turns SET cost = '0.000009', cost_nanos = 9000, currency = 'USD', "
                "cost_kind = 'reported'"
            )
            other.commit()
        finally:
            other.close()

    with stub_openrouter.serve(
        [PONG], generations=[(200, GENERATION)], on_lookup=priced_elsewhere
    ) as upstream:
        thread = metering.backstop(
            database,
            run_id=SESSION,
            generation_id=GENERATION_ID,
            key=SEEDED_KEY,
            upstream=upstream.base_url,
            wait=no_wait,
        )
        thread.join(10)
    assert len(lookups(upstream)) == 1
    [row] = agent_turns(store)
    assert row["cost"] == "0.000009" and row["cost_nanos"] == 9000
    write_evidence_173(
        "no-overwrite",
        "cost set by another path before the answer: 0.000009\n"
        f"lookups made: {len(lookups(upstream))}; the backstop answered 0.000004392; "
        f"row now: cost={row['cost']}",
    )


# ---- the live tier (operator-run, never part of verify) ---------------------------------------


@pytest.mark.live
def test_live_openrouter_streams_through_the_real_proxy(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    """Decision 70: one real turn through the proxy to OpenRouter, and each installed
    ``openrouter`` harness named as installed or not - nothing is claimed for one that is not."""
    from harnessed import config
    from harnessed.providers import quirks

    key = config.load().api_key_for("openrouter")
    if not key:
        pytest.skip("OPENROUTER_API_KEY is not configured")
    upstream = quirks.base_url_for("openrouter")
    assert upstream is not None
    spec = openrouter_spec(tmp_path)
    with metering.proxy(spec, key=key, upstream=upstream, session_id=SESSION, store=store) as proxy:
        status, received = stream_through(proxy.address[1], request_body(PONG_REQUEST))
    assert status == 200
    assert received.rstrip().endswith(b"data: [DONE]")
    [row] = agent_turns(store)
    assert row["cost_kind"] == "reported" and row["complete"] == 1
    harnesses = {
        name: shutil.which(template.command) is not None
        for name, template in TEMPLATES.items()
        if template.billing == "openrouter"
    }
    write_evidence(
        "live",
        f"real turn: cost={row['cost']} cost_kind={row['cost_kind']} model={row['model']}\n"
        + "\n".join(
            f"{name}: {'launcher on PATH, not run - nothing claimed' if on else 'not installed'}"
            for name, on in sorted(harnesses.items())
        ),
    )
