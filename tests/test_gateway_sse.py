"""One session streams, drops, and resumes without losing a frame (FR-39, ticket 069).

A client subscribes to one **session id**'s events over SSE, drops, reconnects at ``seq`` N and
receives every later event exactly once and in order. The mechanism is fixed by three decisions
and this file asserts each of them rather than the prose: the gateway tails the ``events`` table
over WAL and never runs the loop (decision 15), new rows are detected by ``PRAGMA data_version``
at ``POLL_INTERVAL_S`` (decision 16), and the stream closes on a terminal name or on
``IDLE_CEILING_S`` (decision 17). The frame is hand-rolled, four lines, no ``sse-starlette``
(decision 20).

Two harness facts shape every streaming test here. Starlette's ``TestClient`` runs the app to
completion before it returns the body, so a stream on a session with no terminal event either
runs against a lowered ``IDLE_CEILING_S`` or is closed by a terminal event published from a
background thread. And a second ``EventBus`` for the same session id restarts ``seq`` at 1, so the
thread that publishes live must own the session's whole sequence.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import sqlite3
import threading
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harnessed import ids
from harnessed.errors import CursorError, SurfaceNotFound
from harnessed.evals.suite import load_named_suite
from harnessed.events import names as names_module
from harnessed.events import read
from harnessed.events.bus import EventBus
from harnessed.gateway import create_app, sse
from harnessed.loop.guardrails import NO_MORE_TOOL_CALLS
from harnessed.memory import db
from harnessed.serialize import canonical
from test_events_read import seed_eval_session
from test_improve_run import cycle

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
SSE_PATH = PACKAGE_ROOT / "gateway" / "sse.py"
ROUTE_PATH = PACKAGE_ROOT / "gateway" / "routes" / "events.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "069"

STREAM_ROUTE = "/v1/sessions/{session_id}/stream"
COLLECTION_ROUTE = "/v1/sessions/{session_id}/events"

#: The SQL verbs, plus the pragma, that ``gateway/sse.py`` may not spell (MUST rule 14).
SQL_SPELLINGS = ("SELECT", "INSERT", "UPDATE", "DELETE", "PRAGMA")

#: A ceiling short enough to wait out in a test and long enough to see the tail tick a few times.
SHORT_CEILING_S = 0.3

#: The delay before the background writer publishes its live half, comfortably over one tick.
LIVE_DELAY_S = 0.3

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


def capture_081(name: str, text: str) -> Path:
    """Ticket 081's evidence, beside 069's rather than inside it."""
    directory = REPO_ROOT / "evidence" / "081"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- benches -------------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


def client_over(home: Path) -> TestClient:
    return TestClient(create_app(home / db.DATABASE_FILENAME), raise_server_exceptions=False)


@dataclasses.dataclass(frozen=True)
class Frame:
    """One parsed SSE frame: the three fields decision 20 spells."""

    id: int
    event: str
    data: str


def parse_frames(text: str) -> list[Frame]:
    """Every frame in ``text``, each four lines: ``id:``, ``event:``, ``data:``, blank."""
    frames: list[Frame] = []
    for block in text.split("\n\n"):
        if not block:
            continue
        lines = block.split("\n")
        assert len(lines) == 3, f"a frame is three lines and a blank, got {block!r}"
        fields = dict(line.split(": ", 1) for line in lines)
        assert list(fields) == ["id", "event", "data"], block
        frames.append(Frame(int(fields["id"]), fields["event"], fields["data"]))
    return frames


def stream(client: TestClient, session_id: str, last_event_id: str | None = None) -> Any:
    headers = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    return client.get(STREAM_ROUTE.format(session_id=session_id), headers=headers)


def frames_of(client: TestClient, session_id: str, last_event_id: str | None = None) -> list[Frame]:
    response = stream(client, session_id, last_event_id)
    assert response.status_code == 200, response.text
    return parse_frames(response.text)


def publish_finished_session(conn: sqlite3.Connection, iterations: int = 2) -> EventBus:
    """A session that ran ``iterations`` loop iterations and finished: ``2 * n + 2`` events."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake", "prompt": "héllo wörld"})
    for iteration in range(1, iterations + 1):
        bus.publish("loop.iteration.started", {"iteration": iteration})
        bus.publish(
            "loop.iteration.finished",
            {"iteration": iteration, "tool_calls": 0, "nested": {"z": 1, "a": [1, 2]}},
        )
    bus.publish("run.finished", {"iterations": iterations, "guardrail": NO_MORE_TOOL_CALLS})
    return bus


def assert_strictly_ascending_without_duplicates(seqs: list[int]) -> None:
    assert all(b > a for a, b in pairwise(seqs)), seqs
    assert len(seqs) - len(set(seqs)) == 0, seqs


# ---- AC 1: the response headers ------------------------------------------------------------------


def test_the_stream_answers_200_as_an_unbuffered_uncached_event_stream(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    bus = publish_finished_session(conn)
    with client_over(tmp_harnessed_home) as client:
        response = stream(client, bus.run_id)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


# ---- AC 2: the frame is id, event, data, blank -------------------------------------------------


def test_each_frame_carries_the_seq_the_name_verbatim_and_the_canonical_payload_on_one_line(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    bus = publish_finished_session(conn)
    stored = read.replay(conn, bus.run_id)
    with client_over(tmp_harnessed_home) as client:
        response = stream(client, bus.run_id)
    frames = parse_frames(response.text)
    assert len(frames) == len(stored) == 6
    for frame, event in zip(frames, stored, strict=True):
        assert frame.id == event.seq
        assert frame.event == event.name
        assert frame.event in names_module.NAMES
        assert frame.data == canonical(dict(event.payload))
        assert "\n" not in frame.data
    # The bytes are exactly the four lines, in that order, and nothing else.
    expected = b"".join(
        f"id: {e.seq}\nevent: {e.name}\ndata: {canonical(dict(e.payload))}\n\n".encode()
        for e in stored
    )
    assert response.content == expected
    assert b"".join(sse.frame(e) for e in stored) == expected
    assert "wörld" in response.text, "canonical() does not escape non-ASCII"


def test_frame_is_a_pure_function_of_the_replayed_event() -> None:
    event = read.ReplayedEvent("run_x", 7, "run.started", "2026-01-01T12:00:00Z", {"b": 1, "a": 2})
    assert sse.frame(event) == b'id: 7\nevent: run.started\ndata: {"a":2,"b":1}\n\n'


# ---- AC 3: reconnect at N -> first seq N + 1, total - N frames, ascending, 0 duplicates ---------


def test_reconnecting_at_a_cursor_yields_exactly_the_later_frames_once_and_in_order(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    bus = publish_finished_session(conn, iterations=3)
    total = len(read.replay(conn, bus.run_id))
    assert total == 8
    with client_over(tmp_harnessed_home) as client:
        before = frames_of(client, bus.run_id)
        cursor = 3
        after = frames_of(client, bus.run_id, str(cursor))
    assert [f.id for f in before] == list(range(1, total + 1))
    seqs = [f.id for f in after]
    assert seqs[0] == cursor + 1
    assert len(after) == total - cursor
    assert_strictly_ascending_without_duplicates(seqs)
    assert after == before[cursor:], "the resumed frames are the same frames, not re-rendered"
    assert after[-1].event == read.terminal_for(conn, bus.run_id)
    lines = [
        f"session id: {bus.run_id}",
        f"total events recorded: {total}",
        "",
        f"first connection (no Last-Event-ID): {len(before)} frames, seq "
        f"{before[0].id}..{before[-1].id}",
        f"disconnect, then reconnect with Last-Event-ID: {cursor}",
        f"second connection: {len(after)} frames, first seq {seqs[0]}, last seq {seqs[-1]}",
        f"strictly ascending: {seqs == sorted(set(seqs))}, "
        f"duplicates: {len(seqs) - len(set(seqs))}",
        "",
        "frames after the cursor:",
    ]
    lines += [f"  id: {f.id:<3} event: {f.event:<26} data: {f.data}" for f in after]
    capture("resume-from-cursor.txt", "\n".join(lines) + "\n")


@pytest.mark.parametrize("cursor", [0, 1, 5, 7])
def test_every_cursor_resumes_at_the_next_seq_with_the_rest_of_the_sequence(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, cursor: int
) -> None:
    bus = publish_finished_session(conn, iterations=3)
    with client_over(tmp_harnessed_home) as client:
        frames = frames_of(client, bus.run_id, str(cursor))
    assert [f.id for f in frames] == list(range(cursor + 1, 9))


# ---- AC 4: a cursor above the highest seq -> zero frames, clean close -------------------------


@pytest.mark.parametrize("beyond", [0, 1, 1000])
def test_a_cursor_at_or_above_the_highest_seq_yields_zero_frames_and_closes_cleanly(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, beyond: int
) -> None:
    bus = publish_finished_session(conn)
    highest = read.replay(conn, bus.run_id)[-1].seq
    with client_over(tmp_harnessed_home) as client:
        response = stream(client, bus.run_id, str(highest + beyond))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.content == b""
    assert parse_frames(response.text) == []


def test_a_cursor_past_the_head_of_a_live_session_closes_at_once_rather_than_waiting(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """Decision 20 on a session that has not ended: the generator itself finishes, no ceiling.

    Driven through the response's own body iterator under a hard timeout, because through the
    ``TestClient`` a generator that waited would sit on the default 900 s ceiling.
    """
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    bus.publish("loop.iteration.started", {"iteration": 1})
    assert sse.IDLE_CEILING_S == 900
    reader = db.connect_readonly(tmp_harnessed_home / db.DATABASE_FILENAME)
    try:
        response = sse.stream(reader, bus.run_id, "7")

        async def drain() -> list[bytes]:
            return [chunk async for chunk in response.body_iterator]  # type: ignore[misc]

        chunks = asyncio.run(asyncio.wait_for(drain(), timeout=5.0))
    finally:
        reader.close()
    assert chunks == []
    assert response.headers["content-type"] == "text/event-stream"


def test_resuming_exactly_at_the_head_of_a_live_session_keeps_following(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconnect UJ-4 describes: the client saw everything so far and the session is live."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    bus.publish("loop.iteration.started", {"iteration": 1})
    monkeypatch.setattr(sse, "IDLE_CEILING_S", SHORT_CEILING_S)
    reader = db.connect_readonly(tmp_harnessed_home / db.DATABASE_FILENAME)
    try:
        response = sse.stream(reader, bus.run_id, "2")
        ticks = 0

        async def drain() -> list[bytes]:
            nonlocal ticks
            out: list[bytes] = []
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                out.append(chunk)
            return out

        original_sleep = asyncio.sleep

        async def counting_sleep(seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            await original_sleep(seconds)

        monkeypatch.setattr(sse.asyncio, "sleep", counting_sleep)
        chunks = asyncio.run(asyncio.wait_for(drain(), timeout=5.0))
    finally:
        reader.close()
    assert chunks == []
    assert ticks >= 2, "the generator followed the live head through the tick until the ceiling"


# ---- AC 5: a malformed Last-Event-ID is CursorError and 400, never 0 -----------------------------


@pytest.mark.parametrize("raw", ["abc", "-1", "", " 3", "3 ", "+3", "1_0", "3.0", "0x10"])
def test_a_malformed_cursor_is_400_in_the_decision_24_body_and_never_treated_as_zero(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, raw: str
) -> None:
    bus = publish_finished_session(conn)
    with client_over(tmp_harnessed_home) as client:
        response = stream(client, bus.run_id, raw)
    assert response.status_code == 400, response.text
    body = response.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == CursorError.__name__
    assert "Last-Event-ID" in body["message"]
    with pytest.raises(CursorError):
        sse.parse_cursor(raw)


@pytest.mark.parametrize(
    ("raw", "cursor"), [(None, 0), ("0", 0), ("1", 1), ("0012", 12), ("9" * 30, int("9" * 30))]
)
def test_a_well_formed_cursor_is_the_non_negative_integer_it_spells(
    raw: str | None, cursor: int
) -> None:
    assert sse.parse_cursor(raw) == cursor


def test_a_non_ascii_digit_is_not_a_cursor_even_though_python_calls_it_one() -> None:
    """``str.isdigit`` accepts Arabic-Indic and fullwidth digits; the parser does not."""
    for raw in ("\u0661", "\uff12", "\u0663\u0664"):  # Arabic-Indic one, fullwidth two, 34
        assert raw.isdigit()
        with pytest.raises(CursorError):
            sse.parse_cursor(raw)


# ---- AC 6: events published while nobody was listening are delivered on reconnect ---------------


def test_events_published_while_the_client_was_away_arrive_on_reconnect(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attach, detach, publish while detached, reconnect from the cursor: nothing is dropped."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    bus.publish("loop.iteration.started", {"iteration": 1})
    # The session has no terminal event yet, so the first attachment closes on the ceiling.
    monkeypatch.setattr(sse, "IDLE_CEILING_S", SHORT_CEILING_S)
    with client_over(tmp_harnessed_home) as client:
        attached = frames_of(client, bus.run_id)
    assert [f.id for f in attached] == [1, 2]
    # Detached: nobody is listening while these three land.
    bus.publish("loop.iteration.finished", {"iteration": 1, "tool_calls": 0})
    bus.publish("loop.iteration.started", {"iteration": 2})
    bus.publish("run.finished", {"iterations": 2, "guardrail": NO_MORE_TOOL_CALLS})
    with client_over(tmp_harnessed_home) as client:
        resumed = frames_of(client, bus.run_id, str(attached[-1].id))
    assert [f.id for f in resumed] == [3, 4, 5]
    assert [f.event for f in resumed] == [
        "loop.iteration.finished",
        "loop.iteration.started",
        "run.finished",
    ]
    assert_strictly_ascending_without_duplicates([f.id for f in attached + resumed])


def test_events_published_while_the_client_is_attached_arrive_through_the_tick(
    tmp_harnessed_home: Path,
) -> None:
    """Decision 16 positively: a writer on another connection commits, the tail sees it.

    The writer is a background thread with its own connection, because the ``TestClient`` runs
    the app to completion before returning and a bus restarts ``seq`` per instance, so the one
    bus that owns the session lives in the thread and publishes both halves.
    """
    ready = threading.Event()
    session_id: list[str] = []

    def writer() -> None:
        writer_conn = db.connect()
        try:
            bus = EventBus(conn=writer_conn)
            session_id.append(bus.run_id)
            bus.publish("run.started", {"provider": "fake"})
            bus.publish("loop.iteration.started", {"iteration": 1})
            ready.set()
            threading.Event().wait(LIVE_DELAY_S)
            bus.publish("loop.iteration.finished", {"iteration": 1, "tool_calls": 0})
            bus.publish("run.finished", {"iterations": 1, "guardrail": NO_MORE_TOOL_CALLS})
        finally:
            writer_conn.close()

    thread = threading.Thread(target=writer, name="cli-writer")
    thread.start()
    try:
        assert ready.wait(5.0)
        with client_over(tmp_harnessed_home) as client:
            frames = frames_of(client, session_id[0])
    finally:
        thread.join(5.0)
    assert not thread.is_alive()
    assert [f.id for f in frames] == [1, 2, 3, 4]
    assert [f.event for f in frames][-1] == "run.finished"


# ---- AC 7: the stream closes on the terminal event ----------------------------------------------


def test_the_stream_closes_on_the_terminal_frame_on_the_run_and_eval_paths(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    finished = publish_finished_session(conn)
    evaluated = seed_eval_session(conn)
    with client_over(tmp_harnessed_home) as client:
        run_frames = frames_of(client, finished.run_id)
        eval_frames = frames_of(client, evaluated.session_id)
    assert run_frames[-1].event == "run.finished"
    assert eval_frames[-1].event == "eval.run.finished"
    assert run_frames[-1].event == read.terminal_for(conn, finished.run_id)
    assert eval_frames[-1].event == read.terminal_for(conn, evaluated.session_id)
    assert len(run_frames) == len(read.replay(conn, finished.run_id))
    assert len(eval_frames) == len(read.replay(conn, evaluated.session_id))
    lines = [
        f"run path   session id {finished.run_id}: {len(run_frames)} frames, closed after "
        f"id: {run_frames[-1].id} event: {run_frames[-1].event}",
        f"eval path  session id {evaluated.session_id}: {len(eval_frames)} frames, closed after "
        f"id: {eval_frames[-1].id} event: {eval_frames[-1].event}",
        "",
        "terminal frame (run path):",
        f"  id: {run_frames[-1].id}",
        f"  event: {run_frames[-1].event}",
        f"  data: {run_frames[-1].data}",
        "",
        f"TERMINAL_PAIRS = {dict(read.TERMINAL_PAIRS)}",
        f"IDLE_CEILING_S = {sse.IDLE_CEILING_S}, not reached: the close was the terminal frame.",
    ]
    capture("terminal-close.txt", "\n".join(lines) + "\n")


def test_a_terminal_event_before_the_cursor_still_closes_the_stream_at_once(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """A finished session resumed at its last seq is the same code path: zero frames, closed."""
    bus = publish_finished_session(conn)
    total = len(read.replay(conn, bus.run_id))
    with client_over(tmp_harnessed_home) as client:
        frames = frames_of(client, bus.run_id, str(total))
    assert frames == []


# ---- AC 8: a session with no terminal event closes on the idle ceiling ---------------------------


def test_a_session_with_no_terminal_event_closes_cleanly_on_the_idle_ceiling(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sse.IDLE_CEILING_S == 900
    bus = EventBus(conn=conn)
    bus.publish("improve.run.started", {"suite": "smoke"})
    monkeypatch.setattr(sse, "IDLE_CEILING_S", SHORT_CEILING_S)
    with client_over(tmp_harnessed_home) as client:
        response = stream(client, bus.run_id)
    assert response.status_code == 200
    frames = parse_frames(response.text)
    assert [f.event for f in frames] == ["improve.run.started"]
    assert frames[-1].event != read.terminal_for(conn, bus.run_id)


def test_a_crashed_publisher_that_already_emitted_a_nested_finish_still_closes_on_the_ceiling(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 081 AC 3: a cycle that died after its baseline eval neither hangs nor closes early."""
    bus = EventBus(conn=conn)
    bus.publish("improve.run.started", {"suite": "smoke"})
    bus.publish("eval.run.started", {"suite": "smoke"})
    bus.publish("eval.run.finished", {"suite": "smoke"})
    monkeypatch.setattr(sse, "IDLE_CEILING_S", SHORT_CEILING_S)
    with client_over(tmp_harnessed_home) as client:
        frames = frames_of(client, bus.run_id)
        resumed = frames_of(client, bus.run_id, "3")
    assert [f.event for f in frames][-1] == "eval.run.finished"
    assert [f.id for f in frames] == [1, 2, 3]
    assert resumed == []


# ---- ticket 081: an improve session streams through every inner eval ---------------------------


def stream_a_cycle(
    conn: sqlite3.Connection, home: Path, *, dry_run: bool = False
) -> tuple[str, list[Frame]]:
    bus = EventBus(conn=conn)
    cycle(
        conn, home, load_named_suite("smoke"), load_named_suite("holdout"), dry_run=dry_run, bus=bus
    )
    with client_over(home) as client:
        return bus.run_id, frames_of(client, bus.run_id)


def test_an_improve_stream_stays_open_through_every_inner_eval_and_closes_on_the_cycle_end(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    session_id, frames = stream_a_cycle(conn, tmp_harnessed_home)
    stored = read.replay(conn, session_id)
    events = [f.event for f in frames]
    assert [f.id for f in frames] == [e.seq for e in stored], "every frame, none dropped"
    assert events[0] == "improve.run.started"
    assert events[-1] == "improve.run.finished"
    assert events.count("improve.run.finished") == 1
    assert events.count("eval.run.finished") == 3, "baseline, candidate and holdout"
    assert "improve.promotion.decided" in events
    recorded = [json.loads(f.data)["step"] for f in frames if f.event == "improve.step.recorded"]
    assert recorded == ["baseline", "reflect", "candidate", "holdout", "promote"]

    first_nested = events.index("eval.run.finished")
    lines = [
        f"improve session id: {session_id}, {len(stored)} events recorded",
        "",
        "BEFORE (terminal decided by name alone, TERMINAL_NAMES = run.finished, "
        "eval.run.finished):",
        f"  stream closed after id: {frames[first_nested].id} event: eval.run.finished "
        f"(the baseline eval), {len(stored) - first_nested - 1} frames undelivered",
        "",
        "AFTER (terminal = the finish paired with the seq 1 start, "
        f"{events[0]} -> {read.terminal_for(conn, session_id)}):",
        f"  stream closed after id: {frames[-1].id} event: {events[-1]}, 0 frames undelivered",
        "",
        "frames delivered:",
    ]
    lines += [f"  id: {f.id:<4} event: {f.event:<28} data: {f.data}" for f in frames]
    capture_081("improve-stream.txt", "\n".join(lines) + "\n")


def test_a_dry_run_cycle_stream_also_closes_on_its_finish(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    _, frames = stream_a_cycle(conn, tmp_harnessed_home, dry_run=True)
    events = [f.event for f in frames]
    assert events[-1] == "improve.run.finished"
    assert json.loads(frames[-1].data)["dry_run"] is True
    assert "improve.promotion.decided" not in events


def test_resuming_an_improve_stream_just_after_its_baseline_eval_delivers_the_rest(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    session_id, frames = stream_a_cycle(conn, tmp_harnessed_home)
    cut = next(f.id for f in frames if f.event == "eval.run.finished")
    with client_over(tmp_harnessed_home) as client:
        resumed = frames_of(client, session_id, str(cut))
    assert resumed == [f for f in frames if f.id > cut]
    assert resumed[-1].event == "improve.run.finished"


def test_the_stream_decides_its_end_by_pairing_never_by_a_flat_name_set() -> None:
    """MUST rule 20, structurally: ``sse.py`` reads ``terminal_for`` and never TERMINAL_NAMES."""
    source = SSE_PATH.read_text(encoding="utf-8")
    assert "TERMINAL_NAMES" not in source
    assert "terminal_for" in source


def test_the_idle_ceiling_counts_from_the_last_frame_not_from_the_subscription(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session still emitting is not idle: each frame restarts the ceiling (review gate)."""

    def event(seq: int) -> read.ReplayedEvent:
        return read.ReplayedEvent("s", seq, "improve.run.started", "t", {"seq": seq})

    # One new row per tick for five ticks, so the live stream outlasts SHORT_CEILING_S in total
    # while no gap between two frames comes near it.
    pending = [event(seq) for seq in range(2, 7)]
    versions = iter(range(1, 1000))
    monkeypatch.setattr(sse, "IDLE_CEILING_S", SHORT_CEILING_S)
    monkeypatch.setattr(sse.db, "data_version", lambda _conn: next(versions))
    monkeypatch.setattr(sse.read, "after", lambda *_: (pending.pop(0),) if pending else ())

    async def collect() -> list[bytes]:
        return [
            chunk
            async for chunk in sse.tail(conn, "s", 0, (event(1),), 0, follow=True, terminal=None)
        ]

    chunks = asyncio.run(collect())
    assert [f.id for f in parse_frames(b"".join(chunks).decode())] == [1, 2, 3, 4, 5, 6]


# ---- AC 9: the module shape - one tick constant, the cursor in the generator, no registry -------


def sse_module() -> ast.Module:
    return ast.parse(SSE_PATH.read_text(encoding="utf-8"))


def test_the_poll_interval_is_declared_once_as_a_tenth_of_a_second() -> None:
    declarations = [
        node
        for node in ast.walk(sse_module())
        if isinstance(node, ast.Assign | ast.AnnAssign)
        and "POLL_INTERVAL_S"
        in {
            ast.unparse(t)
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        }
    ]
    assert len(declarations) == 1
    value = declarations[0].value
    assert isinstance(value, ast.Constant) and value.value == 0.1
    assert SSE_PATH.read_text(encoding="utf-8").count("POLL_INTERVAL_S") >= 2, "declared and used"
    assert sse.POLL_INTERVAL_S == 0.1
    assert isinstance(sse.POLL_INTERVAL_S, float)


def test_the_high_water_mark_lives_in_the_generator_and_no_registry_at_module_level() -> None:
    tree = sse_module()
    generators = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
    assert len(generators) == 1, [g.name for g in generators]
    generator = generators[0]
    assert any(isinstance(n, ast.Yield) for n in ast.walk(generator))
    stores = {
        n.id
        for n in ast.walk(generator)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }
    assert "cursor" in stores, "the high-water mark is a local of the generator"
    module_names = {
        ast.unparse(t)
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
    }
    assert module_names <= {"POLL_INTERVAL_S", "IDLE_CEILING_S", "__all__"}, module_names
    for name, value in vars(sse).items():
        if name.startswith("__"):
            continue
        assert not isinstance(value, dict | list | set | sqlite3.Connection | sqlite3.Cursor), name
        assert not hasattr(value, "cache_info"), name
    values = {
        name: value
        for name, value in vars(sse).items()
        if not name.startswith("__") and not callable(value) and not isinstance(value, ModuleType)
    }
    assert set(values) == {"POLL_INTERVAL_S", "IDLE_CEILING_S", "annotations"}, values
    assert not any(
        keyword in name.lower()
        for name in values
        for keyword in ("cursor", "subscri", "registry", "clients")
    )


def test_the_sse_module_owns_no_sql_and_reads_rows_through_the_events_package() -> None:
    offenders = [
        f"{node.lineno} {node.value!r}"
        for node in ast.walk(sse_module())
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(spelling in node.value.upper() for spelling in SQL_SPELLINGS)
    ]
    assert offenders == []
    attrs = {
        node.attr
        for node in ast.walk(sse_module())
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "read"
    }
    assert "after" in attrs
    assert "sse_starlette" not in SSE_PATH.read_text(encoding="utf-8")


def test_the_stream_handler_renders_one_gateway_callable_and_names_the_glossary_term() -> None:
    tree = ast.parse(ROUTE_PATH.read_text(encoding="utf-8"))
    handlers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Call) for d in node.decorator_list)
    }
    assert "session_stream" in handlers
    calls = [
        n
        for stmt in handlers["session_stream"].body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Call)
    ]
    assert len(calls) == 1
    func = calls[0].func
    assert isinstance(func, ast.Attribute)
    assert isinstance(func.value, ast.Name) and func.value.id == "sse"
    source = ROUTE_PATH.read_text(encoding="utf-8")
    assert "{{session_id}}/stream" in source, "the path is an f-string over the prefix"
    assert "run_id" not in source


# ---- AC 10: an unknown session id, or an eval run id in its place, is 404 -----------------------


def test_an_unknown_session_id_is_404_in_the_decision_24_body_not_an_open_stream(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    missing = ids.new_run_id()
    with client_over(tmp_harnessed_home) as client:
        response = stream(client, missing)
        with_cursor = stream(client, missing, "3")
    for candidate in (response, with_cursor):
        assert candidate.status_code == 404
        assert candidate.headers["content-type"] == "application/json"
        body = candidate.json()
        assert set(body) == {"error", "message"}
        assert body["error"] == SurfaceNotFound.__name__
        assert missing in body["message"]


def test_an_eval_run_id_in_the_session_id_position_is_404_pinning_decision_14(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    session = seed_eval_session(conn)
    with client_over(tmp_harnessed_home) as client:
        by_session = stream(client, session.session_id)
        by_eval_run = stream(client, session.eval_run_id)
        by_case_key = stream(client, session.case_run_keys[0])
    assert by_session.status_code == 200
    assert parse_frames(by_session.text)[-1].event == "eval.run.finished"
    for wrong in (by_eval_run, by_case_key):
        assert wrong.status_code == 404
        assert wrong.json()["error"] == SurfaceNotFound.__name__


# ---- AC 11: no global stream across session ids ------------------------------------------------


def test_the_route_table_offers_one_stream_scoped_to_a_session_id_and_no_global_one(
    tmp_harnessed_home: Path,
) -> None:
    app = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    paths = [route.path for route in app.routes]
    assert all(isinstance(p, str) for p in paths)
    streams = [p for p in paths if p.endswith("/stream")]
    assert streams == [STREAM_ROUTE]
    assert "{session_id}" in streams[0]
    assert "/v1/stream" not in paths
    assert "/v1/events" not in paths
    assert "/v1/sessions/stream" not in paths
    assert all("{session_id}" in p for p in paths if "/sessions/" in p)


def test_seq_carries_no_meaning_across_session_ids(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """Two sessions interleave on the table; each stream from cursor 1 is its own rows only."""
    one, two = EventBus(conn=conn), EventBus(conn=conn)
    one.publish("run.started", {"provider": "fake"})
    two.publish("run.started", {"provider": "fake"})
    one.publish("loop.iteration.started", {"iteration": 1})
    two.publish("run.finished", {"iterations": 0, "guardrail": NO_MORE_TOOL_CALLS})
    one.publish("run.finished", {"iterations": 1, "guardrail": NO_MORE_TOOL_CALLS})
    with client_over(tmp_harnessed_home) as client:
        first = frames_of(client, one.run_id, "1")
        second = frames_of(client, two.run_id, "1")
    assert [f.id for f in first] == [2, 3]
    assert [f.id for f in second] == [2]
    assert [f.event for f in first] == ["loop.iteration.started", "run.finished"]
    assert [f.event for f in second] == ["run.finished"]


# ---- the read half: events/read.py::after owns the seq > cursor query ---------------------------


def test_after_is_the_tail_of_replay_and_refuses_an_unknown_session_id(
    conn: sqlite3.Connection,
) -> None:
    bus = publish_finished_session(conn)
    full = read.replay(conn, bus.run_id)
    assert read.after(conn, bus.run_id, 0) == full
    assert read.after(conn, bus.run_id, 2) == full[2:]
    assert read.after(conn, bus.run_id, len(full)) == ()
    assert read.after(conn, bus.run_id, len(full) + 10) == ()
    with pytest.raises(SurfaceNotFound):
        read.after(conn, ids.new_run_id(), 0)
    with pytest.raises(SurfaceNotFound):
        read.after(conn, ids.new_run_id(), 99)
