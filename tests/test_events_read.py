"""The ``events`` table is readable in ``(session id, seq)`` order and its replay agrees with the
journal (FR-38, ticket 068).

``events/bus.py`` writes the table and, until this ticket, nothing in ``src/`` read it. The read is
``events/read.py::replay`` - the ``events`` package's, so the CLI could call it too - and the
collection route ``GET /v1/sessions/{session_id}/events`` renders it through one subsystem
callable and no SQL of its own (MUST rule 14).

The glossary is load-bearing. ``events.run_id`` holds a **session id** and nothing else: an
**eval run id** keys ``eval_runs`` and a **case run key** keys ``turns``. The journal-agreement
check is asserted on the ``harnessed run`` path only, where the session id and the journal key are
the same string. On the eval path the two ledgers share no key, and that partition is asserted
positively - the session id matches 0 ``turns`` rows while the five case run keys each match some -
rather than skipped quietly.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import sqlite3
import statistics
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnessed import clock, config, ids
from harnessed.cli import run_cmd
from harnessed.cli.app import app
from harnessed.errors import LimitError, SurfaceNotFound, UnknownEventError
from harnessed.evals import runner
from harnessed.evals.suite import Case, GraderSpec, Suite, load_named_suite
from harnessed.events import names as names_module
from harnessed.events import read
from harnessed.events.bus import Event, EventBus
from harnessed.gateway import create_app
from harnessed.improve import steps
from harnessed.loop.engine import LoopResult, run_loop
from harnessed.loop.guardrails import ITERATION_LIMIT, NO_MORE_TOOL_CALLS
from harnessed.loop.journal import Journal
from harnessed.loop.prompt import build_system_prompt
from harnessed.memory import db, gate
from harnessed.providers.fake import FakeProvider
from harnessed.providers.translate import tool_call_payload
from harnessed.providers.types import ChatRequest, ChatResponse, Message
from harnessed.serialize import canonical
from harnessed.tools import default_registry
from support.spies import CountingProvider

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
NAMES_PATH = PACKAGE_ROOT / "events" / "names.py"
ROUTE_PATH = PACKAGE_ROOT / "gateway" / "routes" / "events.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "068"

#: ``events/names.py`` as it stood when this ticket started. The frozen set is byte unchanged by
#: this ticket: ``TERMINAL_NAMES`` is a derived grouping in ``events/read.py``, never an edit
#: here (MUST rule 4). A later ticket that adds a name updates this pin in the same commit.
#: +1: loop.route.decided (ticket 162); +2: task.started, task.paused (ticket 240)
NAMES_SHA256 = "0ee6c3f20036a443c7612f8ba9cd4739cb5b4bb102b587ce3b75a9d19f8f083f"

#: The SQL verbs the route module may not spell in any string constant (MUST rule 14).
SQL_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")

#: The route the collection read is served on. The parameter is the glossary term it carries.
COLLECTION_ROUTE = "/v1/sessions/{session_id}/events"

PROMPT = "ping"
MODEL = "fake-model"
WORDS = ("alpha", "beta", "gamma", "delta", "epsilon")

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- benches -------------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


def database_path(home: Path) -> Path:
    return home / db.DATABASE_FILENAME


def client_over(home: Path) -> TestClient:
    return TestClient(create_app(database_path(home)), raise_server_exceptions=False)


def tick(seconds: int) -> None:
    clock.freeze(clock.now() + timedelta(seconds=seconds))


def record_turns(conn: sqlite3.Connection, run_key: str, *replies: str) -> None:
    """Journal one turn per reply under ``run_key``, the last one terminal."""
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


@dataclasses.dataclass
class JournalingSolver:
    """A ``Solve`` that journals under the case run key it is handed, like ``Session.solve``."""

    conn: sqlite3.Connection
    run_keys: list[str] = dataclasses.field(default_factory=list)

    def __call__(self, request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        assert run_key is not None
        self.run_keys.append(run_key)
        word = request.messages[-1].content.removeprefix("say ")
        record_turns(self.conn, run_key, word)
        return LoopResult(word, (), 1)


def five_case_suite() -> Suite:
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


@dataclasses.dataclass(frozen=True)
class EvalSession:
    """One five-case eval driven under one session id: the bus's id, the eval run id, the keys."""

    session_id: str
    eval_run_id: str
    case_run_keys: tuple[str, ...]


def seed_eval_session(conn: sqlite3.Connection) -> EvalSession:
    """A five-case eval through the real runner, publishing on a bus that persists to ``conn``."""
    bus = EventBus(conn=conn)
    solver = JournalingSolver(conn)
    result = runner.run_suite(
        five_case_suite(), solver, conn, model=MODEL, provider="fake", bus=bus
    )
    assert len(solver.run_keys) == 5
    return EvalSession(bus.run_id, result.eval_run_id, tuple(solver.run_keys))


def turns_under(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM turns WHERE run_id = ?", (key,)).fetchone()
    return int(row[0])


def tool_rows_at(conn: sqlite3.Connection, key: str, iteration: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM tool_results WHERE run_id = ? AND iteration = ?", (key, iteration)
    ).fetchone()
    return int(row[0])


def journal_guardrail(conn: sqlite3.Connection, session_id: str) -> str:
    """The guardrail the journal implies for a finished run.

    The journal records turns and tool rows, never a guardrail string, so it is derived the way
    the loop decides it: a final turn with no tool call is guardrail 1 (the model answered);
    a final turn that still asked for a tool means the ceiling stopped it, guardrail 2.
    """
    row = conn.execute(
        "SELECT MAX(iteration) FROM turns WHERE run_id = ?", (session_id,)
    ).fetchone()
    assert row is not None and row[0] is not None, f"no turn journaled under {session_id!r}"
    last = int(row[0])
    return NO_MORE_TOOL_CALLS if tool_rows_at(conn, session_id, last) == 0 else ITERATION_LIMIT


def seqs_of(events: list[dict[str, Any]]) -> list[int]:
    return [int(event["seq"]) for event in events]


def assert_contiguous_from_one(seqs: list[int]) -> None:
    assert seqs, "an empty replay proves nothing about order"
    assert seqs == sorted(seqs), "seq is not ascending"
    assert len(set(seqs)) == len(seqs), "seq has duplicates"
    assert seqs == list(range(1, len(seqs) + 1)), "seq is not contiguous from 1"


# ---- AC 1 and 2: the collection route, ascending contiguous seq, every name from the frozen set --


def test_the_collection_route_returns_every_event_in_ascending_contiguous_seq(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    bus = EventBus(conn=conn)
    published = [
        bus.publish("run.started", {"provider": "fake"}),
        bus.publish("loop.iteration.started", {"iteration": 1}),
        bus.publish("loop.iteration.finished", {"iteration": 1, "tool_calls": 0}),
        bus.publish("run.finished", {"iterations": 1, "guardrail": NO_MORE_TOOL_CALLS}),
    ]
    with client_over(tmp_harnessed_home) as client:
        response = client.get(COLLECTION_ROUTE.format(session_id=bus.run_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == bus.run_id
    events = body["events"]
    assert_contiguous_from_one(seqs_of(events))
    assert [event["name"] for event in events] == [event.name for event in published]
    assert [event["payload"] for event in events] == [dict(event.payload) for event in published]
    assert [event["ts"] for event in events] == [event.ts for event in published]
    assert all(set(event) == {"seq", "name", "ts", "payload"} for event in events)


def test_the_route_returns_only_the_requested_session_and_the_bus_order_survives_interleaving(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """Two sessions publish alternately; each replay is its own rows, in its own order."""
    one, two = EventBus(conn=conn), EventBus(conn=conn)
    one.publish("run.started", {"provider": "fake"})
    two.publish("run.started", {"provider": "fake"})
    one.publish("loop.iteration.started", {"iteration": 1})
    two.publish("loop.iteration.started", {"iteration": 1})
    one.publish("run.finished", {"iterations": 1, "guardrail": NO_MORE_TOOL_CALLS})
    with client_over(tmp_harnessed_home) as client:
        first_body = client.get(COLLECTION_ROUTE.format(session_id=one.run_id)).json()
        second_body = client.get(COLLECTION_ROUTE.format(session_id=two.run_id)).json()
    first, second = first_body["events"], second_body["events"]
    assert_contiguous_from_one(seqs_of(first))
    assert_contiguous_from_one(seqs_of(second))
    assert first_body["session_id"] == one.run_id
    assert second_body["session_id"] == two.run_id
    assert {event.session_id for event in read.replay(conn, one.run_id)} == {one.run_id}
    assert {event.session_id for event in read.replay(conn, two.run_id)} == {two.run_id}
    assert [event["name"] for event in first][-1] == "run.finished"
    assert len(second) == 2


def test_every_name_in_the_response_is_drawn_verbatim_from_the_frozen_set(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    session = seed_eval_session(conn)
    with client_over(tmp_harnessed_home) as client:
        events = client.get(COLLECTION_ROUTE.format(session_id=session.session_id)).json()["events"]
    assert events
    foreign = [event["name"] for event in events if event["name"] not in names_module.NAMES]
    assert foreign == []


def test_a_name_outside_the_frozen_set_fails_the_read_the_way_publish_fails(
    conn: sqlite3.Connection,
) -> None:
    """The positive arm: a row the bus could never have written is refused, not served."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    conn.execute(
        "INSERT INTO events (run_id, seq, name, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (bus.run_id, 2, "run.renamed", "{}", clock.isoformat(clock.now())),
    )
    with pytest.raises(UnknownEventError):
        bus.publish("run.renamed", {})
    with pytest.raises(UnknownEventError):
        read.replay(conn, bus.run_id)


# ---- AC 3: names.py is byte unchanged; TERMINAL_NAMES is derived in read.py -------------------


def test_the_frozen_name_set_is_byte_unchanged() -> None:
    # Text mode normalizes line endings, so the pin is the same bytes on a CRLF checkout
    # (this repo has core.autocrlf on) and on a Linux runner.
    text = NAMES_PATH.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest == NAMES_SHA256, (
        "events/names.py changed. Adding a name is a decision for the ticket that publishes it "
        "(MUST rule 4); this ticket only reads the table."
    )
    assert "TERMINAL" not in NAMES_PATH.read_text(encoding="utf-8")


def test_terminal_pairs_are_a_derived_grouping_declared_in_the_read_module() -> None:
    """Decision 17's three pairs, each half in the frozen set (ticket 081).

    ``TERMINAL_NAMES`` survives as the finish halves, a vocabulary for "can this name end some
    session"; the stream decides with :func:`read.terminal_for`, never with it (MUST rule 20).
    """
    assert dict(read.TERMINAL_PAIRS) == {
        "run.started": "run.finished",
        "eval.run.started": "eval.run.finished",
        "improve.run.started": "improve.run.finished",
        "task.finished": "task.finished",
        "memory.consolidation.finished": "memory.consolidation.finished",
    }
    halves = set(read.TERMINAL_PAIRS) | set(read.TERMINAL_PAIRS.values())
    assert halves <= names_module.NAMES
    assert frozenset(read.TERMINAL_PAIRS.values()) == read.TERMINAL_NAMES
    assert isinstance(read.TERMINAL_NAMES, frozenset)
    assert not hasattr(names_module, "TERMINAL_NAMES")
    assert not hasattr(names_module, "TERMINAL_PAIRS")


def test_terminal_for_pairs_the_finish_with_the_start_at_seq_one(
    conn: sqlite3.Connection,
) -> None:
    for start, finish in read.TERMINAL_PAIRS.items():
        bus = EventBus(conn=conn)
        bus.publish(start, {})
        bus.publish("eval.run.finished", {})  # a nested finish changes nothing
        assert read.terminal_for(conn, bus.run_id) == finish
    unpaired = EventBus(conn=conn)
    unpaired.publish("loop.iteration.started", {"iteration": 1})
    assert read.terminal_for(conn, unpaired.run_id) is None
    with pytest.raises(SurfaceNotFound):
        read.terminal_for(conn, ids.new_run_id())


def test_the_read_module_docstring_describes_the_pairing_the_code_does() -> None:
    """Ticket 081: the docstring once promised an idle-ceiling close the code never did."""
    doc = read.__doc__ or ""
    assert "TERMINAL_PAIRS" in doc
    assert "terminal_for" in doc
    assert "seq 1" in doc
    source = (PACKAGE_ROOT / "events" / "read.py").read_text(encoding="utf-8")
    assert "closes on 069's idle" not in source
    assert "Two names, not the three" not in source


# ---- AC 4: a completed `harnessed run` replays in agreement with its journal --------------------


def test_a_completed_run_replays_terminally_and_agrees_with_its_journal(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    """The one path where the session id and the journal key are the same string."""
    session = run_cmd.open_session()
    finished = run_cmd.solve_with_memory(session, PROMPT)
    session_id = finished.run_id
    assert session_id == session.bus.run_id

    events = read.replay(session.conn, session_id)
    assert_contiguous_from_one([event.seq for event in events])
    assert events[-1].name == "run.finished"
    assert events[-1].name in read.TERMINAL_NAMES

    iterations_finished = sum(1 for event in events if event.name == "loop.iteration.finished")
    turns = turns_under(session.conn, session_id)
    assert turns > 0
    assert iterations_finished == turns

    terminal_guardrail = events[-1].payload["guardrail"]
    recorded_guardrail = journal_guardrail(session.conn, session_id)
    assert terminal_guardrail == recorded_guardrail
    assert terminal_guardrail == finished.result.guardrail

    with client_over(tmp_harnessed_home) as client:
        wire = client.get(COLLECTION_ROUTE.format(session_id=session_id)).json()
    assert [event["seq"] for event in wire["events"]] == [event.seq for event in events]
    assert [event["name"] for event in wire["events"]] == [event.name for event in events]

    lines = [f"session id: {session_id}", "", "replay:"]
    lines += [f"  {event.seq:>3}  {event.name:<28} {dict(event.payload)}" for event in events]
    lines += [
        "",
        "journal agreement:",
        f"  final event                 = {events[-1].name}",
        f"  loop.iteration.finished     = {iterations_finished}",
        f"  turns rows for session id   = {turns}",
        f"  guardrail (terminal payload) = {terminal_guardrail}",
        f"  guardrail (journal)          = {recorded_guardrail}",
    ]
    capture("replay-run-path.txt", "\n".join(lines) + "\n")
    session.conn.close()


# ---- AC 5: an eval session id matches 0 turns rows, asserted positively ------------------------


def test_an_eval_session_id_matches_zero_turns_rows_while_its_case_run_keys_match_some(
    conn: sqlite3.Connection,
) -> None:
    """Events partition by session id, ``turns`` by case run key: the two ledgers share no key.

    The journal-agreement comparison is therefore not made on this path, and this test is the
    positive statement of why, over two non-empty sets rather than a vacuous one.
    """
    session = seed_eval_session(conn)
    events = read.replay(conn, session.session_id)
    assert_contiguous_from_one([event.seq for event in events])
    assert events[-1].name == "eval.run.finished"

    per_key = {key: turns_under(conn, key) for key in session.case_run_keys}
    assert all(count > 0 for count in per_key.values()), per_key
    assert turns_under(conn, session.session_id) == 0
    assert turns_under(conn, session.eval_run_id) == 0
    assert session.session_id not in session.case_run_keys
    assert session.eval_run_id not in session.case_run_keys

    lines = [
        f"session id   : {session.session_id}  ({len(events)} events, "
        f"terminal {events[-1].name})",
        f"eval run id  : {session.eval_run_id}",
        "",
        "turns rows per key:",
        f"  session id  {session.session_id:<32} -> 0",
        f"  eval run id {session.eval_run_id:<32} -> 0",
    ]
    lines += [f"  case run key {key:<31} -> {count}" for key, count in per_key.items()]
    lines += ["", "the journal-agreement comparison is not made on the eval path: no shared key."]
    capture("eval-session-zero-turns.txt", "\n".join(lines) + "\n")


# ---- AC 6 and 7: an unknown session id, and an eval run id in the session id position, are 404 --


def test_an_unknown_session_id_is_404_in_the_decision_24_body_never_an_empty_200(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    missing = ids.new_run_id()
    with client_over(tmp_harnessed_home) as client:
        response = client.get(COLLECTION_ROUTE.format(session_id=missing))
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == SurfaceNotFound.__name__
    assert missing in body["message"]
    with pytest.raises(SurfaceNotFound):
        read.replay(conn, missing)


def test_an_eval_run_id_in_the_session_id_position_is_404_by_the_same_clause(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    session = seed_eval_session(conn)
    with client_over(tmp_harnessed_home) as client:
        by_session = client.get(COLLECTION_ROUTE.format(session_id=session.session_id))
        by_eval_run = client.get(COLLECTION_ROUTE.format(session_id=session.eval_run_id))
    assert by_session.status_code == 200
    assert by_eval_run.status_code == 404
    assert by_eval_run.json()["error"] == SurfaceNotFound.__name__
    assert session.eval_run_id in by_eval_run.json()["message"]


# ---- AC 8: the read is the events package's -----------------------------------------------------


def test_replay_over_a_bare_connection_returns_the_same_ordered_sequence(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    session = seed_eval_session(conn)
    direct = read.replay(conn, session.session_id)
    assert all(dataclasses.is_dataclass(event) for event in direct)
    assert all(event.session_id == session.session_id for event in direct)
    with client_over(tmp_harnessed_home) as client:
        wire = client.get(COLLECTION_ROUTE.format(session_id=session.session_id)).json()
    assert wire["session_id"] == session.session_id
    assert wire["events"] == [
        {"seq": e.seq, "name": e.name, "ts": e.ts, "payload": dict(e.payload)} for e in direct
    ]
    assert wire == read.json_payload(conn, session.session_id)


def test_the_read_module_imports_nothing_from_the_gateway() -> None:
    tree = ast.parse((PACKAGE_ROOT / "events" / "read.py").read_text(encoding="utf-8"))
    roots = {
        (node.module or "").split(".")[1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("harnessed.")
    }
    assert "gateway" not in roots
    assert "memory" not in roots, "events/ may not reach memory/ (IMPORT_TABLE)"


# ---- AC 9: the route module spells no SQL and calls one subsystem callable per handler ----------


def route_module() -> ast.Module:
    return ast.parse(ROUTE_PATH.read_text(encoding="utf-8"))


def test_the_route_module_contains_zero_sql_string_literals() -> None:
    offenders = [
        f"{node.lineno} {node.value!r}"
        for node in ast.walk(route_module())
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(verb in node.value for verb in SQL_VERBS)
    ]
    assert offenders == []


#: Every handler the module registers and the one callable each renders. The collection is
#: this ticket's; the stream beside it is ticket 069's, the listing 097's, the gate split
#: 128's and the totals 129's, all pinned here so the module cannot grow another handler, or a
#: second call in any, unnoticed.
HANDLER_CALLS = {
    "gate_split": (("read", "gate_split_payload"), ("read", "gate_split")),
    "session_events": (("read", "json_payload"),),
    "session_stream": (("sse", "stream"),),
    "sessions": (("read", "sessions"), ("read", "sessions_payload")),
    "totals": (("read", "totals_payload"), ("read", "totals")),
}

#: The only bare names a handler here may call: the two translations
#: ``gateway/routes/__init__.py`` owns, which are neither a query nor a re-shaping (decision 19).
#: A handler that reaches anything else by bare name is doing work the store should do.
LOCAL_HELPERS = {"parse_limit", "found"}


def test_each_handler_calls_its_store_read_and_at_most_its_emitter() -> None:
    """Decision 38: one call into the package that owns the table, then that package's emitter.

    Ticket 068 wrote this as "exactly one call", which was true while no handler here took a
    query parameter. ``GET /v1/sessions`` takes ``?limit=``, so the pin is restated as the
    decision states it: the subsystem calls are listed per handler and matched exactly, and the
    only other call permitted is the limit parse the gateway's own package owns.
    """
    handlers = [
        node
        for node in ast.walk(route_module())
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Call) for d in node.decorator_list)
    ]
    assert sorted(h.name for h in handlers) == sorted(HANDLER_CALLS)
    for handler in handlers:
        calls = [
            node for stmt in handler.body for node in ast.walk(stmt) if isinstance(node, ast.Call)
        ]
        subsystem: list[tuple[str, str]] = []
        for call in calls:
            func = call.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                subsystem.append((func.value.id, func.attr))
                continue
            assert isinstance(func, ast.Name), f"{handler.name} calls {ast.unparse(func)}"
            assert func.id in LOCAL_HELPERS, f"{handler.name} calls {func.id}"
        assert tuple(subsystem) == HANDLER_CALLS[handler.name], handler.name


def test_the_route_names_the_glossary_term_and_never_a_bare_run() -> None:
    source = ROUTE_PATH.read_text(encoding="utf-8")
    assert "{session_id}" in source
    assert "{run_id}" not in source
    assert "run_id" not in source


# ---- ticket 097: the session listing, so a session can be found without the terminal ------------
#
# FR-48, decision 36. ``events/read.py::sessions`` reads the ``seq`` 1 row of every session and
# probes each one's newest row for its paired finish; ``sessions_payload`` is the emitter both
# ``GET /v1/sessions`` and ``harnessed sessions list --json`` render. Nothing here computes: a
# row's four fields are the two columns of the first event, the id it is keyed by, and a name
# taken verbatim from the newest row when the pairing says that name ends this session.

LISTING_ROUTE = "/v1/sessions"
EVIDENCE_097 = REPO_ROOT / "evidence" / "097"

#: What ``?limit=`` means when the request omits it (decision 36).
DEFAULT_LISTING_LIMIT = 20

cli = CliRunner()


def capture_097(name: str, text: str) -> Path:
    """Write ticket 097's evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_097.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_097 / name
    path.write_text(text, encoding="utf-8")
    return path


def first_row_of(conn: sqlite3.Connection, session_id: str) -> tuple[str, str]:
    """The ``name`` and ``created_at`` of the ``seq`` 1 row, read straight from the table.

    The oracle for ``first_event`` and ``first_ts``: it re-reads the columns rather than calling
    the code under test, so a listing that computed either from something else would be caught.
    """
    row = conn.execute(
        "SELECT name, created_at FROM events WHERE run_id = ? AND seq = 1", (session_id,)
    ).fetchone()
    assert row is not None, f"no seq 1 row under {session_id!r}"
    return str(row[0]), str(row[1])


def plant_sessions(conn: sqlite3.Connection, count: int) -> list[str]:
    """``count`` sessions opened at the current instant, each one ``run.started`` and no more."""
    made = []
    for _ in range(count):
        bus = EventBus(conn=conn)
        bus.publish("run.started", {"provider": "fake"})
        made.append(bus.run_id)
    return made


def terminal_in(rows: tuple[Any, ...], session_id: str) -> str | None:
    """One session's ``terminal_event`` out of a listing, by id."""
    found = [row for row in rows if row.session_id == session_id]
    assert len(found) == 1, f"{session_id!r} appears {len(found)} times in the listing"
    return found[0].terminal_event


@dataclasses.dataclass(frozen=True)
class SeededHome:
    """The three sessions a seeded home holds, and what the eval and improve runs wrote beside
    them: the case run keys and inner eval run ids that must not be listed as sessions."""

    run: str
    evaluated: str
    improved: str
    case_run_keys: tuple[str, ...]
    eval_run_ids: tuple[str, ...]
    #: One ``(published name, newest row, terminal_event)`` triple per event the improvement
    #: cycle published, sampled from inside ``publish`` while the cycle was still running: the
    #: mid-cycle window MUST rule 20 is about.
    mid_cycle: tuple[tuple[str, str, str | None], ...]

    @property
    def sessions(self) -> tuple[str, ...]:
        return (self.run, self.evaluated, self.improved)


def seed_three_sessions(conn: sqlite3.Connection, home: Path) -> SeededHome:
    """One ``harnessed run``, one five-case eval run and one improve cycle, on three buses.

    Seeded through the real writers rather than planted, because the claim under test is about
    what the eval runner and the improvement cycle publish: five case runs that publish no event
    at all, and inner evals that publish onto the cycle's own bus and therefore share its
    session id (decision 36).
    """
    run_bus = EventBus(conn=conn)
    run_bus.publish("run.started", {"provider": "fake"})
    record_turns(conn, run_bus.run_id, "pong")
    run_bus.publish("run.finished", {"guardrail": NO_MORE_TOOL_CALLS})

    tick(1)
    evaluated = seed_eval_session(conn)

    tick(1)
    provider = FakeProvider(config.load())
    registry = default_registry(home)

    def solve(request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        return run_loop(provider, registry, request, limit=6)

    improve_bus = EventBus(conn=conn)
    sampled: list[tuple[str, str, str | None]] = []

    def watch(event: Event) -> None:
        """Sample the listing from inside ``publish``, after the row is written.

        ``bus.publish`` persists and then notifies, so an observer reads the table exactly as a
        concurrent request would at that instant. That is the only place the mid-cycle window
        exists: by the time ``run_cycle`` returns, ``improve.run.finished`` is the newest row.
        Observers are isolated, so this one records and never asserts - a raise here would be
        swallowed into an ``observer.failed`` event rather than failing the test.
        """
        listed = {row.session_id: row.terminal_event for row in read.sessions(conn, 50)}
        sampled.append(
            (event.name, read.latest(conn, improve_bus.run_id).name, listed.get(improve_bus.run_id))
        )

    improve_bus.subscribe(watch)

    steps.run_cycle(
        conn,
        suite=load_named_suite("smoke"),
        holdout=load_named_suite("holdout"),
        solve=solve,
        reflector=provider,
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
        bus=improve_bus,
    )

    inner = tuple(
        str(row[0])
        for row in conn.execute("SELECT eval_run_id FROM eval_runs ORDER BY eval_run_id")
    )
    return SeededHome(
        run=run_bus.run_id,
        evaluated=evaluated.session_id,
        improved=improve_bus.run_id,
        case_run_keys=evaluated.case_run_keys,
        eval_run_ids=inner,
        mid_cycle=tuple(sampled),
    )


def test_the_listing_holds_one_row_per_session_never_a_case_run_or_an_inner_eval(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 1: three sessions from a home that also wrote five case runs and several inner evals."""
    seeded = seed_three_sessions(conn, tmp_harnessed_home)
    with client_over(tmp_harnessed_home) as client:
        response = client.get(LISTING_ROUTE)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"sessions"}
    listed = [str(row["session_id"]) for row in payload["sessions"]]

    assert len(listed) == 3, f"listed {len(listed)}: {listed}"
    assert set(listed) == set(seeded.sessions)
    assert len(seeded.case_run_keys) == 5
    assert not set(listed) & set(seeded.case_run_keys), "a case run is listed as a session"
    assert not set(listed) & set(seeded.eval_run_ids), "an inner eval is listed as a session"
    assert len(seeded.eval_run_ids) >= 4, seeded.eval_run_ids
    assert all(turns_under(conn, key) > 0 for key in seeded.case_run_keys)

    capture_097(
        "sessions.txt",
        "\n".join(
            [
                "ticket 097 - GET /v1/sessions over a seeded home",
                "",
                f"harnessed run   session id: {seeded.run}",
                f"eval run        session id: {seeded.evaluated}",
                f"improve cycle   session id: {seeded.improved}",
                "",
                f"case run keys (journaled, no events, never listed): {len(seeded.case_run_keys)}",
                *(f"  {key}" for key in seeded.case_run_keys),
                f"eval run ids (the cycle's inner evals share its session id): "
                f"{len(seeded.eval_run_ids)}",
                *(f"  {eval_run_id}" for eval_run_id in seeded.eval_run_ids),
                "",
                f"http GET {LISTING_ROUTE}",
                f"  {response.content.decode()}",
                f"rows: {len(listed)}",
                "",
            ]
        )
        + "\n",
    )


def test_a_row_carries_the_first_event_its_ts_and_the_paired_terminal_event(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 2: four fields, every one of them read off a row."""
    seeded = seed_three_sessions(conn, tmp_harnessed_home)
    rows = read.sessions(conn, DEFAULT_LISTING_LIMIT)
    assert {row.session_id for row in rows} == set(seeded.sessions)
    assert all(dataclasses.is_dataclass(row) for row in rows)
    for row in rows:
        assert dataclasses.asdict(row).keys() == {
            "session_id",
            "first_event",
            "first_ts",
            "terminal_event",
            "title",
            "last_handler",
            "last_outcome",
            "last_cost",
            "last_at",
        }
        name, created_at = first_row_of(conn, row.session_id)
        assert row.first_event == name
        assert row.first_ts == created_at
        newest = read.latest(conn, row.session_id).name
        expected = newest if newest == read.TERMINAL_PAIRS.get(row.first_event) else None
        assert row.terminal_event == expected
        assert row.title is None, "none of these three sessions ever routed a message"

    by_id = {row.session_id: row for row in rows}
    assert by_id[seeded.run].first_event == "run.started"
    assert by_id[seeded.run].terminal_event == "run.finished"
    assert by_id[seeded.evaluated].first_event == "eval.run.started"
    assert by_id[seeded.evaluated].terminal_event == "eval.run.finished"
    assert by_id[seeded.improved].first_event == "improve.run.started"
    assert by_id[seeded.improved].terminal_event == "improve.run.finished"


def test_an_unpaired_first_event_leaves_the_terminal_event_null(conn: sqlite3.Connection) -> None:
    """A session whose seq 1 pairs with nothing has no finish to find, and says so."""
    bus = EventBus(conn=conn)
    bus.publish("loop.iteration.started", {"iteration": 1})
    bus.publish("loop.iteration.finished", {"iteration": 1})
    (row,) = read.sessions(conn, DEFAULT_LISTING_LIMIT)
    assert row.session_id == bus.run_id
    assert row.first_event == "loop.iteration.started"
    assert read.TERMINAL_PAIRS.get(row.first_event) is None
    assert row.terminal_event is None


def test_an_improve_session_with_a_finished_inner_eval_is_not_terminal(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 3, MUST rule 20: ``eval.run.finished`` is the newest row and the cycle is still open.

    The window is sampled by an observer on the cycle's own bus, because it exists only while
    the cycle runs: once ``run_cycle`` returns, the newest row is ``improve.run.finished`` and
    the question cannot be asked. The last sample is that close, so the mid-cycle nulls are not
    a listing that never fills the field in.
    """
    seeded = seed_three_sessions(conn, tmp_harnessed_home)
    assert "observer.failed" not in {name for name, _, _ in seeded.mid_cycle}, seeded.mid_cycle

    inner_finished = [sample for sample in seeded.mid_cycle if sample[0] == "eval.run.finished"]
    assert len(inner_finished) >= 2, f"the cycle ran no inner eval: {seeded.mid_cycle}"
    for name, newest, terminal in inner_finished:
        assert newest == name, "the finish the observer saw is not the newest row"
        assert terminal is None, f"the listing called the cycle over on {newest}"

    still_open = [sample for sample in seeded.mid_cycle if sample[0] != "improve.run.finished"]
    assert all(terminal is None for _, _, terminal in still_open)
    closed = seeded.mid_cycle[-1]
    assert closed[0] == "improve.run.finished", seeded.mid_cycle
    assert closed[2] == "improve.run.finished", "the listing never closes the cycle"
    assert (
        terminal_in(read.sessions(conn, DEFAULT_LISTING_LIMIT), seeded.improved)
        == "improve.run.finished"
    )

    EVIDENCE_097.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE_097 / "sessions.txt").open("a", encoding="utf-8") as handle:
        handle.write(
            "mid-cycle listing of the improve session (MUST rule 20):\n"
            + "".join(
                f"  published {name:<24} newest {newest:<24} terminal_event {terminal!r}\n"
                for name, newest, terminal in seeded.mid_cycle
            )
            + "\n"
        )


def test_the_listing_is_newest_first_then_session_id_descending_and_cut_at_limit(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 4: the order decision 36 spells, ties included, and ``?limit=`` cutting it."""
    older = plant_sessions(conn, 3)
    tick(1)
    newer = plant_sessions(conn, 2)
    expected = sorted(newer, reverse=True) + sorted(older, reverse=True)

    assert [row.session_id for row in read.sessions(conn, 20)] == expected
    assert [row.session_id for row in read.sessions(conn, 2)] == expected[:2]
    assert len({row.first_ts for row in read.sessions(conn, 20)}) == 2, "the tie is not a tie"

    with client_over(tmp_harnessed_home) as client:
        wire = client.get(LISTING_ROUTE).json()["sessions"]
        cut = client.get(f"{LISTING_ROUTE}?limit=4").json()["sessions"]
    assert [row["session_id"] for row in wire] == expected
    assert [row["session_id"] for row in cut] == expected[:4]


def test_the_route_defaults_to_twenty_rows(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 4's default: more sessions than the default, and the answer is the default's worth."""
    planted = plant_sessions(conn, DEFAULT_LISTING_LIMIT + 5)
    assert len(planted) == 25
    with client_over(tmp_harnessed_home) as client:
        listed = client.get(LISTING_ROUTE).json()["sessions"]
    assert len(listed) == DEFAULT_LISTING_LIMIT
    assert len(read.sessions(conn, 25)) == 25


@pytest.mark.parametrize("bad", ["abc", "0", "-1"])
def test_a_bad_limit_on_the_listing_is_400_in_the_decision_24_body(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, bad: str
) -> None:
    """AC 5, first half: the existing ``LimitError`` contract, never FastAPI's own shape."""
    plant_sessions(conn, 1)
    with client_over(tmp_harnessed_home) as client:
        response = client.get(f"{LISTING_ROUTE}?limit={bad}")
    assert response.status_code == 400, response.text
    body = response.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == LimitError.__name__
    assert repr(bad) in body["message"]
    EVIDENCE_097.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE_097 / "sessions.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"http GET {LISTING_ROUTE}?limit={bad}\n  400 {response.content.decode()}\n")


def test_an_empty_events_table_is_an_empty_collection_at_200(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 5, second half: nothing happened yet is ``[]`` and 200, never a 404 and never a null."""
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert read.sessions(conn, DEFAULT_LISTING_LIMIT) == ()
    with client_over(tmp_harnessed_home) as client:
        response = client.get(LISTING_ROUTE)
    assert response.status_code == 200, response.text
    assert response.content == canonical({"sessions": []}).encode("utf-8")
    EVIDENCE_097.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE_097 / "sessions.txt").open("a", encoding="utf-8") as handle:
        handle.write(
            f"http GET {LISTING_ROUTE} over an empty events table\n"
            f"  {response.status_code} {response.content.decode()}\n"
        )


def test_the_cli_twin_prints_the_body_the_route_serves(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 6 in this file: one emitter, so the CLI line and the body are the same bytes.

    The parity table in ``tests/test_gateway_parity.py`` holds the pair; this is the same claim
    over the seeded three-session home, and it is where the evidence artefact records it.
    """
    seeded = seed_three_sessions(conn, tmp_harnessed_home)
    result = cli.invoke(app, ["sessions", "list", "--json"])
    assert result.exit_code == 0, result.output
    stdout = result.stdout.encode("utf-8")
    assert stdout.endswith(b"\n") and b"\n" not in stdout[:-1], stdout

    with client_over(tmp_harnessed_home) as client:
        response = client.get(LISTING_ROUTE)
    assert response.status_code == 200, response.text
    assert stdout[:-1] == response.content
    assert len(response.json()["sessions"]) == 3

    prose = cli.invoke(app, ["sessions", "list"])
    assert prose.exit_code == 0, prose.output
    for session_id in seeded.sessions:
        assert session_id in prose.output

    EVIDENCE_097.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE_097 / "sessions.txt").open("a", encoding="utf-8") as handle:
        handle.write(
            "\ncli  harnessed sessions list --json\n"
            f"  {stdout[:-1].decode()}\n"
            f"http GET {LISTING_ROUTE}\n"
            f"  {response.content.decode()}\n"
            "parity: equal as bytes\n\n"
            "cli  harnessed sessions list\n"
            + "".join(f"  {line}\n" for line in prose.output.splitlines())
        )


LISTING_SQL = (
    "SELECT e.run_id, e.name, e.created_at, d.task FROM events e "
    "LEFT JOIN decisions d ON d.run_id = e.run_id AND d.message_seq = 1 AND d.hop = 0 "
    "WHERE e.seq = 1 ORDER BY e.created_at DESC, e.run_id DESC LIMIT ?"
)


def test_the_listing_callables_are_exported_and_spell_decision_36s_query() -> None:
    """The three names the ticket adds are the module's surface, and the read is the one decided."""
    assert "sessions" in read.__all__
    assert "sessions_payload" in read.__all__
    assert "SessionSummary" in read.__all__
    assert set(read.__all__) <= set(vars(read)), "__all__ names something the module lacks"
    assert read._SELECT_SESSIONS == LISTING_SQL, "the listing query is not decision 36's, verbatim"


def test_sessions_payload_is_the_one_projection_and_neither_half_spells_a_row(
    conn: sqlite3.Connection,
) -> None:
    """MUST rule 16: neither half builds the row; both call ``read.sessions_payload``."""
    plant_sessions(conn, 2)
    rows = read.sessions(conn, DEFAULT_LISTING_LIMIT)
    assert read.sessions_payload(rows) == {"sessions": [dataclasses.asdict(row) for row in rows]}
    for path in (
        PACKAGE_ROOT / "cli" / "sessions_cmd.py",
        PACKAGE_ROOT / "gateway" / "routes" / "events.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "sessions_payload" in source, f"{path.name} does not call read.sessions_payload"
        spelled = {
            field.name
            for field in dataclasses.fields(read.SessionSummary)
            if '"' + field.name + '":' in source
        }
        assert not spelled, f"{path.name} builds a session row by hand: {sorted(spelled)}"


# ===============================================================================================
# ticket 128 (FR-69): GET /v1/events/gate, the gate split with failed_open as its own bucket
# ===============================================================================================
#
# Decision 48: ``retrieval.gate.decided`` carries ``retrieve`` and ``failed_open`` as payload
# booleans, so counting the three buckets means ``json_extract`` in the query. That is allowed in
# ``events/read.py``, which owns the table, and forbidden in ``gateway/`` (MUST rule 14 bans SQL
# there, not JSON in a store read). ``failed_open`` is never folded into ``retrieve``: a gate
# that failed open did retrieve, for a different reason, and merging them is a figure that reads
# as confirmation while the thing it confirms is not true. Decision 47: the read at 50,000 gate
# rows measured 208.2 ms without an index and 64.1 ms with migration 009's ``events_gate_idx``.

EVIDENCE_128 = REPO_ROOT / "evidence" / "128"
GATE_ROUTE = "/v1/events/gate"
GATE_ARGV = ["events", "gate", "--json"]
GATE_KEYS_ON_THE_WIRE = ("skip", "retrieve", "failed_open", "decided")
GATEWAY_DIR = PACKAGE_ROOT / "gateway"
SMALL_MODEL = "fake-small"
TASK = "deploy the harness offline"

#: Decision 47's ceiling for this read and the two figures it measured there.
CEILING_GATE_ROWS = 50_000
CEILING_OTHER_ROWS = 50_000
DECISION_47_NO_INDEX_MS = 208.2
DECISION_47_INDEXED_MS = 64.1
NFR_SECONDS = 0.200


def capture_128(name: str, text: str) -> Path:
    """Write ticket 128's evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_128.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_128 / name
    path.write_text(text, encoding="utf-8")
    return path


def gate_answering(answer: str) -> CountingProvider:
    """A gate provider that always says ``answer``."""
    return CountingProvider(
        script=[ChatResponse(response_id="resp_gate", model=SMALL_MODEL, content=answer)]
    )


def decide_through_the_real_gate(conn: sqlite3.Connection, answer: str) -> gate.Decision:
    """One gate decision, published onto a fresh session's bus by ``memory/gate.py`` itself.

    Seeding through the real gate rather than a hand-built payload is the point: the rows the
    read counts are the rows the gate writes, keys and all, so the count cannot pass against a
    payload shape the gate never publishes.
    """
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    return gate.decide(gate_answering(answer), TASK, small_model=SMALL_MODEL, publish=bus.publish)


def served_split(home: Path) -> tuple[dict[str, Any], bytes]:
    with client_over(home) as client:
        response = client.get(GATE_ROUTE)
    assert response.status_code == 200, response.text
    return response.json(), response.content


def as_ints(payload: dict[str, Any]) -> dict[str, int]:
    return {key: int(payload[key]) for key in GATE_KEYS_ON_THE_WIRE}


def test_the_gate_route_serves_the_four_buckets_as_strings_with_decided_the_served_sum(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 1: ``{"skip","retrieve","failed_open","decided"}``, every figure a string, and
    ``decided`` the sum of the three as its own served field."""
    for answer in ("no", "yes", "no", "maybe", "yes", "no"):
        decide_through_the_real_gate(conn, answer)
    payload, body = served_split(tmp_harnessed_home)
    assert set(payload) == set(GATE_KEYS_ON_THE_WIRE)
    for key in GATE_KEYS_ON_THE_WIRE:
        assert isinstance(payload[key], str), (key, payload[key])
        assert payload[key].isdigit(), (key, payload[key])
    counts = as_ints(payload)
    assert counts == {"skip": 3, "retrieve": 2, "failed_open": 1, "decided": 6}
    assert counts["decided"] == counts["skip"] + counts["retrieve"] + counts["failed_open"]
    assert b'"decided":"6"' in body, body
    capture_128(
        "gate-split.txt",
        "ticket 128 - GET /v1/events/gate over six real gate decisions "
        "(no, yes, no, <unparseable>, yes, no)\n"
        f"{body.decode()}\n"
        + "\n".join(f"{key}: {type(payload[key]).__name__} {payload[key]!r}" for key in payload)
        + "\n",
    )


def test_a_failed_open_decision_raises_failed_open_by_one_and_retrieve_by_zero(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 2, first half: ``failed_open`` is its own bucket and is never folded into ``retrieve``,
    although a gate that failed open did retrieve."""
    decide_through_the_real_gate(conn, "yes")
    before = as_ints(served_split(tmp_harnessed_home)[0])
    decision = decide_through_the_real_gate(conn, "maybe")
    assert decision.retrieve is True and decision.failed_open is True, decision
    after = as_ints(served_split(tmp_harnessed_home)[0])
    assert after["failed_open"] - before["failed_open"] == 1
    assert after["retrieve"] - before["retrieve"] == 0
    assert after["skip"] - before["skip"] == 0
    assert after["decided"] - before["decided"] == 1
    capture_128(
        "deltas.txt",
        "ticket 128 - one seeded failed-open decision (the gate answered 'maybe')\n"
        f"before: {before}\nafter:  {after}\n"
        "failed_open +1, retrieve +0, skip +0, decided +1\n",
    )


def test_an_ordinary_retrieve_raises_retrieve_by_one_and_failed_open_by_zero(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 2, second half: an ordinary retrieve moves ``retrieve`` and nothing else."""
    decide_through_the_real_gate(conn, "maybe")
    before = as_ints(served_split(tmp_harnessed_home)[0])
    decision = decide_through_the_real_gate(conn, "yes")
    assert decision.retrieve is True and decision.failed_open is False, decision
    after = as_ints(served_split(tmp_harnessed_home)[0])
    assert after["retrieve"] - before["retrieve"] == 1
    assert after["failed_open"] - before["failed_open"] == 0
    assert after["skip"] - before["skip"] == 0
    assert after["decided"] - before["decided"] == 1
    EVIDENCE_128.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE_128 / "deltas.txt").open("a", encoding="utf-8") as handle:
        handle.write(
            "\none seeded ordinary retrieve (the gate answered 'yes')\n"
            f"before: {before}\nafter:  {after}\n"
            "retrieve +1, failed_open +0, skip +0, decided +1\n"
        )


def test_a_skip_raises_skip_by_one_and_neither_retrieve_bucket(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    before = as_ints(served_split(tmp_harnessed_home)[0])
    decision = decide_through_the_real_gate(conn, "no")
    assert decision.retrieve is False and decision.failed_open is False, decision
    after = as_ints(served_split(tmp_harnessed_home)[0])
    assert after["skip"] - before["skip"] == 1
    assert after["retrieve"] - before["retrieve"] == 0
    assert after["failed_open"] - before["failed_open"] == 0


def test_the_failed_open_event_itself_is_not_a_decision_and_is_not_counted(
    conn: sqlite3.Connection,
) -> None:
    """The gate publishes ``retrieval.gate.failed_open`` and then ``retrieval.gate.decided`` for
    one failed-open decision. The split counts decisions: one, not two."""
    decide_through_the_real_gate(conn, "maybe")
    names = [str(row[0]) for row in conn.execute("SELECT name FROM events ORDER BY run_id, seq")]
    assert names.count(gate.FAILED_OPEN) == 1 and names.count(gate.DECIDED) == 1
    found = read.gate_split(conn)
    assert (found.skip, found.retrieve, found.failed_open, found.decided) == (0, 0, 1, 1)


def test_an_empty_events_table_serves_zero_for_all_four_never_a_blank(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 3: all four read ``"0"`` and the tile says so, never a blank."""
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    payload, body = served_split(tmp_harnessed_home)
    assert payload == {key: "0" for key in GATE_KEYS_ON_THE_WIRE}
    assert "null" not in body.decode() and '""' not in body.decode()
    prose = cli.invoke(app, ["events", "gate"])
    assert prose.exit_code == 0, prose.output
    for label in ("skip", "retrieve", "failed open", "decided"):
        line = next(ln for ln in prose.output.splitlines() if ln.strip().startswith(label))
        assert " 0" in line, line
    capture_128(
        "empty-table.txt",
        "ticket 128 - the empty events table\n"
        f"GET {GATE_ROUTE}\n{body.decode()}\n\nharnessed events gate\n"
        + "".join(f"  {line}\n" for line in prose.output.splitlines()),
    )


def test_the_read_and_its_emitter_are_the_one_projection(conn: sqlite3.Connection) -> None:
    """Decision 41: one store read with its own emitter, every figure a string (MUST rule 29)."""
    for answer in ("yes", "maybe", "no", "no"):
        decide_through_the_real_gate(conn, answer)
    found = read.gate_split(conn)
    assert isinstance(found, read.GateSplit)
    assert (found.skip, found.retrieve, found.failed_open) == (2, 1, 1)
    assert found.decided == 4
    payload = read.gate_split_payload(found)
    assert list(payload) == list(GATE_KEYS_ON_THE_WIRE), "the emitter's own order"
    assert payload == {"skip": "2", "retrieve": "1", "failed_open": "1", "decided": "4"}
    for name in ("GateSplit", "gate_split", "gate_split_payload", "GATE_DECIDED", "GATE_KEYS"):
        assert name in read.__all__, name


def test_the_gateway_directory_contains_zero_sql_and_the_json_extract_lives_in_the_read() -> None:
    """AC 4: ``gateway/`` holds 0 SQL; ``json_extract`` is spelled in ``events/read.py`` only.

    A raw scan over every string constant under ``gateway/``, keyed on upper-case statement
    shape and on the JSON function by name, so a docstring saying "404 from the store" does not
    trip it and a handler that smuggled the query in would.
    """
    offenders: list[str] = []
    for path in sorted(GATEWAY_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            statement = ("SELECT " in value and " FROM " in value) or value.lstrip().startswith(
                ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE ", "PRAGMA ")
            )
            if statement or "json_extract" in value.lower():
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {value[:60]!r}")
    assert offenders == [], offenders
    read_source = (PACKAGE_ROOT / "events" / "read.py").read_text(encoding="utf-8")
    assert "json_extract" in read_source
    assert "json_extract" in read._SELECT_GATE_SPLIT
    assert f"name = '{read.GATE_DECIDED}'" in read._SELECT_GATE_SPLIT, (
        "the name is a literal in the query, which is what lets SQLite prove the partial "
        "index events_gate_idx applies; a bound parameter cannot"
    )


def test_the_published_payload_keys_match_the_keys_the_read_extracts() -> None:
    """AC 5, decision 48's guard: ``memory/gate.py`` publishes ``retrieve`` and ``failed_open``
    in one place and the read extracts the same two names, so a rename fails here rather than
    silently zeroing a bucket. ``memory/gate.py`` is read, never changed."""
    published: list[tuple[str, dict[str, object]]] = []

    def record(name: str, payload: Any) -> None:
        published.append((name, dict(payload)))

    for answer in ("yes", "no", "maybe"):
        gate.decide(gate_answering(answer), TASK, small_model=SMALL_MODEL, publish=record)
    decided = [payload for name, payload in published if name == gate.DECIDED]
    assert len(decided) == 3
    assert read.GATE_DECIDED == gate.DECIDED
    assert read.GATE_KEYS == ("retrieve", "failed_open")
    for payload in decided:
        assert set(read.GATE_KEYS) <= set(payload), payload
        for key in read.GATE_KEYS:
            assert isinstance(payload[key], bool), (key, payload[key])
    for key in read.GATE_KEYS:
        assert f"'$.{key}'" in read._SELECT_GATE_SPLIT, key
    assert read._SELECT_GATE_SPLIT.count("'$.") == read._SELECT_GATE_SPLIT.count(
        "json_extract("
    ), "every json_extract in the read names one of GATE_KEYS"
    extracted = {part.split("'")[0] for part in read._SELECT_GATE_SPLIT.split("'$.")[1:]}
    assert extracted == set(read.GATE_KEYS), extracted


def test_the_gate_handler_calls_the_read_and_its_emitter_and_nothing_else() -> None:
    """Decision 38 over the new handler: ``read.gate_split`` then ``read.gate_split_payload``,
    no arithmetic and no third call. ``HANDLER_CALLS`` above already pins the module's set."""
    handler = next(
        node
        for node in ast.walk(route_module())
        if isinstance(node, ast.FunctionDef) and node.name == "gate_split"
    )
    calls = [node for stmt in handler.body for node in ast.walk(stmt) if isinstance(node, ast.Call)]
    assert [ast.unparse(call.func) for call in calls] == [
        "read.gate_split_payload",
        "read.gate_split",
    ]
    assert not any(isinstance(node, ast.BinOp) for stmt in handler.body for node in ast.walk(stmt))


def test_the_cli_twin_prints_the_gate_split_the_route_serves(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 7: ``harnessed events gate --json`` is byte-identical to the route and the pair is in
    ``PARITY_SET`` (asserted in ``tests/test_gateway_parity.py``); this is the same claim over a
    seeded home, where the evidence records it. The prose form carries FR-69's gloss."""
    for answer in ("yes", "no", "maybe", "yes"):
        decide_through_the_real_gate(conn, answer)
    result = cli.invoke(app, GATE_ARGV)
    assert result.exit_code == 0, result.output
    stdout = result.stdout.encode("utf-8")
    assert stdout.endswith(b"\n") and b"\n" not in stdout[:-1], stdout
    payload, body = served_split(tmp_harnessed_home)
    assert stdout[:-1] == body
    assert as_ints(payload) == {"skip": 1, "retrieve": 2, "failed_open": 1, "decided": 4}

    prose = cli.invoke(app, ["events", "gate"])
    assert prose.exit_code == 0, prose.output
    assert "the gate could not answer and memory was retrieved anyway" in prose.output
    assert "%" not in prose.output, "three figures and a gloss, never a percentage"
    capture_128(
        "parity-diff.txt",
        f"cli  harnessed {' '.join(GATE_ARGV)}\n  {stdout[:-1].decode()}\n"
        f"http GET {GATE_ROUTE}\n  {body.decode()}\nparity: equal as bytes\n\n"
        "cli  harnessed events gate\n"
        + "".join(f"  {line}\n" for line in prose.output.splitlines()),
    )


def seed_gate_ceiling(conn: sqlite3.Connection) -> None:
    """Decision 47's ceiling: 50,000 gate decisions among as many other events.

    Rows are inserted directly because 100,000 publishes through the bus, one transaction
    each, would make the seed the slow part; the payloads are the shapes ``memory/gate.py``
    publishes, taken from one real decision of each kind so the seed cannot drift from them.
    """
    shapes: dict[str, str] = {}
    captured: list[tuple[str, dict[str, object]]] = []
    for answer in ("no", "yes", "maybe"):
        gate.decide(
            gate_answering(answer),
            TASK,
            small_model=SMALL_MODEL,
            publish=lambda name, payload: captured.append((name, dict(payload))),
        )
    for name, payload in captured:
        if name != gate.DECIDED:
            continue
        if payload["failed_open"]:
            bucket = "failed_open"
        elif payload["retrieve"]:
            bucket = "retrieve"
        else:
            bucket = "skip"
        shapes[bucket] = canonical(payload)
    assert set(shapes) == {"skip", "retrieve", "failed_open"}
    rows: list[tuple[str, int, str, str, str]] = []
    now = clock.isoformat(clock.now())
    for index in range(CEILING_GATE_ROWS):
        session_id = f"run_ceiling{index:020d}"
        bucket = ("skip", "retrieve", "retrieve", "failed_open")[index % 4]
        rows.append((session_id, 1, "run.started", '{"provider":"fake"}', now))
        rows.append((session_id, 2, gate.DECIDED, shapes[bucket], now))
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO events (run_id, seq, name, payload, created_at) VALUES (?, ?, ?, ?, ?)", rows
    )
    conn.execute("COMMIT")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == (
        CEILING_GATE_ROWS + CEILING_OTHER_ROWS
    )


def test_the_gate_split_at_the_ceiling_is_inside_the_nfr_through_the_partial_index(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """Decision 47 re-measured: the read over 50,000 gate rows, five reads each through a fresh
    ``connect_readonly``, recorded beside the 208.2 ms and 64.1 ms it measured. Only the 200 ms
    NFR is asserted; a median over 150 ms is the trigger to reopen the decision."""
    seed_gate_ceiling(conn)
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    samples: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        readonly = db.connect_readonly(path)
        try:
            found = read.gate_split(readonly)
        finally:
            readonly.close()
        samples.append(time.perf_counter() - started)
    median = statistics.median(samples)
    quarter = CEILING_GATE_ROWS // 4
    assert (found.skip, found.retrieve, found.failed_open, found.decided) == (
        quarter,
        2 * quarter,
        quarter,
        CEILING_GATE_ROWS,
    )
    plan = "\n".join(
        str(row[-1])
        for row in conn.execute(f"EXPLAIN QUERY PLAN {read._SELECT_GATE_SPLIT}").fetchall()
    )
    assert "events_gate_idx" in plan, plan
    capture_128(
        "ceiling-timing.txt",
        f"ticket 128 - read.gate_split over {CEILING_GATE_ROWS} gate rows among "
        f"{CEILING_GATE_ROWS + CEILING_OTHER_ROWS} events, five reads each through a fresh "
        "connect_readonly\n"
        + "\n".join(f"read {n + 1}: {seconds * 1000:.1f} ms" for n, seconds in enumerate(samples))
        + f"\nmedian: {median * 1000:.1f} ms\n"
        f"decision 47 measured: {DECISION_47_NO_INDEX_MS} ms without an index, "
        f"{DECISION_47_INDEXED_MS} ms with migration 009\n"
        f"NFR: {NFR_SECONDS * 1000:.0f} ms; reopen trigger: 150 ms\nplan:\n{plan}\n",
    )
    assert median < NFR_SECONDS, f"median {median * 1000:.1f} ms is over the 200 ms NFR"


# ===============================================================================================
# ticket 129 (FR-70): GET /v1/events/totals, every event and every session, all time
# ===============================================================================================
#
# Decision 47: served all time and counted now. ``sessions`` is the ``seq = 1`` rows, the
# definition FR-48 already uses for the listing, so the tile and the listing cannot disagree on
# what a session is. The literal ``1`` in the query is what lets SQLite prove migration 009's
# ``events_first_idx`` applies, as the gate split's literal name does for ``events_gate_idx``.

EVIDENCE_129 = REPO_ROOT / "evidence" / "129"
TOTALS_ROUTE = "/v1/events/totals"
TOTALS_ARGV = ["events", "totals", "--json"]
TOTALS_COUNTS = ("events", "sessions")

#: Decision 47's seeded ceiling for this read and the two figures it measured there.
CEILING_EVENTS = 500_000
CEILING_EVENTS_PER_SESSION = 10
DECISION_47_TOTALS_NO_INDEX_MS = 72.9
DECISION_47_TOTALS_INDEXED_MS = 47.0


def capture_129(name: str, text: str) -> Path:
    """Write ticket 129's evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_129.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_129 / name
    path.write_text(text, encoding="utf-8")
    return path


def seed_sessions(conn: sqlite3.Connection, *lengths: int) -> None:
    """One session per length, each publishing that many events through the real bus."""
    for length in lengths:
        bus = EventBus(conn=conn)
        bus.publish("run.started", {"provider": "fake"})
        for _ in range(length - 1):
            bus.publish("run.finished", {"stop_reason": "done"})


def served_totals(home: Path) -> tuple[dict[str, Any], bytes]:
    with client_over(home) as client:
        response = client.get(TOTALS_ROUTE)
    assert response.status_code == 200, response.text
    return response.json(), response.content


def test_the_totals_route_serves_events_sessions_and_counted_at_as_strings(
    frozen_clock: object, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 1: ``{"events","sessions","counted_at"}``, ``sessions`` the ``seq = 1`` rows, every
    figure a string."""
    seed_sessions(conn, 3, 1, 2)
    payload, body = served_totals(tmp_harnessed_home)
    assert set(payload) == {"events", "sessions", "counted_at"}
    for key in payload:
        assert isinstance(payload[key], str), (key, payload[key])
    assert payload["events"] == "6"
    assert payload["sessions"] == "3"
    assert payload["sessions"] == str(len(read.sessions(conn, 1000)))
    assert payload["counted_at"] == clock.isoformat(clock.now())
    capture_129(
        "events-totals.txt",
        "ticket 129 - GET /v1/events/totals over three sessions of 3, 1 and 2 events\n"
        f"{body.decode()}\n"
        + "\n".join(f"{key}: {type(payload[key]).__name__} {payload[key]!r}" for key in payload)
        + "\n",
    )


def test_the_totals_read_and_its_emitter_are_the_one_projection(
    frozen_clock: object, conn: sqlite3.Connection
) -> None:
    seed_sessions(conn, 4, 2)
    found = read.totals(conn)
    assert isinstance(found, read.EventTotals)
    assert (found.events, found.sessions) == (6, 2)
    assert found.counted_at == clock.isoformat(clock.now())
    payload = read.totals_payload(found)
    assert list(payload) == ["events", "sessions", "counted_at"], "the emitter's own order"
    assert payload == {"events": "6", "sessions": "2", "counted_at": found.counted_at}
    for name in ("EventTotals", "totals", "totals_payload"):
        assert name in read.__all__, name
    assert "WHERE seq = 1" in read._COUNT_EVENTS, "the literal FR-48's listing already uses"


def test_an_empty_events_table_totals_to_zero_never_null(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 3: an empty table is ``"0"`` for both counts, never ``null``."""
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    payload, body = served_totals(tmp_harnessed_home)
    assert {key: payload[key] for key in TOTALS_COUNTS} == {"events": "0", "sessions": "0"}
    assert "null" not in body.decode()
    prose = cli.invoke(app, ["events", "totals"])
    assert prose.exit_code == 0, prose.output
    for label in TOTALS_COUNTS:
        line = next(ln for ln in prose.output.splitlines() if ln.strip().startswith(label))
        assert line.split()[-1] == "0", line
    capture_129(
        "events-empty-table.txt",
        f"ticket 129 - the empty events table\nGET {TOTALS_ROUTE}\n{body.decode()}\n\n"
        "harnessed events totals\n" + "".join(f"  {ln}\n" for ln in prose.output.splitlines()),
    )


def test_the_totals_handler_calls_the_read_and_its_emitter_and_nothing_else() -> None:
    """AC 4: one callable from one package plus its emitter, 0 arithmetic, 0 SQL."""
    handler = next(
        node
        for node in ast.walk(route_module())
        if isinstance(node, ast.FunctionDef) and node.name == "totals"
    )
    calls = [node for stmt in handler.body for node in ast.walk(stmt) if isinstance(node, ast.Call)]
    assert [ast.unparse(call.func) for call in calls] == ["read.totals_payload", "read.totals"]
    body = [node for stmt in handler.body for node in ast.walk(stmt)]
    assert not any(isinstance(node, ast.BinOp | ast.AugAssign) for node in body)
    strings = [n.value for n in body if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("SELECT" in value or "FROM " in value for value in strings), strings


def test_the_cli_twin_prints_the_totals_the_route_serves(
    clean_env: None, frozen_clock: object, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 5: ``harnessed events totals --json`` is the route's bytes; ``PARITY_SET`` holds the
    pair (asserted in ``tests/test_gateway_parity.py``)."""
    seed_sessions(conn, 2, 5)
    result = cli.invoke(app, TOTALS_ARGV)
    assert result.exit_code == 0, result.output
    stdout = result.stdout.encode("utf-8")
    assert stdout.endswith(b"\n") and b"\n" not in stdout[:-1], stdout
    payload, body = served_totals(tmp_harnessed_home)
    assert stdout[:-1] == body
    assert (payload["events"], payload["sessions"]) == ("7", "2")
    prose = cli.invoke(app, ["events", "totals"])
    assert prose.exit_code == 0, prose.output
    assert "all time" in prose.output
    capture_129(
        "events-parity-diff.txt",
        f"cli  harnessed {' '.join(TOTALS_ARGV)}\n  {stdout[:-1].decode()}\n"
        f"http GET {TOTALS_ROUTE}\n  {body.decode()}\nparity: equal as bytes\n\n"
        "cli  harnessed events totals\n"
        + "".join(f"  {line}\n" for line in prose.output.splitlines()),
    )


def test_the_event_totals_at_the_ceiling_are_inside_the_nfr(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """Decision 47 re-measured: 500,000 events, five reads each through a fresh
    ``connect_readonly``, recorded beside the 47 ms it measured with migration 009. Only the
    200 ms NFR is asserted; a median over 150 ms is the trigger to reopen the decision."""
    now = clock.isoformat(clock.now())
    sessions_seeded = CEILING_EVENTS // CEILING_EVENTS_PER_SESSION
    rows = (
        (
            f"run_ceiling{index // CEILING_EVENTS_PER_SESSION:020d}",
            index % CEILING_EVENTS_PER_SESSION + 1,
            "run.started" if index % CEILING_EVENTS_PER_SESSION == 0 else "run.finished",
            '{"provider":"fake"}',
            now,
        )
        for index in range(CEILING_EVENTS)
    )
    with conn:
        conn.executemany(
            "INSERT INTO events (run_id, seq, name, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    samples: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        readonly = db.connect_readonly(path)
        try:
            found = read.totals(readonly)
        finally:
            readonly.close()
        samples.append(time.perf_counter() - started)
    median = statistics.median(samples)
    assert (found.events, found.sessions) == (CEILING_EVENTS, sessions_seeded)
    plan = "\n".join(
        str(row[-1]) for row in conn.execute(f"EXPLAIN QUERY PLAN {read._COUNT_EVENTS}").fetchall()
    )
    capture_129(
        "events-ceiling-timing.txt",
        f"ticket 129 - read.totals over {CEILING_EVENTS} events in {sessions_seeded} sessions, "
        "five reads each through a fresh connect_readonly\n"
        + "\n".join(f"read {n + 1}: {seconds * 1000:.1f} ms" for n, seconds in enumerate(samples))
        + f"\nmedian: {median * 1000:.1f} ms\n"
        f"decision 47 measured: {DECISION_47_TOTALS_NO_INDEX_MS} ms without an index, "
        f"{DECISION_47_TOTALS_INDEXED_MS} ms with migration 009\n"
        f"NFR: {NFR_SECONDS * 1000:.0f} ms; reopen trigger: 150 ms\nplan:\n{plan}\n",
    )
    assert "events_first_idx" in plan, plan
    assert median < NFR_SECONDS, f"median {median * 1000:.1f} ms is over the 200 ms NFR"
