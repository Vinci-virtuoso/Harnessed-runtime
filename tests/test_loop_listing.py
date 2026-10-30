"""FR-41: the loop-run listing over ``turns``, and the payload one loop run renders as (ticket 066).

The defining hazard is the key shape. ``turns.run_id`` holds two different things: a plain
**session id** from ``harnessed run`` and a colon-joined **case run key** from every scored eval
case. A listing that ignores the difference reports a five-case eval as five top-level runs, so
every row here carries ``key_kind`` and the counting test seeds one of each kind and counts them.

``loop/`` may not import ``evals/``, so the shape predicate lives in ``loop/runs.py`` and this
file - which may import both sides - pins it to the real ``runner.case_run_key`` output and to a
real ``ids.new_run_id()``. The two definitions are held together here rather than by a docstring.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from harnessed import clock, ids
from harnessed.errors import HarnessError, SurfaceNotFound
from harnessed.evals import runner
from harnessed.evals.suite import Case, GraderSpec, Suite
from harnessed.loop import runs
from harnessed.loop.engine import LoopResult
from harnessed.loop.journal import Journal
from harnessed.memory import db
from harnessed.providers.translate import tool_call_payload
from harnessed.providers.types import ChatRequest, ChatResponse, Message

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence" / "066" / "key-kinds.txt"

MODEL = "fake-model"
WORDS = ("alpha", "beta", "gamma", "delta", "epsilon")

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")


# ---- benches -------------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


def tick(seconds: int) -> None:
    """Move the frozen clock forward so ``created_at`` stops tying and newest-first is testable."""
    clock.freeze(clock.now() + timedelta(seconds=seconds))


def record_turns(conn: sqlite3.Connection, run_key: str, *replies: str) -> None:
    """Journal one turn per reply under ``run_key``, each a second apart, the last one terminal."""
    journal = Journal(conn, run_key, serialize_call=tool_call_payload, provider="fake")
    request = ChatRequest(model=MODEL, system="", messages=(Message(role="user", content="hi"),))
    for iteration, reply in enumerate(replies, start=1):
        tick(1)
        last = iteration == len(replies)
        response = ChatResponse(
            response_id=f"resp_{iteration}",
            model=MODEL,
            content=reply,
            stop_reason="end_turn" if last else "tool_use",
        )
        journal.record(iteration, request, response, ())


def seed_session(conn: sqlite3.Connection, *replies: str) -> str:
    """One ``harnessed run``: a fresh session id with its turns journaled under it."""
    session_id = ids.new_run_id()
    record_turns(conn, session_id, *replies)
    return session_id


@dataclasses.dataclass
class JournalingSolver:
    """A ``Solve`` that journals one turn under the key the runner hands it, like ``Session.solve``.

    This is what makes the five case run keys in the listing real ones: they were produced by
    ``runner.case_run_key`` inside ``run_suite``, not typed into the test.
    """

    conn: sqlite3.Connection
    run_keys: list[str] = dataclasses.field(default_factory=list)

    def __call__(self, request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        assert run_key is not None
        self.run_keys.append(run_key)
        word = request.messages[-1].content.removeprefix("say ")
        record_turns(self.conn, run_key, word)
        return LoopResult(word, (), 1)


def five_case_suite() -> Suite:
    """Five cases, one grader each, scored purely from the reply text."""
    return Suite(
        name="five",
        path=Path("five.yaml"),
        cases=tuple(
            Case(
                id=f"five-{index}",
                prompt=f"say {word}",
                graders=(GraderSpec(name="exact_match", expect={"value": word}),),
            )
            for index, word in enumerate(WORDS, start=1)
        ),
    )


def seed_eval(conn: sqlite3.Connection) -> tuple[str, list[str]]:
    """One five-case eval through the real runner. Returns its eval run id and its case run keys."""
    solver = JournalingSolver(conn)
    result = runner.run_suite(five_case_suite(), solver, conn, model=MODEL, provider="fake")
    assert len(solver.run_keys) == 5
    return result.eval_run_id, solver.run_keys


# ---- AC 1: at most N rows, newest first, frozen dataclasses --------------------------------------


def test_listing_returns_at_most_n_rows_newest_first(conn: sqlite3.Connection) -> None:
    first = seed_session(conn, "one")
    second = seed_session(conn, "two")
    third = seed_session(conn, "three")

    rows = runs.listing(conn, limit=2)

    assert [row.run_key for row in rows] == [third, second]
    assert first not in {row.run_key for row in rows}
    assert [row.run_key for row in runs.listing(conn, limit=10)] == [third, second, first]


def test_each_row_is_a_frozen_dataclass_carrying_key_start_and_terminal_state(
    conn: sqlite3.Connection,
) -> None:
    """Never a raw row and never a bare dict: the package's own typed shape."""
    session_id = seed_session(conn, "working", "done")
    started_at = conn.execute(
        "SELECT MIN(created_at) FROM turns WHERE run_id = ?", (session_id,)
    ).fetchone()[0]

    (row,) = runs.listing(conn, limit=5)

    assert isinstance(row, runs.LoopRun)
    assert dataclasses.is_dataclass(row) and not isinstance(row, dict | tuple)
    assert row.run_key == session_id
    assert row.key_kind == "session"
    assert row.started_at == started_at
    assert row.iterations == 2
    assert row.stop_reason == "end_turn"
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.run_key = "other"  # type: ignore[misc]


# ---- AC 2: one session and one five-case eval count as 1 and 5, never 6 --------------------------


def test_one_session_and_one_five_case_eval_are_labelled_1_and_5_never_6(
    conn: sqlite3.Connection,
) -> None:
    session_id = seed_session(conn, "hello")
    _eval_run_id, case_keys = seed_eval(conn)

    rows = runs.listing(conn, limit=20)
    by_kind: dict[str, list[str]] = {}
    for row in rows:
        by_kind.setdefault(row.key_kind, []).append(row.run_key)

    assert len(rows) == 6
    assert set(by_kind) == {"session", "case_run"}
    assert by_kind["session"] == [session_id]
    assert sorted(by_kind["case_run"]) == sorted(case_keys)
    assert len(by_kind["case_run"]) == 5

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "ticket 066 - loop/runs.py::listing over one `harnessed run` and one five-case eval",
        "",
        f"rows: {len(rows)}  session: {len(by_kind['session'])}  "
        f"case_run: {len(by_kind['case_run'])}",
        "",
        *(
            f"{row.key_kind:<9} {row.run_key}  started {row.started_at}  {row.stop_reason}"
            for row in rows
        ),
        "",
    ]
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")
    assert EVIDENCE.read_text(encoding="utf-8").count("\nsession ") == 1
    assert EVIDENCE.read_text(encoding="utf-8").count("\ncase_run ") == 5


# ---- AC 3: the shape predicate is pinned to both real producers ----------------------------------


def test_the_shape_predicate_classifies_a_real_case_run_key_and_a_real_session_id() -> None:
    case_key = runner.case_run_key("eval_01HZZZZZZZZZZZZZZZZZZZZZZZ", "smoke-1", 1)
    session_id = ids.new_run_id()

    assert runs.key_kind_of(case_key) == "case_run"
    assert runs.key_kind_of(session_id) == "session"
    assert runs.key_kind_of(runner.case_run_key(session_id, "case", 3)) == "case_run"


# ---- AC 4: the loop listing and the eval listing are disjoint ------------------------------------


def test_listing_and_runner_recent_return_disjoint_id_sets(conn: sqlite3.Connection) -> None:
    session_id = seed_session(conn, "hello")
    eval_run_id, case_keys = seed_eval(conn)

    loop_keys = {row.run_key for row in runs.listing(conn, limit=20)}
    eval_ids = {summary.eval_run_id for summary in runner.recent(conn, limit=20)}

    assert loop_keys and eval_ids, "a disjointness claim over an empty set proves nothing"
    assert loop_keys == {session_id, *case_keys}
    assert eval_ids == {eval_run_id}
    assert loop_keys.isdisjoint(eval_ids)
    assert eval_run_id not in loop_keys
    assert session_id not in eval_ids


# ---- AC 5 and 6: run_payload, from the journal, or SurfaceNotFound -------------------------------


def test_run_payload_is_built_from_the_journal(conn: sqlite3.Connection) -> None:
    session_id = seed_session(conn, "thinking", "the answer")
    started_at, finished_at = conn.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM turns WHERE run_id = ?", (session_id,)
    ).fetchone()

    payload = runs.run_payload(conn, session_id)

    assert type(payload) is dict
    assert payload == {
        "run_key": session_id,
        "key_kind": "session",
        "started_at": started_at,
        "finished_at": finished_at,
        "iterations": 2,
        "tool_calls": 0,
        "stop_reason": "end_turn",
        "reply": "the answer",
        "usage": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
        "duration_seconds": None,
        "duration_reason": "predates migration 007",
        "provider": "fake",
        "model": "fake-model",
    }


def test_run_payload_labels_a_case_run_key(conn: sqlite3.Connection) -> None:
    _eval_run_id, case_keys = seed_eval(conn)

    payload = runs.run_payload(conn, case_keys[0])

    assert payload["run_key"] == case_keys[0]
    assert payload["key_kind"] == "case_run"
    assert payload["reply"] == WORDS[0]
    assert payload["iterations"] == 1


def test_run_payload_agrees_with_the_listing_row(conn: sqlite3.Connection) -> None:
    """One callable, one truth: what the listing says about a run is what its payload says."""
    seed_session(conn, "a", "b", "c")
    (row,) = runs.listing(conn, limit=1)

    payload = runs.run_payload(conn, row.run_key)

    assert (payload["run_key"], payload["key_kind"]) == (row.run_key, row.key_kind)
    assert (payload["started_at"], payload["iterations"]) == (row.started_at, row.iterations)
    assert payload["stop_reason"] == row.stop_reason


def test_run_payload_for_an_unknown_key_raises_surface_not_found(conn: sqlite3.Connection) -> None:
    seed_session(conn, "hello")

    with pytest.raises(SurfaceNotFound) as caught:
        runs.run_payload(conn, "run_00000000000000000000000000")

    assert isinstance(caught.value, HarnessError)
    assert "run_00000000000000000000000000" in str(caught.value)


# ---- AC 9: an empty store lists nothing, never None and never an error ---------------------------


def test_an_empty_store_lists_nothing(conn: sqlite3.Connection) -> None:
    rows = runs.listing(conn, limit=20)

    assert rows == ()
    assert isinstance(rows, tuple)
