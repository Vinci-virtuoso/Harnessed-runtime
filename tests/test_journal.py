"""FR-7: the run journal, and the one thing it buys - a run that resumes instead of repeating.

The claim under test is arithmetic, not vibes. An iteration the journal already holds costs
**zero** provider calls and **zero** tool executions on the next run, and both numbers are read
off a spy rather than inferred from a transcript. Everything else here exists to make that number
trustworthy when the journal is not pristine: a hole punched in the middle of it, a run killed
between two turns, a second run sharing the same database.

Two structural assertions sit alongside the behavioural ones. The primary keys are the done-state
keys, so they are asserted against the live schema rather than trusted to the migration file; and
the malformed-arguments marker keeps the glossary spelling ``_arg_parse_error`` in the serialized
transcript while staying a column - never a magic key inside the arguments dict the model
controls, which is the distinction ``003_ledgers.sql`` was written around.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from harnessed.errors import StorageError
from harnessed.loop import run_loop
from harnessed.loop.journal import NO_JOURNAL, Journal, RecordedTurn, messages_for, record_exchange
from harnessed.memory import db
from harnessed.providers.translate import (
    ARG_PARSE_ERROR_KEY,
    journal_message,
    tool_call_payload,
)
from harnessed.providers.types import (
    ChatRequest,
    ChatResponse,
    Message,
    ToolCall,
    ToolResult,
)
from harnessed.tools import Tool, ToolRegistry
from support.spies import CountingProvider

RUN = "run_journal"
#: The provider name a bench journal records (FR-72). A writer that names none cannot be built.
PROVIDER = "fake"
OTHER_RUN = "run_other"

# Assembled from fragments: a literal that looks like a credential is what the repository's own
# write guard exists to reject, and a test for the ledger is not an exception to that rule.
SECRET = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDDEEEEFFFF"


class Interrupted(RuntimeError):
    """The run died mid-flight. Deliberately outside the ``HarnessError`` tree, like FR-1's guard.

    A resumable run is only interesting if it can be killed at a moment nobody chose, so the
    interrupt is raised from inside the provider spy rather than arranged by the loop.
    """


# ---- benches -----------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def journal(conn: sqlite3.Connection) -> Journal:
    """The journal under test, wired to the one function that owns the glossary spelling."""
    return Journal(conn, RUN, serialize_call=tool_call_payload, provider=PROVIDER)


@pytest.fixture
def side_effects() -> list[str]:
    """What the counting tool did. The list every exactly-once assertion is made against."""
    return []


@pytest.fixture
def registry(side_effects: list[str]) -> ToolRegistry:
    """One tool whose only job is to be observable: it appends and says so."""

    def touch(arguments: Mapping[str, Any]) -> str:
        side_effects.append(str(arguments.get("mark", "x")))
        return f"touched {len(side_effects)}"

    return ToolRegistry([Tool(name="touch", handler=touch, description="Leave a mark.")])


def answer(text: str = "done") -> ChatResponse:
    """A turn where the model is done talking to tools."""
    return ChatResponse(response_id="resp_answer", model="fake", content=text)


def asks_for(call_id: str = "call_1") -> ChatResponse:
    """A turn where the model asks for the one tool the bench registry declares."""
    return ChatResponse(
        response_id="resp_tool",
        model="fake",
        content="",
        tool_calls=(ToolCall(id=call_id, name="touch", arguments={"mark": call_id}),),
        stop_reason="tool_use",
    )


def request_for(text: str = "what is the answer?") -> ChatRequest:
    return ChatRequest(model="fake", messages=(Message(role="user", content=text),))


def prerecord(journal: Journal, iterations: tuple[int, ...]) -> None:
    """Put ``iterations`` on the journal as completed tool turns, as a crashed run would leave."""
    for iteration in iterations:
        call_id = f"call_{iteration}"
        journal.record(
            iteration,
            request_for(),
            asks_for(call_id),
            (ToolResult(call_id=call_id, content=f"touched {iteration}"),),
        )


def recorded_iterations(conn: sqlite3.Connection, run_id: str = RUN) -> tuple[int, ...]:
    """Every iteration the journal holds for ``run_id``, in order."""
    rows = conn.execute(
        "SELECT iteration FROM turns WHERE run_id = ? ORDER BY iteration", (run_id,)
    )
    return tuple(int(row[0]) for row in rows)


def primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    """The primary key columns of ``table``, in key order, read off the live schema."""
    columns = [row for row in conn.execute(f"PRAGMA table_info({table})") if row[5]]
    return [str(row[1]) for row in sorted(columns, key=lambda row: row[5])]


# ---- resuming costs nothing ---------------------------------------------------------------------


def test_a_recorded_iteration_costs_no_provider_call_and_work_starts_after_it(
    journal: Journal, registry: ToolRegistry, side_effects: list[str]
) -> None:
    """The headline claim: iterations 1 and 2 recorded, so the run starts at iteration 3."""
    prerecord(journal, (1, 2))
    model = CountingProvider(script=[answer()])
    result = run_loop(model, registry, request_for(), limit=10, journal=journal)
    assert model.calls == 1
    assert result.iterations == 3
    assert side_effects == []


def test_a_replayed_turn_reaches_the_model_as_transcript_rather_than_as_a_gap(
    journal: Journal, registry: ToolRegistry
) -> None:
    """Resuming is not amnesia: the model's first real turn sees the recorded ones behind it."""
    prerecord(journal, (1, 2))
    model = CountingProvider(script=[answer()])
    run_loop(model, registry, request_for(), limit=10, journal=journal)
    shown = model.seen[0]
    assert [message.role for message in shown.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert [call.id for message in shown.messages for call in message.tool_calls] == [
        "call_1",
        "call_2",
    ]
    assert [result.content for message in shown.messages for result in message.tool_results] == [
        "touched 1",
        "touched 2",
    ]


def test_an_unrecorded_iteration_is_a_miss_not_an_error(journal: Journal) -> None:
    """A miss is an answer. Every entry point tolerates a half-finished ledger."""
    assert journal.recorded(1) is None


def test_a_recorded_turn_reads_back_as_the_response_and_the_results_it_recorded(
    journal: Journal,
) -> None:
    prerecord(journal, (1,))
    turn = journal.recorded(1)
    assert isinstance(turn, RecordedTurn)
    assert turn.iteration == 1
    assert turn.response.stop_reason == "tool_use"
    assert [call.id for call in turn.response.tool_calls] == ["call_1"]
    assert [result.content for result in turn.results] == ["touched 1"]
    assert [result.is_error for result in turn.results] == [False]


# ---- exactly once, across an interrupt -----------------------------------------------------------


def die_on_the_second_turn(call: int) -> None:
    """Kill the run at the top of turn 2, after turn 1's work and its journal write."""
    if call == 2:
        raise Interrupted("the process went away between two turns")


def test_a_tool_side_effect_happens_exactly_once_across_an_interrupt_and_resume(
    journal: Journal, registry: ToolRegistry, side_effects: list[str]
) -> None:
    """The whole point of the ledger. The tool ran before the crash; it does not run again."""
    interrupted = CountingProvider(script=[asks_for()], probe=die_on_the_second_turn)
    with pytest.raises(Interrupted):
        run_loop(interrupted, registry, request_for(), limit=10, journal=journal)
    assert side_effects == ["call_1"]

    resumed = CountingProvider(script=[answer()])
    result = run_loop(resumed, registry, request_for(), limit=10, journal=journal)
    assert side_effects == ["call_1"]
    assert resumed.calls == 1
    assert result.iterations == 2


def test_the_interrupted_turn_is_on_disk_and_the_one_that_never_finished_is_not(
    journal: Journal, registry: ToolRegistry, conn: sqlite3.Connection
) -> None:
    """A crash at the top of turn 2 leaves turn 1 recorded, whole, and nothing else."""
    model = CountingProvider(script=[asks_for()], probe=die_on_the_second_turn)
    with pytest.raises(Interrupted):
        run_loop(model, registry, request_for(), limit=10, journal=journal)
    assert recorded_iterations(conn) == (1,)


# ---- the write lands before the next step --------------------------------------------------------


def test_every_write_is_persisted_before_the_next_step_begins(
    journal: Journal, registry: ToolRegistry, conn: sqlite3.Connection
) -> None:
    """Read at the top of turn N, the journal holds exactly 1..N-1. Never N, never less."""
    observed: list[tuple[int, ...]] = []
    model = CountingProvider(
        script=[asks_for("call_1"), asks_for("call_2"), answer()],
        probe=lambda call: observed.append(recorded_iterations(conn)),
    )
    run_loop(model, registry, request_for(), limit=10, journal=journal)
    assert observed == [(), (1,), (1, 2)]
    assert recorded_iterations(conn) == (1, 2, 3)


def test_the_last_turn_of_a_run_is_recorded_too(
    journal: Journal, registry: ToolRegistry, conn: sqlite3.Connection
) -> None:
    """The turn that trips guardrail 1 is a turn. A run that ends is a run fully on disk."""
    model = CountingProvider(script=[answer("final")])
    run_loop(model, registry, request_for(), limit=10, journal=journal)
    assert recorded_iterations(conn) == (1,)
    row = conn.execute(
        "SELECT response, stop_reason FROM turns WHERE run_id = ?", (RUN,)
    ).fetchone()
    assert row[0] == "final"
    assert row[1] == "end_turn"


# ---- a truncated journal is tolerated ------------------------------------------------------------


def test_a_truncated_journal_is_tolerated_without_duplicating_work(
    journal: Journal, registry: ToolRegistry, conn: sqlite3.Connection, side_effects: list[str]
) -> None:
    """A hole at iteration 2 stops the replay there: 1 is replayed, 3 is not read past the gap."""
    prerecord(journal, (1, 2, 3))
    conn.execute("DELETE FROM tool_results WHERE run_id = ? AND iteration = 2", (RUN,))
    conn.execute("DELETE FROM turns WHERE run_id = ? AND iteration = 2", (RUN,))

    model = CountingProvider(script=[answer()])
    result = run_loop(model, registry, request_for(), limit=10, journal=journal)
    assert model.calls == 1
    assert result.iterations == 2
    assert side_effects == []
    assert recorded_iterations(conn) == (1, 2, 3)


def test_a_turn_after_a_gap_is_not_replayed(journal: Journal) -> None:
    """Contiguity is the rule: a row whose predecessors are missing is not done-state."""
    prerecord(journal, (2, 3))
    assert journal.recorded(2) is None
    assert journal.recorded(3) is None


def test_a_failed_write_leaves_no_half_written_turn(
    journal: Journal, conn: sqlite3.Connection
) -> None:
    """The turn row and its tool rows land together or not at all, so truncation is row-level."""
    duplicated = (
        ToolResult(call_id="call_1", content="ok"),
        ToolResult(call_id="call_1", content="again"),
    )
    with pytest.raises(StorageError):
        journal.record(1, request_for(), two_calls_one_id(), duplicated)
    assert recorded_iterations(conn) == ()


def two_calls_one_id() -> ChatResponse:
    """A response that would write the same ``(run_id, iteration, call_id)`` twice."""
    call = ToolCall(id="call_1", name="touch", arguments={"mark": "a"})
    return ChatResponse(
        response_id="resp_dup",
        model="fake",
        tool_calls=(call, call),
        stop_reason="tool_use",
    )


# ---- the keys are the done-state keys ------------------------------------------------------------


def test_the_tables_are_keyed_by_run_iteration_and_call_id(conn: sqlite3.Connection) -> None:
    assert primary_key(conn, "turns") == ["run_id", "iteration"]
    assert primary_key(conn, "tool_results") == ["run_id", "iteration", "call_id"]


def test_recording_the_same_iteration_twice_does_no_second_write(
    journal: Journal, conn: sqlite3.Connection
) -> None:
    """Read the done-state, find it present, do no work. Not an upsert and not an error."""
    prerecord(journal, (1,))
    journal.record(1, request_for("second attempt"), answer("overwritten"), ())
    row = conn.execute(
        "SELECT request, response FROM turns WHERE run_id = ? AND iteration = 1", (RUN,)
    ).fetchone()
    assert "second attempt" not in row[0]
    assert row[1] != "overwritten"
    assert conn.execute("SELECT COUNT(*) FROM turns WHERE run_id = ?", (RUN,)).fetchone()[0] == 1


def test_two_runs_sharing_one_database_do_not_read_each_others_turns(
    journal: Journal, conn: sqlite3.Connection
) -> None:
    """The key carries the run id, so a second run in the same file starts clean."""
    prerecord(journal, (1, 2))
    other = Journal(conn, OTHER_RUN, serialize_call=tool_call_payload, provider=PROVIDER)
    assert other.recorded(1) is None
    assert recorded_iterations(conn, OTHER_RUN) == ()


# ---- the marker keeps the glossary spelling ------------------------------------------------------


def malformed_turn() -> tuple[ChatRequest, ChatResponse, tuple[ToolResult, ...]]:
    """One turn where the model's arguments never parsed, request transcript included."""
    call = ToolCall(id="call_bad", name="touch", arguments={}, parse_error="Expecting a comma")
    request = ChatRequest(
        model="fake",
        messages=(
            Message(role="user", content="go"),
            Message(role="assistant", tool_calls=(call,)),
        ),
    )
    response = ChatResponse(
        response_id="resp_bad", model="fake", tool_calls=(call,), stop_reason="tool_use"
    )
    return request, response, (ToolResult(call_id="call_bad", content="malformed", is_error=True),)


def test_a_recorded_parse_error_is_serialized_under_the_glossary_name(
    journal: Journal, conn: sqlite3.Connection
) -> None:
    """PRD glossary: the serialized name is ``_arg_parse_error``, verbatim, in the ledger."""
    journal.record(1, *malformed_turn())
    stored = conn.execute(
        "SELECT request FROM turns WHERE run_id = ? AND iteration = 1", (RUN,)
    ).fetchone()[0]
    assert ARG_PARSE_ERROR_KEY in stored
    assert "Expecting a comma" in stored


def test_the_marker_is_a_column_and_never_a_magic_key_inside_the_arguments(
    journal: Journal, conn: sqlite3.Connection
) -> None:
    """``003_ledgers.sql``'s rationale, asserted: the arguments dict stays the model's alone."""
    journal.record(1, *malformed_turn())
    arguments, marker, ok = conn.execute(
        "SELECT arguments, parse_error, ok FROM tool_results WHERE run_id = ? AND call_id = ?",
        (RUN, "call_bad"),
    ).fetchone()
    assert marker == "Expecting a comma"
    assert ARG_PARSE_ERROR_KEY not in arguments
    assert arguments == "{}"
    assert ok == 0


def test_a_well_formed_call_records_no_marker_at_all(
    journal: Journal, conn: sqlite3.Connection
) -> None:
    prerecord(journal, (1,))
    arguments, marker, ok = conn.execute(
        "SELECT arguments, parse_error, ok FROM tool_results WHERE run_id = ? AND call_id = ?",
        (RUN, "call_1"),
    ).fetchone()
    assert marker is None
    assert arguments == '{"mark":"call_1"}'
    assert ok == 1


# ---- redaction and the null journal --------------------------------------------------------------


def test_a_secret_in_the_transcript_never_reaches_the_ledger(
    journal: Journal, conn: sqlite3.Connection, clean_env: None
) -> None:
    """FR-13: the journal is a store write, so it routes through ``scrub()`` like every other."""
    journal.record(
        1,
        request_for(f"the key is {SECRET} please use it"),
        answer(f"I will use {SECRET}"),
        (),
    )
    request, response = conn.execute(
        "SELECT request, response FROM turns WHERE run_id = ? AND iteration = 1", (RUN,)
    ).fetchone()
    assert SECRET not in request
    assert SECRET not in response
    assert "[redacted:" in request
    assert "[redacted:" in response


def test_the_loop_runs_without_a_journal_and_records_nothing(
    registry: ToolRegistry, side_effects: list[str]
) -> None:
    """The ledger is opt-in at the seam: no journal, no database, same run (FR-4 unchanged)."""
    model = CountingProvider(script=[asks_for(), answer()])
    result = run_loop(model, registry, request_for(), limit=10)
    assert result.iterations == 2
    assert result.reply == "done"
    assert side_effects == ["call_1"]


def test_the_null_journal_is_a_miss_for_every_iteration() -> None:
    """The default seam answers "not recorded" forever, which is what makes it a no-op."""
    assert NO_JOURNAL.recorded(1) is None
    assert NO_JOURNAL.record(1, request_for(), answer(), ()) is None


# ---- FR-72: a turn records its provider and model (ticket 131) ---------------------------------

EVIDENCE_131 = Path(__file__).resolve().parents[1] / "evidence" / "131"


def provider_columns(conn: sqlite3.Connection, run_id: str = RUN) -> list[tuple[object, ...]]:
    rows = conn.execute(
        "SELECT iteration, provider, model FROM turns WHERE run_id = ? ORDER BY iteration",
        (run_id,),
    )
    return [tuple(row) for row in rows]


def test_record_writes_the_provider_name_and_the_requests_model_on_every_turn(
    journal: Journal, registry: ToolRegistry
) -> None:
    model = CountingProvider(script=[asks_for(), answer()])
    run_loop(model, registry, request_for(), limit=10, journal=journal)
    assert provider_columns(journal.conn) == [(1, "fake", "fake"), (2, "fake", "fake")]


def test_the_model_recorded_is_the_requests_not_the_replys(conn: sqlite3.Connection) -> None:
    """The request names the model a turn was asked of; that is the id a price is keyed on."""
    journal = Journal(conn, RUN, serialize_call=tool_call_payload, provider="openrouter")
    request = ChatRequest(model="vendor/model-a", messages=(Message(role="user", content="hi"),))
    reply = ChatResponse(response_id="r", model="vendor/model-a-2026", content="ok")
    journal.record(1, request, reply, ())
    assert provider_columns(conn) == [(1, "openrouter", "vendor/model-a")]


def test_a_journal_cannot_be_built_without_naming_its_provider(conn: sqlite3.Connection) -> None:
    """Every writer fills both columns from 010 on: there is no default to fall back to."""
    with pytest.raises(TypeError):
        Journal(conn, RUN, serialize_call=tool_call_payload)  # type: ignore[call-arg]


# ---- FR-108: a turn records the message's seq (ticket 161, migration 014, decision 61) ---------


def message_seq_columns(conn: sqlite3.Connection, run_id: str = RUN) -> list[tuple[object, ...]]:
    rows = conn.execute(
        "SELECT iteration, message_seq FROM turns WHERE run_id = ? ORDER BY iteration", (run_id,)
    )
    return [tuple(row) for row in rows]


def test_a_routed_messages_turns_all_carry_its_message_seq(
    conn: sqlite3.Connection, registry: ToolRegistry
) -> None:
    """AC: a Groq turn in a routed message carries the message's ``seq``, on every turn of it -
    a chain hop is 163's, but this message's own turns all answer to the one ``run.started``."""
    journal = Journal(conn, RUN, serialize_call=tool_call_payload, provider="fake", message_seq=7)
    model = CountingProvider(script=[asks_for(), answer()])
    run_loop(model, registry, request_for(), limit=10, journal=journal)
    assert message_seq_columns(conn) == [(1, 7), (2, 7)]


def test_a_turn_recorded_with_no_message_seq_reads_null(conn: sqlite3.Connection) -> None:
    """The default: an eval, an improve run, or ``HARNESSED_ROUTER=off`` never routes, and never
    names one - the same ``null`` a pre-014 turn reads (``tests/test_migrations.py``)."""
    journal = Journal(conn, RUN, serialize_call=tool_call_payload, provider="fake")
    journal.record(1, request_for(), answer(), ())
    assert message_seq_columns(conn) == [(1, None)]


def test_a_seeded_run_under_the_fake_provider_records_fake_and_its_model_on_every_turn(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    """AC: a real ``harnessed run`` under ``HARNESSED_PROVIDER=fake`` stores ``provider: "fake"``
    and the fake model id on every turn, and ``run --json`` carries both."""
    import json

    from typer.testing import CliRunner

    from harnessed.cli.app import app
    from harnessed.config import load
    from harnessed.providers import registry as provider_registry

    result = CliRunner(mix_stderr=False).invoke(app, ["run", "--json", "ping"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    fake_model = provider_registry.model_for(load())
    connection = db.connect()
    try:
        rows = provider_columns(connection, str(payload["run_key"]))
    finally:
        connection.close()
    assert rows, "the run journaled at least one turn"
    assert all(row[1:] == ("fake", fake_model) for row in rows), rows
    assert (payload["provider"], payload["model"]) == ("fake", fake_model)
    EVIDENCE_131.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_131 / "fake-run-turns.txt").write_text(
        "ticket 131 - harnessed run under HARNESSED_PROVIDER=fake, each turn's provider and model\n"
        + "\n".join(f"iteration {i}: provider={p} model={m}" for i, p, m in rows)
        + f"\nrun --json provider={payload['provider']} model={payload['model']}\n",
        encoding="utf-8",
    )


def test_run_payload_reads_null_provider_and_model_for_a_pre_010_turn(
    conn: sqlite3.Connection,
) -> None:
    """AC: a pre-migration row reads ``null`` for both, never a default provider."""
    from harnessed.loop import runs

    conn.execute(
        "INSERT INTO turns (run_id, iteration, request, response, stop_reason, created_at) "
        "VALUES ('run_old', 1, '{}', 'hi', 'end_turn', '2026-01-01T00:00:00Z')"
    )
    payload = runs.run_payload(conn, "run_old")
    assert payload["provider"] is None
    assert payload["model"] is None
    EVIDENCE_131.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_131 / "pre-migration-row.txt").write_text(
        "ticket 131 - a turn written before 010 reads back through run_payload\n"
        f"provider={payload['provider']!r} model={payload['model']!r}\n"
        "the dashboard renders both as `unknown` with \"recorded before migration 010\"\n",
        encoding="utf-8",
    )


# ---- FR-80: the run's messages are stored on the run (ticket 138) -------------------------------

EVIDENCE_138 = Path(__file__).resolve().parents[1] / "evidence" / "138"


def stored_messages(conn: sqlite3.Connection, run_id: str = RUN) -> list[tuple[object, ...]]:
    rows = conn.execute(
        "SELECT idx, role, content, tool_calls, tool_results FROM run_messages "
        "WHERE run_id = ? ORDER BY idx",
        (run_id,),
    )
    return [tuple(row) for row in rows]


def test_run_messages_is_keyed_by_run_id_and_idx(conn: sqlite3.Connection) -> None:
    assert primary_key(conn, "run_messages") == ["run_id", "idx"]


def test_a_run_stores_every_message_the_loop_carried_in_order(
    journal: Journal, registry: ToolRegistry
) -> None:
    """The user turn, the assistant's call, the tool batch, the answer: four rows, in order."""
    model = CountingProvider(script=[asks_for(), answer()])
    result = run_loop(model, registry, request_for(), limit=10, journal=journal)
    rows = stored_messages(journal.conn)
    assert [row[:2] for row in rows] == [
        (0, "user"),
        (1, "assistant"),
        (2, "tool"),
        (3, "assistant"),
    ]
    assert [row[1] for row in rows] == [message.role for message in result.messages]
    assert rows[0][3] is None and rows[0][4] is None, "no call and no result is NULL, not []"
    assert rows[1][3] is not None and "touch" in str(rows[1][3])
    assert rows[2][4] is not None and "touched 1" in str(rows[2][4])


def test_messages_for_reads_back_exactly_the_list_the_loop_carried(
    journal: Journal, registry: ToolRegistry
) -> None:
    """The one sanctioned reader: the stored list comes back as the ``Message``s it was."""
    model = CountingProvider(script=[asks_for(), answer()])
    result = run_loop(model, registry, request_for(), limit=10, journal=journal)
    assert messages_for(journal.conn, RUN, decode=journal_message) == result.messages


def test_a_parse_error_marker_survives_the_round_trip_as_the_typed_field(
    journal: Journal,
) -> None:
    """Serialized and decoded by ``translate``, never by ``loop/``: the typed field survives."""
    call = ToolCall(id="call_bad", name="touch", arguments={}, parse_error="not JSON")
    response = ChatResponse(
        response_id="r", model="fake", tool_calls=(call,), stop_reason="tool_use"
    )
    outcome = ToolResult("call_bad", "bad", is_error=True)
    journal.record(1, request_for(), response, (outcome,))
    messages = messages_for(journal.conn, RUN, decode=journal_message)
    assert messages[1].tool_calls == (call,)
    assert messages[2].tool_results == (outcome,)


def test_an_unknown_run_key_reads_an_empty_list(conn: sqlite3.Connection) -> None:
    assert messages_for(conn, "run_nobody", decode=journal_message) == ()


# ---- ticket 167 (FR-106): record_exchange, the agent route's one other writer of run_messages ---


def test_record_exchange_appends_exactly_one_user_and_one_assistant_row(
    conn: sqlite3.Connection,
) -> None:
    record_exchange(conn, RUN, "investigate the failing build", "done: the build is green again")
    rows = stored_messages(conn)
    assert [row[:3] for row in rows] == [
        (0, "user", "investigate the failing build"),
        (1, "assistant", "done: the build is green again"),
    ]
    assert rows[0][3] is None and rows[0][4] is None, "no call and no result is NULL, not []"
    assert rows[1][3] is None and rows[1][4] is None


def test_record_exchange_scrubs_both_messages(conn: sqlite3.Connection) -> None:
    record_exchange(conn, RUN, f"the key is {SECRET}", f"used {SECRET} to authenticate")
    rows = stored_messages(conn)
    assert SECRET not in str(rows[0][2])
    assert SECRET not in str(rows[1][2])


def test_record_exchange_keys_after_whatever_the_run_already_holds(
    journal: Journal, registry: ToolRegistry
) -> None:
    """A follow-up on a run the loop already wrote to resumes ``record_exchange`` right after it -
    the same ``idx`` continuity :meth:`Journal.record`'s own write keeps."""
    model = CountingProvider(script=[asks_for(), answer()])
    run_loop(model, registry, request_for(), limit=10, journal=journal)
    (stored_before,) = journal.conn.execute(
        "SELECT COUNT(*) FROM run_messages WHERE run_id = ?", (RUN,)
    ).fetchone()

    record_exchange(journal.conn, RUN, "one more thing", "handled")

    rows = stored_messages(journal.conn)
    assert [row[:3] for row in rows[-2:]] == [
        (int(stored_before), "user", "one more thing"),
        (int(stored_before) + 1, "assistant", "handled"),
    ]
    assert messages_for(journal.conn, RUN, decode=journal_message)[-2:] == (
        Message(role="user", content="one more thing"),
        Message(role="assistant", content="handled"),
    )


def test_the_message_rows_join_the_turns_one_write(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUST rules 6 and 7: one ``db.write`` tuple per turn carries the turn and its messages."""
    from harnessed.loop import journal as journal_module

    writes: list[tuple[str, ...]] = []
    real = journal_module.db.write

    def spy(connection: sqlite3.Connection, statements: Any) -> None:
        statements = tuple(statements)
        writes.append(tuple(sql.split("(")[0].strip() for sql, _ in statements))
        real(connection, statements)

    monkeypatch.setattr(journal_module.db, "write", spy)
    journal.record(1, request_for(), asks_for(), (ToolResult("call_1", "touched 1"),))
    assert len(writes) == 1, writes
    assert writes[0].count("INSERT INTO run_messages") == 3
    assert writes[0][0] == "INSERT INTO turns"
    EVIDENCE_138.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_138 / "one-write.txt").write_text(
        "ticket 138 - one db.write per recorded turn\n" + "\n".join(writes[0]) + "\n",
        encoding="utf-8",
    )


def test_a_failed_write_leaves_no_message_row_either(
    journal: Journal, conn: sqlite3.Connection
) -> None:
    """The messages ride the turn's transaction, so a refused turn leaves no message behind."""
    duplicated = (
        ToolResult(call_id="call_1", content="ok"),
        ToolResult(call_id="call_1", content="again"),
    )
    with pytest.raises(StorageError):
        journal.record(1, request_for(), two_calls_one_id(), duplicated)
    assert stored_messages(conn) == []


def test_a_crash_mid_run_leaves_a_message_list_that_matches_the_journal(
    journal: Journal, registry: ToolRegistry
) -> None:
    """Killed at the top of turn 3: two turns on disk, and the messages those two turns carried."""

    def probe(call: int) -> None:
        if call == 3:
            raise Interrupted("killed at the top of turn 3")

    model = CountingProvider(script=[asks_for("call_1"), asks_for("call_2"), answer()], probe=probe)
    with pytest.raises(Interrupted):
        run_loop(model, registry, request_for(), limit=10, journal=journal)
    assert recorded_iterations(journal.conn) == (1, 2)
    roles = [row[1] for row in stored_messages(journal.conn)]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]


def test_a_secret_in_a_message_is_stored_as_its_marker(
    journal: Journal, conn: sqlite3.Connection, clean_env: None
) -> None:
    journal.record(1, request_for(f"the key is {SECRET}"), answer(f"using {SECRET}"), ())
    text = "".join(str(cell) for row in stored_messages(conn) for cell in row)
    assert SECRET not in text
    assert "[redacted:" in text


def test_recording_a_turn_twice_writes_its_messages_once(
    journal: Journal, conn: sqlite3.Connection
) -> None:
    journal.record(1, request_for(), answer(), ())
    journal.record(1, request_for(), answer(), ())
    assert len(stored_messages(conn)) == 2


# ---- decision 52: the ceiling is per message, the journal key is absolute ------------------------


def test_start_iteration_offsets_the_journal_key_and_not_the_ceiling(
    journal: Journal, registry: ToolRegistry
) -> None:
    """A follow-up at ``start_iteration=2`` records turns 3 and 4 and may take ``limit`` turns."""
    prerecord(journal, (1, 2))
    model = CountingProvider(script=[asks_for("call_3"), answer()])
    result = run_loop(model, registry, request_for(), limit=2, journal=journal, start_iteration=2)
    assert model.calls == 2, "turns 3 and 4 are new work, not a replay of 1 and 2"
    assert recorded_iterations(journal.conn) == (1, 2, 3, 4)
    assert result.iterations == 2
    assert result.guardrail == "no_more_tool_calls"


def test_the_guardrail_payload_says_the_ceiling_is_per_message(
    journal: Journal, registry: ToolRegistry
) -> None:
    """``limit_reached(taken, limit)`` is unchanged; the payload names which ceiling fired."""
    from harnessed.events.bus import EventBus
    from support.spies import RecordingObserver

    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    prerecord(journal, (1,))
    model = CountingProvider(script=[asks_for("call_2"), asks_for("call_3")])
    result = run_loop(
        model, registry, request_for(), limit=2, bus=bus, journal=journal, start_iteration=1
    )
    assert result.guardrail == "iteration_limit"
    tripped = [event.payload for event in seen.events if event.name == "loop.guardrail.tripped"]
    assert tripped == [
        {
            "guardrail": "iteration_limit",
            "iterations": 2,
            "stop_reason": "tool_use",
            "scope": "message",
        }
    ]
    started = [e.payload["iteration"] for e in seen.events if e.name == "loop.iteration.started"]
    assert started == [2, 3], "the published iteration is the journal's key"
    EVIDENCE_138.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_138 / "guardrail-scope.txt").write_text(
        "ticket 138 - loop.guardrail.tripped payload at start_iteration=1, limit=2\n"
        f"{tripped[0]}\n",
        encoding="utf-8",
    )
