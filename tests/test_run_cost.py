"""FR-7, FR-35, FR-41: a run says how long it took and what it cost (ticket 087).

``turns.created_at`` is stamped when a turn is *recorded*, after the reply arrives, so a payload
built from ``MIN``/``MAX`` of it read every one-turn run as instantaneous. Migration
``007_turn_requested_at`` records when each request was sent; ``started_at`` is the first turn's
``requested_at`` and ``finished_at`` the last turn's ``created_at``. The model double here moves
the frozen clock inside ``complete()``, so every duration asserted is exact rather than timing
dependent.
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harnessed import clock, ids
from harnessed.evals import runner
from harnessed.evals.suite import Case, GraderSpec, Suite
from harnessed.loop import run_loop, runs
from harnessed.loop.engine import LoopResult
from harnessed.loop.journal import Journal
from harnessed.memory import db
from harnessed.providers.translate import tool_call_payload
from harnessed.providers.types import (
    ChatRequest,
    ChatResponse,
    Message,
    ToolCall,
    Usage,
)
from harnessed.tools import Tool, ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE = REPO_ROOT / "src" / "harnessed" / "loop" / "engine.py"
EVIDENCE = REPO_ROOT / "evidence" / "087" / "run-cost.txt"
T0 = datetime(2026, 9, 13, 9, 2, 20, tzinfo=UTC)
REPLY_SECONDS = 5

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


@dataclass
class SlowModel:
    """Answers from a script, and the frozen clock moves ``REPLY_SECONDS`` while it thinks."""

    script: list[ChatResponse]
    sent_at: list[datetime] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.sent_at.append(clock.now())
        clock.freeze(clock.now() + timedelta(seconds=REPLY_SECONDS))
        return self.script.pop(0)


def usage(n: int) -> Usage:
    return Usage(input_tokens=100 * n, output_tokens=10 * n, cached_input_tokens=n)


def answer(text: str, n: int = 1) -> ChatResponse:
    return ChatResponse(response_id=f"r_{text}", model="fake", content=text, usage=usage(n))


def tool_turn(n: int) -> ChatResponse:
    return ChatResponse(
        response_id=f"r_tool_{n}",
        model="fake",
        content="",
        tool_calls=(ToolCall(id=f"call_{n}", name="touch", arguments={}),),
        stop_reason="tool_use",
        usage=usage(n),
    )


def registry() -> ToolRegistry:
    return ToolRegistry([Tool(name="touch", handler=lambda _: "ok", description="Touch.")])


def request(text: str = "go") -> ChatRequest:
    return ChatRequest(model="fake", messages=(Message(role="user", content=text),))


def run(conn: sqlite3.Connection, key: str, *script: ChatResponse) -> SlowModel:
    model = SlowModel(list(script))
    journal = Journal(conn, key, serialize_call=tool_call_payload, provider="fake")
    run_loop(model, registry(), request(), limit=10, journal=journal)
    return model


def iso(instant: datetime) -> str:
    return clock.isoformat(instant)


def raw_turn(conn: sqlite3.Connection, key: str, iteration: int, at: str, *, usage: bool) -> None:
    """A row an older build wrote: no ``requested_at``, and optionally no usage either."""
    counts = (1, 2, 3) if usage else (None, None, None)
    conn.execute(
        "INSERT INTO turns (run_id, iteration, request, response, stop_reason, created_at, "
        "input_tokens, output_tokens, cached_input_tokens) "
        "VALUES (?, ?, '{}', 'old answer', 'end_turn', ?, ?, ?, ?)",
        (key, iteration, at, *counts),
    )
    conn.commit()


# ---- the schema ----------------------------------------------------------------------------------


def test_migration_007_adds_one_nullable_text_column_with_no_default(
    conn: sqlite3.Connection,
) -> None:
    shipped = db.load_migrations()
    assert (7, "turn_requested_at") in {(m.version, m.slug) for m in shipped}
    assert db.schema_version(conn) >= 7
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(turns)")}
    assert columns["requested_at"][2].upper() == "TEXT"
    assert columns["requested_at"][3] == 0
    assert columns["requested_at"][4] is None


# ---- AC 1 and 2: the duration is the request to the last recorded turn ---------------------------


def test_a_one_turn_run_starts_when_its_request_was_sent(conn: sqlite3.Connection) -> None:
    clock.freeze(T0)
    key = ids.new_run_id()
    model = run(conn, key, answer("done"))

    payload = runs.run_payload(conn, key)

    assert model.sent_at == [T0]
    assert payload["started_at"] == iso(T0)
    assert payload["finished_at"] == iso(T0 + timedelta(seconds=REPLY_SECONDS))
    assert clock.parse(str(payload["finished_at"])) > clock.parse(str(payload["started_at"]))


def test_a_longer_run_spans_the_first_request_to_the_last_recorded_turn(
    conn: sqlite3.Connection,
) -> None:
    clock.freeze(T0)
    key = ids.new_run_id()
    model = run(conn, key, tool_turn(1), tool_turn(2), answer("done", 3))

    payload = runs.run_payload(conn, key)
    last_created = conn.execute(
        "SELECT created_at FROM turns WHERE run_id = ? ORDER BY iteration DESC LIMIT 1", (key,)
    ).fetchone()[0]

    assert len(model.sent_at) == 3
    assert payload["started_at"] == iso(model.sent_at[0]) == iso(T0)
    assert payload["finished_at"] == last_created == iso(T0 + timedelta(seconds=3 * REPLY_SECONDS))


# ---- AC 3: an eval case run key follows the same rule --------------------------------------------


@dataclass
class LoopingSolver:
    """A ``Solve`` that runs the real loop under the key the runner hands it."""

    conn: sqlite3.Connection
    run_keys: list[str] = field(default_factory=list)

    def __call__(self, request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        assert run_key is not None
        self.run_keys.append(run_key)
        journal = Journal(self.conn, run_key, serialize_call=tool_call_payload, provider="fake")
        return run_loop(SlowModel([answer("pong")]), registry(), request, limit=5, journal=journal)


def test_an_eval_case_run_payload_and_listing_row_follow_the_same_rule(
    conn: sqlite3.Connection,
) -> None:
    clock.freeze(T0)
    suite = Suite(
        name="cost",
        path=Path("cost.yaml"),
        cases=(
            Case(
                id="cost-1",
                prompt="ping",
                graders=(GraderSpec(name="exact_match", expect={"value": "pong"}),),
            ),
        ),
    )
    solver = LoopingSolver(conn)
    runner.run_suite(suite, solver, conn, model="fake", provider="fake")
    (key,) = solver.run_keys

    payload = runs.run_payload(conn, key)
    (row,) = (row for row in runs.listing(conn, limit=20) if row.run_key == key)

    assert payload["key_kind"] == row.key_kind == "case_run"
    assert payload["started_at"] == row.started_at
    assert clock.parse(str(payload["finished_at"])) - clock.parse(row.started_at) == timedelta(
        seconds=REPLY_SECONDS
    )


# ---- AC 4: a pre-007 turn falls back to its own created_at ---------------------------------------


def test_a_turn_recorded_before_007_starts_at_its_created_at(conn: sqlite3.Connection) -> None:
    raw_turn(conn, "run_old", 1, "2026-01-01T00:00:00Z", usage=True)

    payload = runs.run_payload(conn, "run_old")
    (row,) = runs.listing(conn, limit=5)

    assert payload["started_at"] == row.started_at == "2026-01-01T00:00:00Z"
    assert payload["finished_at"] == "2026-01-01T00:00:00Z"
    assert "created_at" in (runs.run_payload.__doc__ or "")


# ---- AC 5 and 6: usage is summed, or null when any turn predates 006 -----------------------------


def test_usage_is_summed_over_every_turn(conn: sqlite3.Connection) -> None:
    key = ids.new_run_id()
    run(conn, key, tool_turn(1), answer("done", 2))

    payload = runs.run_payload(conn, key)

    assert payload["usage"] == {"input_tokens": 300, "output_tokens": 30, "cached_input_tokens": 3}


def test_usage_is_null_when_any_turn_predates_006(conn: sqlite3.Connection) -> None:
    raw_turn(conn, "run_mixed", 1, "2026-01-01T00:00:00Z", usage=False)
    raw_turn(conn, "run_mixed", 2, "2026-01-01T00:00:05Z", usage=True)

    assert runs.run_payload(conn, "run_mixed")["usage"] is None


# ---- AC 8: newest first, deterministic inside one clock tick -------------------------------------


def test_listing_is_newest_first_and_deterministic_within_one_tick(
    conn: sqlite3.Connection,
) -> None:
    clock.freeze(T0)
    first = ids.new_run_id()
    run(conn, first, answer("a"))
    clock.freeze(T0)
    second = ids.new_run_id()
    run(conn, second, answer("b"))
    clock.freeze(T0 + timedelta(seconds=1))
    third = ids.new_run_id()
    run(conn, third, answer("c"))

    keys = [row.run_key for row in runs.listing(conn, limit=10)]

    assert keys == [third, second, first]
    assert keys == [row.run_key for row in runs.listing(conn, limit=10)]


# ---- AC 9: two guardrails, no new event, and a replay keeps its time -----------------------------


def test_the_loop_still_has_two_exits_and_publishes_the_same_three_names() -> None:
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    run_loop_def = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_loop"
    )
    returns = [node for node in ast.walk(run_loop_def) if isinstance(node, ast.Return)]
    published = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(("loop.", "run."))
    }
    assert len(returns) == 2
    assert published == {
        "loop.iteration.started",
        "loop.iteration.finished",
        "loop.guardrail.tripped",
    }


def test_a_replayed_turn_keeps_the_requested_at_it_recorded(conn: sqlite3.Connection) -> None:
    clock.freeze(T0)
    key = ids.new_run_id()
    run(conn, key, tool_turn(1), tool_turn(2), answer("done"))
    before = conn.execute(
        "SELECT iteration, requested_at FROM turns WHERE run_id = ? ORDER BY iteration", (key,)
    ).fetchall()

    clock.freeze(T0 + timedelta(hours=1))
    model = run(conn, key)

    after = conn.execute(
        "SELECT iteration, requested_at FROM turns WHERE run_id = ? ORDER BY iteration", (key,)
    ).fetchall()
    assert model.sent_at == []
    assert after == before
    assert before[0][1] == iso(T0)


# ---- evidence ------------------------------------------------------------------------------------


def test_evidence_shows_a_real_duration_a_usage_total_and_the_fallback(
    conn: sqlite3.Connection,
) -> None:
    clock.freeze(T0)
    key = ids.new_run_id()
    run(conn, key, tool_turn(1), answer("done", 2))
    raw_turn(conn, "run_pre007", 1, "2026-01-01T00:00:00Z", usage=True)
    fresh = runs.run_payload(conn, key)
    old = runs.run_payload(conn, "run_pre007")
    took = clock.parse(str(fresh["finished_at"])) - clock.parse(str(fresh["started_at"]))
    per_turn = conn.execute(
        "SELECT iteration, requested_at, created_at, input_tokens, output_tokens, "
        "cached_input_tokens FROM turns WHERE run_id = ? ORDER BY iteration",
        (key,),
    ).fetchall()

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "ticket 087 - loop/runs.py::run_payload after migration 007",
        "",
        "turns:",
        *(f"  {row}" for row in per_turn),
        "",
        f"payload: {fresh}",
        f"duration: {took}",
        "",
        f"pre-007 run (started_at falls back to created_at): {old}",
        "",
    ]
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")
    assert fresh["started_at"] != fresh["finished_at"]
    assert fresh["usage"] == {"input_tokens": 300, "output_tokens": 30, "cached_input_tokens": 3}
