"""FR-7, FR-38: a run's token usage is on disk, so its cost can be read after the process exits.

Route 1 of ticket 085 (architecture decision 9): migration ``006_turn_usage`` adds three nullable
counts to ``turns`` and the journal writes and reads them. "After the process exits" is a fresh
connection to the same file, never the one that wrote it. A turn recorded before 006 carries
``NULL`` and reads as unknown, never as a zero somebody could add up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from harnessed.events import names
from harnessed.loop import run_loop
from harnessed.loop.journal import Journal
from harnessed.memory import db
from harnessed.providers.translate import tool_call_payload
from harnessed.providers.types import (
    ChatRequest,
    ChatResponse,
    Message,
    ToolCall,
    ToolResult,
    Usage,
)
from harnessed.tools import Tool, ToolRegistry
from support.spies import CountingProvider

RUN = "run_usage"
EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "085" / "usage-persisted.txt"


@pytest.fixture
def path(tmp_harnessed_home: Path) -> Path:
    return db.database_path()


@pytest.fixture
def conn(path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(path)
    yield connection
    connection.close()


def journal_on(connection: sqlite3.Connection) -> Journal:
    return Journal(connection, RUN, serialize_call=tool_call_payload, provider="fake")


def tool_turn(iteration: int, usage: Usage) -> ChatResponse:
    call_id = f"call_{iteration}"
    return ChatResponse(
        response_id=f"resp_{iteration}",
        model="fake",
        content="",
        tool_calls=(ToolCall(id=call_id, name="touch", arguments={}),),
        stop_reason="tool_use",
        usage=usage,
    )


def request() -> ChatRequest:
    return ChatRequest(model="fake", messages=(Message(role="user", content="go"),))


def registry() -> ToolRegistry:
    return ToolRegistry([Tool(name="touch", handler=lambda _: "ok", description="Touch.")])


def record_tool_turn(journal: Journal, iteration: int, usage: Usage) -> None:
    journal.record(
        iteration,
        request(),
        tool_turn(iteration, usage),
        (ToolResult(call_id=f"call_{iteration}", content="ok"),),
    )


def total(per_turn: dict[int, Usage | None]) -> Usage:
    known = [usage for usage in per_turn.values() if usage is not None]
    return Usage(
        input_tokens=sum(u.input_tokens for u in known),
        output_tokens=sum(u.output_tokens for u in known),
        cached_input_tokens=sum(u.cached_input_tokens for u in known),
    )


def test_migration_006_is_shipped_and_adds_three_nullable_counts(conn: sqlite3.Connection) -> None:
    shipped = db.load_migrations()
    assert (6, "turn_usage") in {(m.version, m.slug) for m in shipped}
    assert db.schema_version(conn) >= 6
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(turns)")}
    for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
        assert columns[name][2].upper() == "INTEGER"
        assert columns[name][3] == 0, f"{name} must be nullable"
        assert columns[name][4] is None, f"{name} must have no default"


def test_a_completed_run_reads_back_its_per_turn_usage_after_the_process_exits(
    path: Path, conn: sqlite3.Connection
) -> None:
    first = Usage(input_tokens=120, output_tokens=30, cached_input_tokens=100)
    second = Usage(input_tokens=200, output_tokens=15, cached_input_tokens=0)
    record_tool_turn(journal_on(conn), 1, first)
    record_tool_turn(journal_on(conn), 2, second)
    conn.close()

    with closing(db.connect(path)) as fresh:
        per_turn = journal_on(fresh).turn_usage()
        replayed = journal_on(fresh).recorded(1)
    assert per_turn == {1: first, 2: second}
    assert replayed is not None
    assert replayed.response.usage == first


def test_a_resumed_run_counts_each_replayed_turn_once_and_makes_no_call_for_it(
    path: Path, conn: sqlite3.Connection
) -> None:
    recorded = Usage(input_tokens=100, output_tokens=10, cached_input_tokens=40)
    record_tool_turn(journal_on(conn), 1, recorded)
    record_tool_turn(journal_on(conn), 2, recorded)

    fresh_usage = Usage(input_tokens=300, output_tokens=5, cached_input_tokens=280)
    model = CountingProvider(
        script=[ChatResponse(response_id="r3", model="fake", content="done", usage=fresh_usage)]
    )
    result = run_loop(model, registry(), request(), limit=10, journal=journal_on(conn))
    assert model.calls == 1
    assert result.iterations == 3

    per_turn = journal_on(conn).turn_usage()
    assert per_turn == {1: recorded, 2: recorded, 3: fresh_usage}
    assert total(per_turn) == Usage(input_tokens=500, output_tokens=25, cached_input_tokens=360)

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"turn {i}: {u}" for i, u in sorted(per_turn.items())]
    lines.append(f"resumed total (provider calls this run: {model.calls}): {total(per_turn)}")
    EVIDENCE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_turn_recorded_before_006_reads_as_unknown_and_replays_as_empty_usage(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO turns (run_id, iteration, request, response, stop_reason, created_at) "
        "VALUES (?, 1, '{}', 'old answer', 'end_turn', '2026-01-01T00:00:00+00:00')",
        (RUN,),
    )
    journal = journal_on(conn)
    assert journal.turn_usage() == {1: None}
    replayed = journal.recorded(1)
    assert replayed is not None
    assert replayed.response.usage == Usage()


def test_a_turn_recorded_from_006_on_has_no_null_count(conn: sqlite3.Connection) -> None:
    record_tool_turn(journal_on(conn), 1, Usage())
    row = conn.execute(
        "SELECT input_tokens, output_tokens, cached_input_tokens FROM turns WHERE run_id = ?",
        (RUN,),
    ).fetchone()
    assert row == (0, 0, 0)


def test_route_1_renames_no_event() -> None:
    assert "provider.response.received" in names.NAMES
