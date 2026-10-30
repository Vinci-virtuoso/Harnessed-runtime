"""The parity check can fail (FR-36, ticket 071), and the parity proof itself (FR-37, ticket 072).

This file is layered exactly as the four-command proof is (decision 23): the in-process primary
runs the CLI half through Typer's ``CliRunner`` and the HTTP half through Starlette's
``TestClient`` against ``create_app()``, over one ``tmp_path`` ``HARNESSED_HOME`` and one
database, and compares the two byte strings. Ticket 072 adds the parity table and the ``slow``
shell test. This ticket adds ``test_parity_can_fail`` first, because a parity test that cannot
fail is worth nothing (MUST rule 12) and the empty diff of UJ-5 means something only because this
test exists.

Three tamperings, three assertions, one test. The numeric one substitutes the gateway's read
callable with one whose stored ``score`` is off by ``1e-6``; the event one drops one event and,
separately, transposes two; the resumption one redelivers an already-sent ``seq``. Each must
break the assertion it targets and each must pass untampered in the same test, so neither branch
can rot alone. The seeding lives in ``tests/support/divergence.py`` and nowhere under ``src/``
(asserted by ``test_boundaries.py``): it substitutes and reorders in memory and never writes a
divergent row, because a divergence that survived the test would be a corrupted ledger.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import DOCUMENTED_ENV_VARS, OFFLINE_PROVIDER
from harnessed import clock, shipped
from harnessed.acp import outputs as acp_outputs
from harnessed.acp import pending as acp_pending
from harnessed.cli import run_cmd
from harnessed.cli.app import app, main
from harnessed.evals import runner
from harnessed.events import read
from harnessed.events.bus import EventBus
from harnessed.gateway import create_app
from harnessed.gateway.routes import evals as eval_routes
from harnessed.improve import steps
from harnessed.memory import db
from harnessed.providers import pricing
from harnessed.serialize import canonical
from support import divergence
from test_cli import shell
from test_events_read import (
    assert_contiguous_from_one,
    journal_guardrail,
    seed_eval_session,
    turns_under,
)
from test_gateway_sse import Frame, assert_strictly_ascending_without_duplicates, frames_of

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "071"
EVIDENCE_072 = REPO_ROOT / "evidence" / "072"

#: The seeded numeric divergence. Deliberately this small: a check that only catches large
#: differences has not been shown to catch the differences that matter.
DELTA = 1e-6

PROMPT = "ping"

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")

cli = CliRunner(mix_stderr=False)


def capture(name: str, text: str, directory: Path = EVIDENCE_DIR) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- benches -------------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


def client_over(home: Path) -> TestClient:
    return TestClient(create_app(home / db.DATABASE_FILENAME), raise_server_exceptions=False)


def cli_bytes(argv: list[str]) -> bytes:
    """The CLI half: one ``--json`` command, asserted to have written one line and nothing else."""
    result = cli.invoke(app, argv)
    assert result.exit_code == 0, f"{argv} exited {result.exit_code}\n{result.stderr}"
    assert result.stderr == "", f"{argv} wrote to stderr: {result.stderr}"
    return result.stdout.encode("utf-8")


# ---- the assertions the tamperings target -------------------------------------------------------


def assert_parity(stdout: bytes, response: Any) -> None:
    """FR-37's parity assertion: the CLI's line and the HTTP body are the same bytes.

    The one allowance is the line terminator. ``render.json_object`` prints canonical bytes and a
    newline, because a command's stdout is a line; ``CanonicalJSONResponse`` carries the same
    bytes with no newline, because a body is not. The newline is asserted to be there exactly
    once and then stripped, and nothing else is normalised: no whitespace, no key order, no
    float formatting. Normalising is how a byte-identity test quietly becomes a subset test.
    """
    assert stdout.endswith(b"\n"), f"--json did not end its one line: {stdout!r}"
    assert b"\n" not in stdout[:-1], f"--json wrote more than one line: {stdout!r}"
    assert (
        stdout[:-1] == response.content
    ), f"the CLI and the gateway disagree:\n  cli:  {stdout[:-1]!r}\n  http: {response.content!r}"


def assert_journal_agreement(
    conn: sqlite3.Connection, session_id: str, events: Sequence[read.ReplayedEvent]
) -> None:
    """FR-38's journal-agreement check, as ``test_events_read`` makes it on the run path.

    Three parts, composed from that file's own helpers rather than re-derived: one
    ``loop.iteration.finished`` per journaled turn, the terminal payload's guardrail equal to the
    one the journal implies, and the ``seq`` column contiguous from 1. The ledger comparisons run
    first so a dropped iteration fails on the journal, not only on the numbering.
    """
    assert events, "an empty replay proves nothing"
    assert events[-1].name in read.TERMINAL_NAMES, f"last event is {events[-1].name}"
    iterations_finished = sum(1 for event in events if event.name == "loop.iteration.finished")
    turns = turns_under(conn, session_id)
    assert turns > 0
    assert (
        iterations_finished == turns
    ), f"{iterations_finished} loop.iteration.finished events against {turns} journaled turns"
    assert events[-1].payload["guardrail"] == journal_guardrail(conn, session_id)
    assert_contiguous_from_one([event.seq for event in events])


def assert_exactly_once(frames: Sequence[Frame]) -> None:
    """FR-39's exactly-once assertion, as ``test_gateway_sse`` makes it on a resumed stream."""
    assert_strictly_ascending_without_duplicates([frame.id for frame in frames])


# ---- benches: one home, one database, both halves reading the same rows ------------------------


def seed_run_session(clean_env: None) -> tuple[sqlite3.Connection, str]:
    """One ``harnessed run`` with a journal: the path where session id and journal key agree."""
    session = run_cmd.open_session()
    finished = run_cmd.solve_with_memory(session, PROMPT)
    assert finished.run_id == session.bus.run_id
    return session.conn, finished.run_id


def index_of(events: Sequence[read.ReplayedEvent], name: str, occurrence: int = 0) -> int:
    positions = [index for index, event in enumerate(events) if event.name == name]
    assert len(positions) > occurrence, f"{name} occurs {len(positions)} times"
    return positions[occurrence]


# ---- FR-36: the proof that the proof can fail ----------------------------------------------------


def test_parity_can_fail(
    clean_env: None,
    conn: sqlite3.Connection,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every branch below fails on its assertion, never on an exception, and passes untampered."""
    # ---- the numeric side: a stored score off by 1e-6 on the gateway's read callable ----------
    evaluated = seed_eval_session(conn)
    eval_run_id = evaluated.eval_run_id
    argv = ["eval", "show", eval_run_id, "--json"]
    path = f"/v1/eval/runs/{eval_run_id}"
    stdout = cli_bytes(argv)

    diverged = divergence.diverged_scored_run(runner.scored_run, delta=DELTA)
    with monkeypatch.context() as patch:
        # The gateway's binding only: the routes module looks ``runner`` up in its own globals,
        # and the CLI half keeps the real module, so the substitution is exactly one side's.
        patch.setattr(
            eval_routes,
            "runner",
            SimpleNamespace(scored_run=diverged, json_payload=runner.json_payload),
        )
        with client_over(tmp_harnessed_home) as client:
            tampered = client.get(path)
    assert tampered.status_code == 200, tampered.text
    with pytest.raises(AssertionError) as numeric_failure:
        assert_parity(stdout, tampered)

    with client_over(tmp_harnessed_home) as client:
        untampered = client.get(path)
    assert untampered.status_code == 200, untampered.text
    assert_parity(stdout, untampered)

    honest = runner.scored_run(conn, eval_run_id)
    assert honest is not None
    shifted = diverged(conn, eval_run_id)
    assert shifted is not None
    assert abs(shifted.cases[0].score - honest.cases[0].score) == pytest.approx(DELTA)
    # The aggregate is a 6 dp mean, so one row shifted by 1e-6 among five leaves it unchanged:
    # the check catches the row because it compares every number, not the headline.
    assert untampered.json()["cases"][0]["score"] == honest.cases[0].score
    assert tampered.json()["cases"][0]["score"] == shifted.cases[0].score
    assert tampered.json()["cases"][0]["score"] != untampered.json()["cases"][0]["score"]
    capture(
        "numeric-divergence.txt",
        "\n".join(
            [
                f"eval run id: {eval_run_id}",
                f"seeded delta on one stored case score: {DELTA!r}",
                f"  case {honest.cases[0].case_id}: {honest.cases[0].score!r} -> "
                f"{shifted.cases[0].score!r}",
                f"  aggregate ({len(honest.cases)} cases): {honest.score!r} -> {shifted.score!r} "
                f"(delta {shifted.score - honest.score!r})",
                "",
                f"cli  {' '.join(argv)}",
                f"  {stdout[:-1].decode()}",
                f"http GET {path}  (gateway read callable substituted)",
                f"  {tampered.content.decode()}",
                "parity assertion: FAILED on the assertion",
                f"  {numeric_failure.value}".replace("\n", "\n  "),
                "",
                f"http GET {path}  (unaltered)",
                f"  {untampered.content.decode()}",
                "parity assertion: passed",
                "",
            ]
        ),
    )

    # ---- the event side: one event dropped, and separately two transposed --------------------
    session_conn, session_id = seed_run_session(clean_env)
    try:
        replayed = read.replay(session_conn, session_id)
        assert_journal_agreement(session_conn, session_id, replayed)

        dropped_at = index_of(replayed, "loop.iteration.finished")
        dropped = divergence.dropped(replayed, dropped_at)
        assert len(dropped) == len(replayed) - 1
        with pytest.raises(AssertionError) as drop_failure:
            assert_journal_agreement(session_conn, session_id, dropped)

        first, second = index_of(replayed, "loop.iteration.started"), dropped_at
        assert first != second
        transposed = divergence.transposed(replayed, first, second)
        assert len(transposed) == len(replayed)
        assert sorted(transposed, key=lambda e: e.seq) == list(replayed)
        with pytest.raises(AssertionError) as transpose_failure:
            assert_journal_agreement(session_conn, session_id, transposed)

        assert_journal_agreement(session_conn, session_id, replayed)
    finally:
        session_conn.close()

    def rows(events: Sequence[read.ReplayedEvent]) -> list[str]:
        return [f"  {event.seq:>3}  {event.name}" for event in events]

    capture(
        "event-tampering.txt",
        "\n".join(
            [
                f"session id: {session_id}",
                f"journaled turns: {turns_under(conn, session_id)}",
                "",
                "untampered replay:",
                *rows(replayed),
                "journal agreement: passed",
                "",
                f"one event dropped (seq {replayed[dropped_at].seq}, {replayed[dropped_at].name}):",
                *rows(dropped),
                "journal agreement: FAILED on the assertion",
                f"  {drop_failure.value}".replace("\n", "\n  "),
                "",
                f"two events transposed (seq {replayed[first].seq} and {replayed[second].seq}):",
                *rows(transposed),
                "journal agreement: FAILED on the assertion",
                f"  {transpose_failure.value}".replace("\n", "\n  "),
                "",
            ]
        ),
    )

    # ---- the resumption side: a stream that redelivers an already-sent seq -------------------
    cursor = 2
    with client_over(tmp_harnessed_home) as client:
        resumed = frames_of(client, evaluated.session_id, str(cursor))
    assert resumed and resumed[0].id == cursor + 1
    assert_exactly_once(resumed)

    redelivered_seq = resumed[1].id
    redelivered = divergence.redelivered(resumed, redelivered_seq)
    assert len(redelivered) == len(resumed) + 1
    with pytest.raises(AssertionError) as resume_failure:
        assert_exactly_once(redelivered)

    assert_exactly_once(resumed)
    capture(
        "duplicate-seq.txt",
        "\n".join(
            [
                f"session id: {evaluated.session_id}",
                f"resumed with Last-Event-ID: {cursor}",
                "",
                "untampered frames:",
                *(f"  id: {frame.id:<3} event: {frame.event}" for frame in resumed),
                "exactly-once assertion: passed",
                "",
                f"seq {redelivered_seq} redelivered after it was already sent:",
                *(f"  id: {frame.id:<3} event: {frame.event}" for frame in redelivered),
                "exactly-once assertion: FAILED on the assertion",
                f"  {resume_failure.value}".replace("\n", "\n  "),
                "",
            ]
        ),
    )


# ---- FR-37: the parity set ----------------------------------------------------------------------
#
# A table, not a loop over the route registry (decision 23). Every ``--json`` read surface the
# CLI has is a row here; every registered route is either the twin of a row or a named exclusion
# in ``OUTSIDE_THE_SET``, and ``test_every_registered_surface_is_a_row_or_a_named_exclusion``
# holds the two tables against ``app.routes`` in both directions so an omission is loud.
#
# Three of the seeds produce rather than read: ``eval run`` writes the run ``eval show`` reads
# back, ``harnessed run`` writes the session ``memory episodes`` and ``/v1/runs/{run_key}`` read
# back, and ``improve run`` writes the cycle ``/v1/improve/cycles/{improvement_run_id}`` reads
# back. Their ids exist only after the CLI has spoken, so ``{placeholders}`` in an argv or a path
# are filled from the seeding commands' own stdout. Extraction is the one thing parsed; the
# comparison itself is ``assert_parity`` on the bytes.

SEED_FACT = "offline first is the rule"
SEARCH_WORD = "offline"


@dataclass(frozen=True, slots=True)
class Pair:
    """One row of the parity set: a ``--json`` invocation and the path that must equal it."""

    cli_argv: tuple[str, ...]
    http_path: str
    note: str
    #: A command that writes the entity it prints. Its line is captured once, at seeding, and
    #: that line is the CLI half: running it again would print a second, different entity.
    produces: bool = False

    @property
    def route(self) -> str:
        """The registered route this pair exercises: the path with its query string removed."""
        return self.http_path.split("?", 1)[0]


PARITY_SET: tuple[Pair, ...] = (
    Pair(
        ("eval", "show", "{eval_run_id}", "--json"),
        "/v1/eval/runs/{eval_run_id}",
        "runner.json_payload on both sides",
    ),
    Pair(
        ("eval", "list", "--json"),
        "/v1/eval/runs",
        "the RunSummary rows, newest first",
    ),
    Pair(
        ("eval", "suites", "--json"),
        "/v1/eval/suites",
        "suite.listing then suite.listing_payload: each suite with its two counts as strings and "
        "its latest scored run, null where it never ran (FR-139, decision 86)",
    ),
    Pair(
        ("eval", "compare", "{eval_run_id}", "{eval_run_id_after}", "--json"),
        "/v1/eval/compare?before={eval_run_id}&after={eval_run_id_after}",
        "runner.comparison then runner.comparison_payload: the comparison FR-62 serves out "
        "of evals/scoring.py, so no reader ever computes one (decision 37)",
    ),
    Pair(
        ("improve", "run", "--fresh", "--json"),
        "/v1/improve/cycles/{improvement_run_id}",
        "the producing command prints steps.json_payload for the cycle it just recorded, and "
        "cycle_for reads a fresh full cycle back identically (ticket 074); `improve show` is "
        "its reading twin, its own row below (ticket 232)",
        produces=True,
    ),
    Pair(
        ("memory", "search", SEARCH_WORD, "--json"),
        f"/v1/memory/facts?q={SEARCH_WORD}",
        "semantic.search under `facts`",
    ),
    Pair(
        ("memory", "procedures", "--json"),
        "/v1/memory/procedures",
        "procedural.active under `procedures`",
    ),
    Pair(
        ("memory", "episodes", "{session_id}", "--json"),
        "/v1/memory/episodes/{session_id}",
        "episodic.for_session under `episodes`, keyed by the session id `harnessed run` wrote",
    ),
    Pair(
        ("sessions", "list", "--json"),
        "/v1/sessions",
        "read.sessions then read.sessions_payload under `sessions`: the listing FR-48 adds so a "
        "session id is reachable without the terminal that printed it",
    ),
    Pair(
        ("runs", "totals", "--json"),
        "/v1/runs/totals",
        "runs.totals then runs.totals_payload: the journal counted all time, every count a "
        "string (FR-67, decision 41); counted_at is the frozen clock on both halves",
    ),
    Pair(
        ("runs", "turn-seconds", "--json"),
        "/v1/runs/turn_seconds",
        "runs.turn_seconds then runs.turn_seconds_payload: the mean turn in seconds, rounded "
        "to one decimal in the store and emitted as a string, with the pre-007 turns counted "
        "and named rather than averaged (FR-68, decision 54)",
    ),
    Pair(
        ("events", "gate", "--json"),
        "/v1/events/gate",
        "read.gate_split then read.gate_split_payload: the retrieval gate's three buckets over "
        "every persisted decision with failed_open its own bucket, every figure a string and "
        "decided the served sum (FR-69, decisions 41 and 48)",
    ),
    Pair(
        ("events", "totals", "--json"),
        "/v1/events/totals",
        "read.totals then read.totals_payload: every event and every seq 1 session, all time, "
        "every count a string (FR-70, decision 47); counted_at is the frozen clock on both halves",
    ),
    Pair(
        ("memory", "consolidate", "--list", "--json"),
        "/v1/memory/consolidation",
        "consolidation.listing then listing_payload: the threshold and the waiting count as "
        "strings, every batch newest first with its facts by exact source (FR-152)",
    ),
    Pair(
        ("memory", "totals", "--json"),
        "/v1/memory/totals",
        "totals.totals then totals.totals_payload: facts active and archived, episodes and the "
        "active procedure set, four strings from one package (FR-70, decision 41)",
    ),
    Pair(
        ("prices", "list", "--json"),
        "/v1/prices",
        "pricing.stored then pricing.json_payload: every stored rate the decimal string the "
        "refresh wrote, cached_input null where unpublished, each row with its own fetched_at "
        "(FR-73, decision 53)",
    ),
    Pair(
        ("providers", "list", "--json"),
        "/v1/providers",
        "registry.states then registry.json_payload: one row per registered provider, configured "
        "read off the key variable's name through config.load and never its value, key_var and "
        "the table's default_model (FR-89, decision 55); both halves read the same environment",
    ),
    Pair(
        ("agents", "list", "--json"),
        "/v1/agents",
        "probes.states then probes.json_payload: one row per agent in agents.toml (read from the "
        "working directory, where agents test and doctor read it), the newest probe and "
        "fingerprint_matches by the ticket 089 rule, or null fields with probed false (FR-90)",
    ),
    Pair(
        ("spend", "--json"),
        "/v1/spend",
        "runs.spend then runs.spend_payload: the journal joined to the stored prices in Decimal, "
        "rounded to four places in the store, the three null classes counted beside total and "
        "never folded into it (FR-74, decision 53)",
    ),
    Pair(
        ("spend", "--session", "{session_id}", "--json"),
        "/v1/spend?session={session_id}",
        "the same read scoped to the one run key `harnessed run` printed; its fake turns are "
        "unpriced against the recorded catalog, so the row compares the null classes' bytes",
    ),
    Pair(
        ("run", PROMPT, "--json"),
        "/v1/runs/{run_key}",
        "runs.run_payload on both sides, read off the journal after the run; the run key here "
        "is the session id `harnessed run` printed (decision 24)",
        produces=True,
    ),
    Pair(
        ("decisions", "list", "{session_id}", "--json"),
        "/v1/sessions/{session_id}/decisions",
        "decisions.for_session then decisions.json_payload: a session's decisions in order, "
        "options nested; the seeded `run` session routed nothing, so both sides compare the "
        "empty-collection bytes (FR-120, decision 67)",
    ),
    Pair(
        ("permissions", "list", "--state", "pending", "--json"),
        "/v1/permissions?state=pending",
        "pending.listing then pending.listing_payload: every request still waiting for an answer; "
        "the seeded home has none, so both sides compare the empty-collection bytes, and the "
        "one-per-state read is its own test (FR-130, decision 74)",
    ),
    Pair(
        ("permissions", "list", "--session", "{session_id}", "--json"),
        "/v1/permissions?session={session_id}",
        "the same two callables scoped to one session, every decision in every state in "
        "requested_at order; the seeded `run` session asked for nothing (FR-130)",
    ),
    Pair(
        ("sessions", "messages", "{session_id}", "--json"),
        "/v1/sessions/{session_id}/messages",
        "messages.for_session then messages.messages_payload: one message, the seeded `run` "
        "session's own routed pair, joined on message_seq against decisions, turns and outputs "
        "(FR-128, decision 66)",
    ),
    Pair(
        ("sessions", "recent", "--json"),
        "/v1/messages",
        "messages.recent then messages.recent_payload: newest first over hop-0 decisions alone "
        "(FR-128, decision 66)",
    ),
    Pair(
        ("task", "list", "--json"),
        "/v1/tasks",
        "store.listing then store.listing_payload: the one queued task `task add` wrote, every "
        "column a string or null with its reason (FR-143, decision 76)",
    ),
    Pair(
        ("task", "list", "--state", "needs_human", "--limit", "5", "--json"),
        "/v1/tasks?state=needs_human&limit=5",
        "the same read narrowed to the tasks waiting on the operator, longest waiting first; the "
        "seeded home has none (FR-143, G-59)",
    ),
    Pair(
        ("task", "list", "--session", "{session_id}", "--json"),
        "/v1/tasks?session={session_id}",
        "the same read scoped to the seeded `run` session, which is no task's: `[]` (G-37)",
    ),
    Pair(
        ("task", "show", "{task_id}", "--json"),
        "/v1/tasks/{task_id}",
        "store.get then store.json_payload: the queued task's row (FR-143)",
    ),
    Pair(
        ("task", "totals", "--json"),
        "/v1/tasks/totals",
        "store.totals then store.totals_payload: all time, `counted_at` from the frozen clock "
        "(FR-143, decision 47's shape)",
    ),
    Pair(
        ("schedule", "list", "--json"),
        "/v1/schedules",
        "schedule.listing then schedule.listing_payload: no schedule is added in the seeded home, "
        "so both sides compare the empty-collection bytes; the populated read is "
        "tests/test_schedule.py's (FR-142, decision 79)",
    ),
    Pair(
        ("router", "status", "--json"),
        "/v1/router",
        "decisions.probe_state then decisions.router_json_payload: the newest recorded router "
        "probe, the confidence floors per route from agents.toml, and the needs-memory threshold; "
        "probed false with nulls before any live rung, since the seeded home never runs the live "
        "doctor rung (FR-121, decision 67, G-32)",
    ),
    Pair(
        ("health", "--json"),
        "/v1/health",
        "the same create_app built in process: schema version, resolved database path and route "
        "count (FR-134, decision 19)",
    ),
    Pair(
        ("runs", "list", "--json"),
        "/v1/runs",
        "runs.listing under `runs`, newest first, each row's key kind (FR-134)",
    ),
    Pair(
        ("runs", "list", "--limit", "1", "--json"),
        "/v1/runs?limit=1",
        "the same listing bounded by --limit, as ?limit= bounds it (FR-134)",
    ),
    Pair(
        ("improve", "promotions", "--json"),
        "/v1/promotions",
        "procedural.promotions under `promotions`, as stored (FR-134)",
    ),
    Pair(
        ("runs", "trace", "{run_key}", "--json"),
        "/v1/runs/{run_key}/trace",
        "trace.assemble: one object per iteration of the seeded run (FR-134)",
    ),
    Pair(
        ("sessions", "events", "{session_id}", "--json"),
        "/v1/sessions/{session_id}/events",
        "read.json_payload: the seeded session's events in order (FR-134)",
    ),
    Pair(
        ("memory", "fact", "{mem_id}", "--json"),
        "/v1/memory/facts/{mem_id}",
        "semantic.get as the store's own row, archived or not (FR-134)",
    ),
    Pair(
        ("improve", "show", "{improvement_run_id}", "--json"),
        "/v1/improve/cycles/{improvement_run_id}",
        "steps.cycle_for then steps.json_payload: the cycle `improve run` recorded (FR-134)",
    ),
)

#: Registered routes with no ``--json`` twin, each with the reason it is outside the set.
OUTSIDE_THE_SET: dict[str, str] = {
    "/v1/sessions/{session_id}/stream": (
        "an SSE stream is not one JSON object; `sessions stream` writes the same frames and "
        "a dedicated byte-comparison test below covers it, not PARITY_SET (FR-39, FR-134)"
    ),
    "/v1/outputs/{output_id}": (
        "the twin writes bytes to --out, not JSON to stdout (decision 65's second named "
        "exception); a dedicated byte-comparison test below covers it, not PARITY_SET"
    ),
}


@dataclass(frozen=True, slots=True)
class Seeded:
    """The ids the seeding commands produced, keyed by the glossary term they are (MUST 15)."""

    eval_run_id: str
    #: A second run of the same suite, so the comparison row (ticket 098) has two sides. It
    #: is scored after the first by the same replaying provider, so the two agree case for
    #: case - which is the honest shape for a comparison whose four lists are then empty.
    eval_run_id_after: str
    session_id: str
    improvement_run_id: str
    #: The seeded fact's id, read back through ``memory search`` (ticket 232).
    mem_id: str
    #: The one line each producing row printed, keyed by its argv: that line is its CLI half.
    produced: dict[tuple[str, ...], bytes]
    #: The one queued task `task add` wrote, which `task show` reads back (ticket 241).
    task_id: str = ""

    def fill(self, template: str) -> str:
        """``{run_key}`` is the session id: the shape ``harnessed run`` prints (decision 24)."""
        return template.format(
            eval_run_id=self.eval_run_id,
            eval_run_id_after=self.eval_run_id_after,
            session_id=self.session_id,
            run_key=self.session_id,
            improvement_run_id=self.improvement_run_id,
            mem_id=self.mem_id,
            task_id=self.task_id,
        )


Invoke = Callable[[list[str]], bytes]


def id_from(stdout: bytes, key: str) -> str:
    """One id out of a ``--json`` line. Parsed for the id only; never for the comparison."""
    value = json.loads(stdout)[key]
    assert isinstance(value, str) and value, f"{key} missing from {stdout!r}"
    return value


def seed_through(invoke: Invoke) -> Seeded:
    """Write every row both halves will read, through the CLI and nothing else.

    One home, one database, seeded once: the two halves must see the same rows or the
    comparison measures the seeding rather than the parity. ``eval run`` and ``memory add`` are
    writers with reading twins (``eval show``, ``memory search``) and are not rows; the
    producing rows are run here, once each, in table order, and their lines kept.
    """
    eval_run_id = id_from(invoke(["eval", "run", "smoke", "--fresh", "--json"]), "eval_run_id")
    eval_run_id_after = id_from(
        invoke(["eval", "run", "smoke", "--fresh", "--json"]), "eval_run_id"
    )
    invoke(["memory", "add", SEED_FACT, "--source", "ticket 072"])
    produced = {pair.cli_argv: invoke(list(pair.cli_argv)) for pair in PARITY_SET if pair.produces}
    session_id = id_from(produced[("run", PROMPT, "--json")], "run_key")
    improvement_run_id = id_from(
        produced[("improve", "run", "--fresh", "--json")], "improvement_run_id"
    )
    found = json.loads(invoke(["memory", "search", SEARCH_WORD, "--json"]))["facts"]
    mem_id = found[0]["mem_id"]
    task_id = id_from(invoke(["task", "add", "tidy the notes", "--json"]), "task_id")
    return Seeded(
        eval_run_id, eval_run_id_after, session_id, improvement_run_id, mem_id, produced, task_id
    )


#: The recorded OpenRouter listing ``tests/test_pricing.py`` parses (FR-73, ticket 132).
RECORDED_CATALOG = Path(__file__).resolve().parent / "fixtures" / "catalogs" / "openrouter.json"


def seed_prices(home: Path) -> None:
    """Store prices the way ``harnessed prices refresh`` does, minus the one network read.

    The exception to "through the CLI and nothing else": the only CLI writer of ``prices`` asks a
    provider, which the socket guard refuses, so the recorded catalog goes through the refresh's
    own ``pricing.parse`` and ``pricing.upserts`` instead. Without it the row compares two empty
    collections and proves nothing about a rate's characters.
    """
    catalog = pricing.parse(
        "openrouter",
        RECORDED_CATALOG.read_bytes(),
        source_url=pricing.catalog_url("openrouter"),
        fetched_at=clock.isoformat(clock.now()),
    )
    conn = db.connect(home / db.DATABASE_FILENAME)
    try:
        db.write(conn, pricing.upserts(catalog))
    finally:
        conn.close()


def in_process(argv: list[str]) -> bytes:
    """The in-process CLI half. ``memory add`` prints prose, so the one-line check is the row's."""
    result = cli.invoke(app, argv)
    assert result.exit_code == 0, f"{argv} exited {result.exit_code}\n{result.stderr}"
    return result.stdout.encode("utf-8")


def cli_half(invoke: Invoke, seeded: Seeded, pair: Pair) -> tuple[list[str], bytes]:
    argv = [seeded.fill(part) for part in pair.cli_argv]
    stdout = seeded.produced[pair.cli_argv] if pair.produces else invoke(argv)
    assert stdout.endswith(b"\n") and b"\n" not in stdout[:-1], f"{argv} did not print one line"
    return argv, stdout


def http_half(client: TestClient, seeded: Seeded, pair: Pair) -> tuple[str, Any]:
    path = seeded.fill(pair.http_path)
    response = client.get(path)
    assert (
        response.status_code == 200
    ), f"GET {path} answered {response.status_code}: {response.text}"
    return path, response


def compare_every_pair(
    invoke: Invoke, seeded: Seeded, client: TestClient, evidence: str
) -> list[str]:
    """Every row through both halves. Returns the rows that diverged, with their byte strings.

    The rows are compared after all the seeding, so the listing rows (``eval list``,
    ``memory procedures``) see the same ledger from both sides. Each row's outcome is recorded
    whether or not it held, so a single divergent pair is a precise finding rather than the
    first assertion that happened to trip.
    """
    lines: list[str] = []
    failures: list[str] = []
    for pair in PARITY_SET:
        argv, stdout = cli_half(invoke, seeded, pair)
        path, response = http_half(client, seeded, pair)
        try:
            assert_parity(stdout, response)
        except AssertionError as failure:
            outcome = f"DIVERGED\n  {failure}".replace("\n", "\n  ")
            failures.append(f"{' '.join(argv)}  <>  GET {path}\n  {failure}")
        else:
            outcome = "equal as bytes"
        lines += [
            f"cli  harnessed {' '.join(argv)}",
            f"  {stdout[:-1].decode()}",
            f"http GET {path}",
            f"  {response.content.decode()}",
            f"parity: {outcome}",
            f"note: {pair.note}",
            "",
        ]
    capture(evidence, "\n".join(lines), EVIDENCE_072)
    return failures


@pytest.fixture
def seeded(clean_env: None, tmp_harnessed_home: Path) -> Seeded:
    found = seed_through(in_process)
    seed_prices(tmp_harnessed_home)
    return found


def test_every_pair_is_byte_identical(seeded: Seeded, tmp_harnessed_home: Path) -> None:
    """FR-37: for every row of the table, the CLI's line and the HTTP body are the same bytes."""
    with client_over(tmp_harnessed_home) as client:
        failures = compare_every_pair(in_process, seeded, client, "parity-table.txt")
    assert not failures, f"{len(failures)} of {len(PARITY_SET)} pairs diverged:\n" + "\n".join(
        failures
    )


def registered_paths(home: Path) -> set[str]:
    application = create_app(home / db.DATABASE_FILENAME)
    return {route.path for route in application.routes if hasattr(route, "path")}


def test_every_registered_surface_is_a_row_or_a_named_exclusion(tmp_harnessed_home: Path) -> None:
    """A surface silently outside the set fails; a row or an exclusion with no route fails too."""
    registered = {path for path in registered_paths(tmp_harnessed_home) if path.startswith("/v1")}
    in_rows = {pair.route for pair in PARITY_SET}
    excluded = set(OUTSIDE_THE_SET)
    assert not in_rows & excluded, f"both a row and an exclusion: {in_rows & excluded}"
    assert (
        registered == in_rows | excluded
    ), f"unlisted: {registered - in_rows - excluded}; stale: {(in_rows | excluded) - registered}"
    for route, reason in OUTSIDE_THE_SET.items():
        assert reason.strip(), f"{route} is excluded without a reason"
    for pair in PARITY_SET:
        assert "--json" in pair.cli_argv, f"{pair.cli_argv} is not a --json surface"
        assert pair.note.strip(), f"{pair.cli_argv} has no note"


def test_the_minimum_set_is_covered() -> None:
    """The seven surfaces FR-37 names by hand, each present as a row."""
    heads = {pair.cli_argv[: 1 if pair.cli_argv[0] == "run" else 2] for pair in PARITY_SET}
    assert heads >= {
        ("eval", "show"),
        ("eval", "list"),
        ("improve", "run"),
        ("memory", "search"),
        ("memory", "procedures"),
        ("memory", "episodes"),
        ("run",),
    }


def endpoint_for(application: Any, path: str) -> Callable[..., Any]:
    matches = [
        route.endpoint for route in application.routes if getattr(route, "path", None) == path
    ]
    assert len(matches) == 1, f"{path} is registered {len(matches)} times"
    return matches[0]


def test_handlers_return_the_existing_json_payload_dict_unchanged(
    seeded: Seeded, tmp_harnessed_home: Path
) -> None:
    """MUST rule 16 on the handler's object, before serialization, and then on the bytes.

    ``runner.json_payload`` and ``steps.json_payload`` already exist; the handler must return
    exactly that dict: same keys in the same order, same values, and the stored 6 dp scores
    crossing unrounded. The byte assertion afterwards is the same fact one layer down.
    """
    application = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    conn = db.connect_readonly(tmp_harnessed_home / db.DATABASE_FILENAME)
    try:
        scored = runner.scored_run(conn, seeded.eval_run_id)
        assert scored is not None
        expected_eval = runner.json_payload(scored)
        eval_handler = endpoint_for(application, "/v1/eval/runs/{eval_run_id}")
        got_eval = eval_handler(seeded.eval_run_id, conn)
        assert got_eval == expected_eval
        assert list(got_eval) == list(expected_eval), "re-keyed or re-ordered"
        for mine, theirs in zip(got_eval["cases"], expected_eval["cases"], strict=True):
            assert mine["score"] == theirs["score"]
            assert isinstance(mine["score"], float)
            assert mine["score"] == round(mine["score"], 6), "a score with more than 6 dp"

        cycle = steps.cycle_for(conn, seeded.improvement_run_id)
        assert cycle is not None
        expected_cycle = steps.json_payload(cycle)
        cycle_handler = endpoint_for(application, "/v1/improve/cycles/{improvement_run_id}")
        got_cycle = cycle_handler(seeded.improvement_run_id, conn)
        assert got_cycle == expected_cycle
        assert list(got_cycle) == list(expected_cycle), "re-keyed or re-ordered"
    finally:
        conn.close()

    with TestClient(application) as client:
        eval_body = client.get(f"/v1/eval/runs/{seeded.eval_run_id}").content
        cycle_body = client.get(f"/v1/improve/cycles/{seeded.improvement_run_id}").content
    assert eval_body == canonical(expected_eval).encode("utf-8")
    assert eval_body == in_process(["eval", "show", seeded.eval_run_id, "--json"])[:-1]
    assert cycle_body == canonical(expected_cycle).encode("utf-8")
    for score in (case["score"] for case in json.loads(eval_body)["cases"]):
        assert score == round(score, 6)


# ---- ticket 079: both halves reject an id neither holds -----------------------------------------
#
# The parity table only ever asks about entities that exist, which is how `memory episodes`
# printed `[]` for an unknown session while its route answered 404. A refusal row asserts the two
# halves *reject* together: the CLI exits non-zero naming the id, the gateway answers 404
# `SurfaceNotFound`. Their error bytes are not compared - a CLI failure line and a JSON error body
# are different shapes by convention (ticket 079).

EVIDENCE_079 = REPO_ROOT / "evidence" / "079"
UNKNOWN_SESSION = "run_00000000000000000000000000"
UNKNOWN_EVAL_RUN = "eval_00000000000000000000000000"


@dataclass(frozen=True, slots=True)
class Refusal:
    """One id-keyed row of the parity set asked about an id no ledger holds."""

    cli_argv: tuple[str, ...]
    http_path: str
    note: str
    #: The id neither half holds, and the words the refusal must name beside it. A comparison
    #: has two sides, so its refusal has to say which one asked (ticket 098, the 079 lesson).
    unknown: str = UNKNOWN_SESSION
    names: tuple[str, ...] = ()

    @property
    def route(self) -> str:
        """The registered route this refusal exercises: no query string, no id."""
        path = self.http_path.split("?", 1)[0]
        return path.replace(UNKNOWN_SESSION, "{session_id}")


REFUSAL_SET: tuple[Refusal, ...] = (
    Refusal(
        ("memory", "episodes", UNKNOWN_SESSION, "--json"),
        f"/v1/memory/episodes/{UNKNOWN_SESSION}",
        "no events row carries the id, so episodic.for_session answers None on both sides",
    ),
    Refusal(
        ("spend", "--session", UNKNOWN_SESSION, "--json"),
        f"/v1/spend?session={UNKNOWN_SESSION}",
        "no turns row carries the key, so runs.spend refuses it: a run that never happened did "
        "not cost nothing (FR-74)",
    ),
    Refusal(
        ("decisions", "list", UNKNOWN_SESSION, "--json"),
        f"/v1/sessions/{UNKNOWN_SESSION}/decisions",
        "no events row carries the id, so decisions.for_session refuses it the same way "
        "episodic.for_session's held check does (FR-120)",
    ),
    Refusal(
        ("permissions", "list", "--session", UNKNOWN_SESSION, "--json"),
        f"/v1/permissions?session={UNKNOWN_SESSION}",
        "no events row carries the id, so pending.listing refuses it: an unknown session is 404 "
        "and an empty one is not (FR-130)",
    ),
    Refusal(
        ("eval", "compare", UNKNOWN_EVAL_RUN, "{eval_run_id}", "--json"),
        f"/v1/eval/compare?before={UNKNOWN_EVAL_RUN}&after={{eval_run_id}}",
        "runner.comparison refuses the before side and names it, so a reader holding two ids "
        "is told which of them the ledgers do not hold (ticket 098)",
        unknown=UNKNOWN_EVAL_RUN,
        names=("before",),
    ),
    Refusal(
        ("eval", "compare", "{eval_run_id}", UNKNOWN_EVAL_RUN, "--json"),
        f"/v1/eval/compare?before={{eval_run_id}}&after={UNKNOWN_EVAL_RUN}",
        "the same refusal on the after side, so neither side can be the only one covered",
        unknown=UNKNOWN_EVAL_RUN,
        names=("after",),
    ),
)


def test_both_halves_reject_an_unknown_session(
    seeded: Seeded,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown id is refused by both halves; a held id, empty or not, is equal as bytes.

    The CLI half of a refusal runs through ``main``, because ``CliRunner`` does not surface the
    failure line an operator reads on stderr (ticket 028).
    """
    rows = {pair.route for pair in PARITY_SET}
    lines: list[str] = []
    with client_over(tmp_harnessed_home) as client:
        for refusal in REFUSAL_SET:
            assert refusal.route in rows, f"{refusal.route} refuses but has no parity row"
            argv = [seeded.fill(part) for part in refusal.cli_argv]
            path = seeded.fill(refusal.http_path)
            capsys.readouterr()
            monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
            with pytest.raises(SystemExit) as exited:
                main()
            captured = capsys.readouterr()
            response = client.get(path)
            assert exited.value.code not in (0, None), f"{argv} accepted"
            assert captured.out == "", f"{argv} printed: {captured.out!r}"
            assert refusal.unknown in captured.err, captured.err
            assert response.status_code == 404, response.text
            assert response.json()["error"] == "SurfaceNotFound"
            assert refusal.unknown in response.json()["message"], response.text
            for named in refusal.names:
                assert named in captured.err, captured.err
                assert named in response.json()["message"], response.text
            lines += [
                f"unknown id: {refusal.unknown}",
                f"cli  harnessed {' '.join(argv)}",
                f"  exit {exited.value.code}  stderr: {captured.err.strip()}",
                f"http GET {path}",
                f"  {response.status_code}  {response.content.decode()}",
                "both reject: yes",
                f"note: {refusal.note}",
                "",
            ]

        conn = db.connect()
        try:
            bus = EventBus(conn=conn)
            bus.publish("run.started", {"provider": "fake"})
        finally:
            conn.close()
        for label, session_id in (
            ("known session, zero episodes", bus.run_id),
            ("session with episodes", seeded.session_id),
        ):
            argv = ["memory", "episodes", session_id, "--json"]
            stdout = cli_bytes(argv)
            response = client.get(f"/v1/memory/episodes/{session_id}")
            assert response.status_code == 200, response.text
            assert_parity(stdout, response)
            lines += [
                f"{label}: {session_id}",
                f"cli  harnessed {' '.join(argv)}",
                f"  exit 0  {stdout[:-1].decode()}",
                f"http GET /v1/memory/episodes/{session_id}",
                f"  {response.status_code}  {response.content.decode()}",
                "parity: equal as bytes",
                "",
            ]
    capture("unknown-session-parity.txt", "\n".join(lines), EVIDENCE_079)


EVIDENCE_180 = REPO_ROOT / "evidence" / "180"
EVIDENCE_234 = REPO_ROOT / "evidence" / "234"


def test_the_permissions_pair_holds_as_bytes_with_one_decision_per_state(
    seeded: Seeded, tmp_harnessed_home: Path
) -> None:
    """FR-130: six decisions, one per state, served in ``requested_at`` order; the CLI's line and
    the HTTP body are the same bytes for ``--state pending`` and for ``--session <id>``."""
    ceiling = "2026-01-01T12:10:00Z"
    with closing(db.connect()) as conn:
        rows = {
            name: acp_pending.record(
                conn,
                session_id=seeded.session_id,
                agent="probe",
                tool_kind="execute",
                description=f"the {name} request",
                ceiling_at=ceiling
                if name in ("pending", "approved", "denied", "expired")
                else None,
                answer={"allowed": "approve", "refused": "deny"}.get(name),
                rule={"allowed": "allowlist", "refused": "deny_list"}.get(name),
            )
            for name in ("pending", "approved", "denied", "expired", "allowed", "refused")
        }
        acp_pending.answer(
            conn, rows["approved"].permission_id, "approve", rule="operator", via="console"
        )
        acp_pending.answer(conn, rows["denied"].permission_id, "deny", rule="operator", via="cli")
        acp_pending.answer(conn, rows["expired"].permission_id, "deny", rule="ceiling", via=None)
    pairs = [p for p in PARITY_SET if p.route == "/v1/permissions"]
    assert len(pairs) == 2
    lines = ["ticket 234 - the permissions pair, one decision per state\n"]
    with client_over(tmp_harnessed_home) as client:
        for pair in pairs:
            argv, stdout = cli_half(in_process, seeded, pair)
            path, response = http_half(client, seeded, pair)
            assert_parity(stdout, response)
            lines.append(f"cli  harnessed {' '.join(argv)}\nhttp GET {path}\n  {response.text}\n")
        served = client.get(f"/v1/permissions?session={seeded.session_id}").json()["permissions"]
    assert [item["state"] for item in served] == [
        "pending", "approved", "denied", "expired", "allowed", "refused"
    ]  # fmt: skip
    capture("permissions-parity.txt", "\n".join(lines), EVIDENCE_234)


def test_the_decisions_pair_and_refusal_hold_as_bytes(
    seeded: Seeded,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC4 (FR-120): ``decisions list <session_id> --json`` and ``GET
    /v1/sessions/{session_id}/decisions`` are byte-identical, and both refuse an unknown id."""
    pair = next(p for p in PARITY_SET if p.http_path == "/v1/sessions/{session_id}/decisions")
    with client_over(tmp_harnessed_home) as client:
        argv, stdout = cli_half(in_process, seeded, pair)
        path, response = http_half(client, seeded, pair)
    assert_parity(stdout, response)

    refusal = next(r for r in REFUSAL_SET if r.http_path.endswith("/decisions"))
    refusal_argv = ["decisions", "list", refusal.unknown, "--json"]
    with client_over(tmp_harnessed_home) as client:
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", ["harnessed", *refusal_argv])
        with pytest.raises(SystemExit) as exited:
            main()
        captured = capsys.readouterr()
        refusal_response = client.get(refusal.http_path)
    assert exited.value.code not in (0, None), refusal_argv
    assert refusal_response.status_code == 404, refusal_response.text
    assert refusal.unknown in captured.err and refusal.unknown in refusal_response.text
    capture(
        "parity-rows.txt",
        "ticket 180 - the decisions pair and refusal, both halves\n\n"
        f"cli  harnessed {' '.join(argv)}\n"
        f"  {stdout[:-1].decode()}\n"
        f"http GET {path}\n"
        f"  {response.content.decode()}\n"
        "parity: equal as bytes\n\n"
        f"cli  harnessed {' '.join(refusal_argv)}\n"
        f"  exit {exited.value.code}  stderr: {captured.err.strip()}\n"
        f"http GET {refusal.http_path}\n"
        f"  {refusal_response.status_code}  {refusal_response.content.decode()}\n",
        EVIDENCE_180,
    )


# ---- AC5 (FR-127, decision 65): the outputs bytes pair, outside PARITY_SET ----------------------

EVIDENCE_181 = REPO_ROOT / "evidence" / "181"


def plant_output(
    conn: sqlite3.Connection, run_id: str, *, message_seq: int = 1, hop: int = 0
) -> str:
    """One delegation output, written through ``acp/outputs.py``'s own writer - never SQL spelled
    here, so the other recorded bodies do not move. A text summary at position 0, no files.
    Returns the id written."""
    (planted,) = acp_outputs.record_hop(
        conn,
        run_id=run_id,
        message_seq=message_seq,
        hop=hop,
        task_hash=None,
        summary="ticket 181 - a planted agent result, for the outputs byte-parity pair",
        cwd=Path.cwd(),
        before={},
        produces=None,
        outputs_glob=None,
    )
    return planted


def test_the_outputs_bytes_pair_holds_and_an_unknown_id_is_404_on_both_halves(
    seeded: Seeded,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC5: ``outputs get <id> --out <path>`` writes the same bytes ``GET /v1/outputs/{id}``
    serves, and both halves refuse an id neither ledger holds."""
    with closing(db.connect()) as conn:
        output_id = plant_output(conn, seeded.session_id)

    with client_over(tmp_harnessed_home) as client:
        response = client.get(f"/v1/outputs/{output_id}")
        assert response.status_code == 200, response.text
        unknown = client.get("/v1/outputs/out_0000000000000000")
        assert unknown.status_code == 404, unknown.text

    out_path = tmp_path / "output.bin"
    result = cli.invoke(app, ["outputs", "get", output_id, "--out", str(out_path)])
    assert result.exit_code == 0, f"outputs get exited {result.exit_code}\n{result.stderr}"
    written = out_path.read_bytes()
    assert written == response.content, "the CLI and the gateway disagree on the stored bytes"

    # A CliRunner result never carries an escaping HarnessError (it is recorded, unrendered); the
    # error contract is `main`'s, so the refusal is read the way an operator's own shell would see
    # it (`test_cli.py::shell`, learnings 2026-09-27).
    unknown_seen = shell(
        ["outputs", "get", "out_0000000000000000", "--out", str(tmp_path / "missing.bin")],
        monkeypatch,
        capsys,
    )
    assert unknown_seen.code != 0
    assert "out_0000000000000000" in unknown_seen.err

    capture(
        "outputs-parity.txt",
        "ticket 181 - the outputs bytes pair, and the refusal, both halves\n\n"
        f"http GET /v1/outputs/{output_id}\n  {response.content!r}\n"
        f"cli  harnessed outputs get {output_id} --out {out_path}\n  {written!r}\n"
        "parity: equal as bytes\n\n"
        f"http GET /v1/outputs/out_0000000000000000  {unknown.status_code}\n"
        f"cli  harnessed outputs get out_0000000000000000 --out ...  "
        f"exit {unknown_seen.code}  stderr: {unknown_seen.err.strip()}\n",
        EVIDENCE_181,
    )


# ---- FR-37: the same CLI half from a real shell -------------------------------------------------


def shell_env(home: Path) -> dict[str, str]:
    """A fresh operator's environment: everything documented cleared, the home and provider set.

    Subtracted from ``os.environ`` rather than built empty, because a minimal environment on
    Windows loses ``SystemRoot`` and the interpreter will not start (ticket 030).
    """
    env = {key: value for key, value in os.environ.items() if key not in DOCUMENTED_ENV_VARS}
    env["HARNESSED_HOME"] = str(home)
    env["HARNESSED_PROVIDER"] = OFFLINE_PROVIDER
    return env


def in_shell(env: dict[str, str], cwd: Path) -> Invoke:
    """The shelled-out CLI half: ``python -m harnessed``, raw stdout bytes, one terminator.

    ``print`` writes ``os.linesep`` through a piped text stdout, so on Windows the line ends
    ``\\r\\n``. That terminator is asserted to be there exactly once and replaced by the one
    ``assert_parity`` expects; nothing else about the bytes is touched.
    """
    terminator = os.linesep.encode("utf-8")
    # The subprocess pins its clock to the instant this process is frozen at, then runs the real
    # ``python -m harnessed`` entry point. A wall-clock figure (``counted_at``, FR-67) cannot be
    # the same bytes across two processes any other way, and ``clock.freeze`` is the one sanctioned
    # pin (MUST rule 3); nothing else about the interpreter, the argv or the bytes is touched.
    bootstrap = (
        "import runpy, sys; from harnessed import clock; "
        f"clock.freeze(clock.parse({clock.isoformat(clock.now())!r})); "
        "sys.argv[0] = 'harnessed'; runpy.run_module('harnessed', run_name='__main__')"
    )

    def invoke(argv: list[str]) -> bytes:
        completed = subprocess.run(
            [sys.executable, "-c", bootstrap, *argv],
            capture_output=True,
            cwd=cwd,
            env=env,
            check=False,
        )
        assert completed.returncode == 0, (
            f"{argv} exited {completed.returncode}\nstdout:\n{completed.stdout!r}\n"
            f"stderr:\n{completed.stderr!r}"
        )
        stdout = completed.stdout
        if "--json" not in argv:
            # A prose command (``memory add``) seeds and is never compared; its lines are its own.
            return stdout
        assert stdout.endswith(terminator), f"{argv} did not end its line: {stdout!r}"
        body = stdout[: -len(terminator)]
        assert terminator not in body, f"{argv} wrote more than one line: {stdout!r}"
        return body + b"\n"

    return invoke


@pytest.mark.slow
def test_shell_parity(tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI half through a real interpreter, in a fresh home, against the same TestClient body.

    Only the CLI is shelled out: no server subprocess and no ephemeral port (decision 23). The
    fresh home is seeded through the shell too, so nothing in-process has touched its database
    before the gateway reads it. ``clean_env`` clears the documented variables for the in-process
    gateway half too: ``shell_env`` only cleans the shelled-out CLI, so a key exported in the
    operator's shell made ``GET /v1/providers`` read ``configured: true`` where the CLI read false.
    """
    home = tmp_path / "shell" / ".harnessed"
    home.mkdir(parents=True)
    # Neither half may run from the repository root: its ``agents.toml`` and ``.env`` are the
    # operator's, and each half would read them differently (FR-159, decision 71). One empty cwd
    # serves the shelled-out CLI and the in-process gateway alike.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    invoke = in_shell(shell_env(home), cwd)
    seeded = seed_through(invoke)
    seed_prices(home)
    with client_over(home) as client:
        failures = compare_every_pair(invoke, seeded, client, "shell-parity.txt")
    assert not failures, f"{len(failures)} of {len(PARITY_SET)} pairs diverged:\n" + "\n".join(
        failures
    )


# ---- ticket 075: one emitter for an eval summary row --------------------------------------------

EVIDENCE_075 = REPO_ROOT / "evidence" / "075"
SRC_ROOT = REPO_ROOT / "src" / "harnessed"

#: The two places an eval summary row becomes a payload. Both must call the one emitter and
#: neither may spell the row out by hand: a hand-built dict at a call site is how the CLI came to
#: say ``pass_count`` while the gateway said ``passes`` (072's finding).
SUMMARY_CALL_SITES = (
    SRC_ROOT / "cli" / "eval_cmd.py",
    SRC_ROOT / "gateway" / "routes" / "evals.py",
)


def test_both_summary_call_sites_use_the_one_emitter() -> None:
    """AC3, AC4: ``runner.summary_payload`` is the only way a ``RunSummary`` becomes a dict.

    Read as source rather than executed, because the defect is a shape written at a call site,
    and a call site that spells a key by hand is wrong before any row is rendered. The scan is
    scoped to the two surfaces that emit the row; ``pass_count`` stays the name of
    ``scoring.py``'s count and of ``eval show --json``'s key, which are not this row.
    """
    for path in SUMMARY_CALL_SITES:
        source = path.read_text(encoding="utf-8")
        assert "summary_payload" in source, f"{path.name} does not call runner.summary_payload"
        assert "pass_count" not in source, f"{path.name} still spells pass_count by hand"
        assert "asdict(" not in source, f"{path.name} projects the row itself instead of asking"
        keys = {f.name for f in fields(runner.RunSummary)}
        spelled = {key for key in keys if f'"{key}":' in source}
        assert not spelled, f"{path.name} builds a summary dict by hand: {sorted(spelled)}"


def test_eval_list_row_is_byte_identical_and_keyed_from_one_place(
    seeded: Seeded, tmp_harnessed_home: Path
) -> None:
    """AC2: the seventh pair holds as bytes, and the evidence shows the drift it closed.

    The pre-fix CLI line is rebuilt honestly from the same rows - the emitter's payload with
    ``passes`` re-keyed to ``pass_count``, which is exactly what ``list_runs`` used to do - so
    the diff above the two identical bodies is real and regenerated, not pasted.
    """
    pair = next(p for p in PARITY_SET if p.http_path == "/v1/eval/runs")
    conn = db.connect_readonly(tmp_harnessed_home / db.DATABASE_FILENAME)
    try:
        rows = runner.recent(conn, 20)
        application = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
        handler = endpoint_for(application, "/v1/eval/runs")
        assert handler(conn, None) == {"runs": [runner.summary_payload(row) for row in rows]}
    finally:
        conn.close()
    assert rows, "the seeding left no eval run to list"
    for row in rows:
        assert set(runner.summary_payload(row)) == {f.name for f in fields(runner.RunSummary)}

    with client_over(tmp_harnessed_home) as client:
        argv, stdout = cli_half(in_process, seeded, pair)
        path, response = http_half(client, seeded, pair)
    assert_parity(stdout, response)

    def as_it_was(row: runner.RunSummary) -> dict[str, object]:
        payload = runner.summary_payload(row)
        payload["pass_count"] = payload.pop("passes")
        return payload

    before = canonical({"runs": [as_it_was(row) for row in rows]})
    capture(
        "eval-list-parity.txt",
        "\n".join(
            [
                "pre-fix (cli/eval_cmd.py::list_runs hand-built its row, ticket 072's finding):",
                f"- cli:  {before}",
                f"+ http: {response.content.decode()}",
                "",
                "after (both sides call evals/runner.py::summary_payload):",
                f"cli  harnessed {' '.join(argv)}",
                f"  {stdout[:-1].decode()}",
                f"http GET {path}",
                f"  {response.content.decode()}",
                "parity: equal as bytes",
                "",
            ]
        ),
        EVIDENCE_075,
    )


# ---- ticket 096: a zero-case eval run serves a null score, not 0.0 ------------------------------
#
# FR-47, decision 35. The four eval surfaces are two CLI/HTTP pairs over the two emitters, so the
# claim "`score` is null in all four, and each pair is byte-identical" is one bench: plant a run
# that recorded no case beside a run that recorded one, and read both through both halves.

EVIDENCE_096 = REPO_ROOT / "evidence" / "096"

ZERO_CASE_RUN_ID = "eval_zero"
ONE_CASE_RUN_ID = "eval_one"


def plant_eval_run(
    conn: sqlite3.Connection,
    eval_run_id: str,
    *,
    cases: Sequence[tuple[str, float, bool]],
    suite: str = "smoke",
) -> str:
    """One eval run and its scored rows, written straight to the ledger.

    Planted rather than run, because the interesting run here is the one no solver produces: a
    run that opened and recorded nothing. Both runs are planted the same way so the zero-case
    side differs from the measured side in exactly one thing - whether it has rows.
    """
    at = clock.isoformat(clock.now())
    rows: list[tuple[str, tuple[object, ...]]] = [
        (
            "INSERT INTO eval_runs (eval_run_id, suite, label, started_at) VALUES (?, ?, ?, ?)",
            (eval_run_id, suite, "baseline", at),
        )
    ]
    rows += [
        (
            "INSERT INTO eval_results (eval_run_id, case_id, attempt, score, passed, detail, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eval_run_id, case_id, 1, score, int(passed), "[]", at),
        )
        for case_id, score, passed in cases
    ]
    db.write(conn, tuple(rows))
    return eval_run_id


def test_a_zero_case_eval_run_serves_null_on_all_four_surfaces(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC1, AC2: `score` is null in all four, each pair is byte-identical, and a measured run is
    untouched.

    The measured run is read through the same four surfaces in the same bench. Its ``score`` is
    asserted to still be the number the ledger holds, so a change that nulled every score - which
    would satisfy the zero-case half on its own - fails here (MUST rule 12).
    """
    plant_eval_run(conn, ZERO_CASE_RUN_ID, cases=())
    plant_eval_run(conn, ONE_CASE_RUN_ID, cases=(("case-a", 0.75, True),))

    lines: list[str] = []
    with client_over(tmp_harnessed_home) as client:
        list_stdout = cli_bytes(["eval", "list", "--json"])
        list_response = client.get("/v1/eval/runs")
        assert list_response.status_code == 200, list_response.text
        assert_parity(list_stdout, list_response)

        listed = {row["eval_run_id"]: row for row in json.loads(list_stdout)["runs"]}
        assert listed[ZERO_CASE_RUN_ID]["score"] is None, listed[ZERO_CASE_RUN_ID]
        assert listed[ZERO_CASE_RUN_ID]["case_count"] == 0
        assert listed[ONE_CASE_RUN_ID]["score"] == 0.75, "a measured run still serves its score"

        lines += [
            "cli  harnessed eval list --json",
            f"  {list_stdout[:-1].decode()}",
            "http GET /v1/eval/runs",
            f"  {list_response.content.decode()}",
            "parity: equal as bytes",
            "",
        ]

        for eval_run_id, expected in ((ZERO_CASE_RUN_ID, None), (ONE_CASE_RUN_ID, 0.75)):
            argv = ["eval", "show", eval_run_id, "--json"]
            path = f"/v1/eval/runs/{eval_run_id}"
            stdout = cli_bytes(argv)
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert_parity(stdout, response)

            shown = json.loads(stdout)
            assert shown["score"] == expected, f"{eval_run_id} served {shown['score']!r}"
            if expected is None:
                assert b'"score":null' in stdout, stdout
            lines += [
                f"cli  harnessed {' '.join(argv)}",
                f"  {stdout[:-1].decode()}",
                f"http GET {path}",
                f"  {response.content.decode()}",
                "parity: equal as bytes",
                "",
            ]

    capture("zero-case.txt", "\n".join(lines), EVIDENCE_096)


# ---- ticket 131: provider and model ride every run-shaped pair (FR-72) -------------------------

EVIDENCE_131 = REPO_ROOT / "evidence" / "131"

#: The pairs FR-72 names, each with where the two fields sit in its body.
PROVIDER_PAIRS: tuple[tuple[tuple[str, ...], str, Callable[[Any], list[Any]]], ...] = (
    (("run", PROMPT, "--json"), "/v1/runs/{run_key}", lambda body: [body]),
    (("eval", "list", "--json"), "/v1/eval/runs", lambda body: body["runs"]),
    (("eval", "show", "{eval_run_id}", "--json"), "/v1/eval/runs/{eval_run_id}", lambda b: [b]),
    (
        ("eval", "compare", "{eval_run_id}", "{eval_run_id_after}", "--json"),
        "/v1/eval/compare?before={eval_run_id}&after={eval_run_id_after}",
        lambda body: [body["before"], body["after"]],
    ),
    (
        ("improve", "run", "--fresh", "--json"),
        "/v1/improve/cycles/{improvement_run_id}",
        lambda body: [body],
    ),
)


def test_run_eval_and_cycle_pairs_carry_provider_and_model_on_both_halves(
    seeded: Seeded, tmp_harnessed_home: Path
) -> None:
    """AC: ``run_payload``, ``summary_payload`` and ``json_payload`` carry ``provider`` and
    ``model``; CLI and HTTP move together and each pair is still byte-identical."""
    from harnessed.config import load
    from harnessed.providers import registry as provider_registry

    fake_model = provider_registry.model_for(load())
    by_argv = {pair.cli_argv: pair for pair in PARITY_SET}
    lines: list[str] = []
    with client_over(tmp_harnessed_home) as client:
        for argv, route, objects in PROVIDER_PAIRS:
            pair = by_argv.get(argv)
            assert pair is not None and pair.http_path == route, f"{argv} is not a parity row"
            filled, stdout = cli_half(in_process, seeded, pair)
            path, response = http_half(client, seeded, pair)
            assert_parity(stdout, response)
            for side, body in (("cli", json.loads(stdout)), ("http", response.json())):
                for found in objects(body):
                    assert (found["provider"], found["model"]) == ("fake", fake_model), (
                        side,
                        path,
                        found,
                    )
            lines += [f"harnessed {' '.join(filled)}  ==  GET {path}", f"  {response.text}", ""]
    EVIDENCE_131.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_131 / "parity.txt").write_text(
        "ticket 131 - every run-shaped pair carries provider and model, byte-identical\n"
        + "\n".join(lines),
        encoding="utf-8",
    )


# ---- ticket 232 (FR-134): the stream pair joins the byte comparison, a live stream is refused ---


def test_the_stream_pair_writes_the_same_frames_and_refuses_a_live_session(
    seeded: Seeded,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``sessions stream <id> [--after N]`` writes the frames ``GET .../stream`` serves, byte for
    byte, from the start and from a cursor; a session with no finish yet is refused, exit 1."""
    with client_over(tmp_harnessed_home) as client:
        for after in (None, 2):
            argv = ["sessions", "stream", seeded.session_id]
            headers = {}
            if after is not None:
                argv += ["--after", str(after)]
                headers = {"Last-Event-ID": str(after)}
            response = client.get(f"/v1/sessions/{seeded.session_id}/stream", headers=headers)
            assert response.status_code == 200, response.text
            result = cli.invoke(app, argv)
            assert result.exit_code == 0, result.stderr
            assert result.stdout_bytes == response.content, f"{argv} differs from the route"
            assert result.stdout_bytes.startswith(b"id: "), "no frames were written"

    with closing(db.connect()) as conn:
        bus = EventBus(conn=conn)
        bus.publish("run.started", {"provider": "fake"})
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harnessed", "sessions", "stream", bus.run_id])
    with pytest.raises(SystemExit) as exited:
        main()
    captured = capsys.readouterr()
    assert exited.value.code == 1
    assert captured.out == ""
    assert "has not finished" in captured.err


def test_the_new_twins_refuse_an_id_the_ledgers_do_not_hold(
    seeded: Seeded,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unknown = {
        ("memory", "fact", "mem_00000000000000000000000000", "--json"): (
            "/v1/memory/facts/mem_00000000000000000000000000"
        ),
        ("runs", "trace", UNKNOWN_SESSION, "--json"): f"/v1/runs/{UNKNOWN_SESSION}/trace",
        ("sessions", "events", UNKNOWN_SESSION, "--json"): (
            f"/v1/sessions/{UNKNOWN_SESSION}/events"
        ),
        ("improve", "show", "imp_00000000000000000000000000", "--json"): (
            "/v1/improve/cycles/imp_00000000000000000000000000"
        ),
    }
    with client_over(tmp_harnessed_home) as client:
        for argv, path in unknown.items():
            capsys.readouterr()
            monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
            with pytest.raises(SystemExit) as exited:
                main()
            captured = capsys.readouterr()
            assert exited.value.code == 1, argv
            assert captured.out == ""
            assert client.get(path).status_code == 404, path


WORK_SUITE = """\
suite: work-parity
kind: work
description: recorded from real sessions
cases:
  - {id: work-001-a, input: hi, handler: groq_chat, reference: hello, verdict: pending}
  - {id: work-002-b, input: yo, handler: groq_chat, reference: hey, verdict: accept}
"""


def test_the_eval_suites_pair_serves_a_work_suite_and_its_latest_run_on_both_halves(
    seeded: Seeded, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-139: counts are strings, the pending count is the store's, and a suite that never ran
    serves null for every ``latest_*`` field, on the CLI and on the gateway alike."""
    root = tmp_path / "suites"
    root.mkdir()
    (root / "work-parity.yaml").write_text(WORK_SUITE, encoding="utf-8")
    (root / "smoke.yaml").write_bytes((shipped.suites_dir() / "smoke.yaml").read_bytes())
    monkeypatch.setattr(shipped, "suites_dir", lambda: root)
    pair = next(p for p in PARITY_SET if p.http_path == "/v1/eval/suites")

    with client_over(tmp_harnessed_home) as client:
        _, stdout = cli_half(in_process, seeded, pair)
        _, response = http_half(client, seeded, pair)
    assert_parity(stdout, response)

    rows = {row["suite"]: row for row in response.json()["suites"]}
    work = rows["work-parity"]
    assert (work["kind"], work["cases_total"], work["cases_pending"]) == ("work", "2", "1")
    assert work["latest_eval_run_id"] is None and work["latest_score"] is None
    assert work["latest_started_at"] is None and work["latest_label"] is None
    ran = rows["smoke"]
    newest = next(row for row in runner.recent(db.connect(), 20) if row.suite == "smoke")
    assert ran["latest_eval_run_id"] == newest.eval_run_id
    assert isinstance(ran["latest_score"], float) and ran["latest_label"] == newest.label
