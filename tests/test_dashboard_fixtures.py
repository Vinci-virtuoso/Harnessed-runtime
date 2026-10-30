"""FR-45: the dashboard's test inputs are recorded from the real emitters (ticket 100).

Decision 33, MUST rule 24. Every dashboard unit test reads a gateway response recorded from this
side of the wire, so the inputs cannot drift from the emitters that produce them. This file is
the recorder **and** the drift check: it builds one seeded home through the CLI and the packages'
own writers, requests every route and state through ``TestClient``, and compares each response
with the committed files under ``dashboard/tests/fixtures/gateway/``.

The record is three files per case, and which of them exists is decided by what the response is:

* ``<name>.meta.json`` - always. The route requested, the status, the content type. Canonical
  JSON, so a diff on it reads as one line.
* ``<name>.body`` - the response bytes, exactly, for every case whose body is one JSON document.
  ``dashboard/tests/unit/wire-fidelity.test.tsx`` walks precisely this set.
* ``<name>.sse`` - the recorded frames, exactly, for a stream. A stream's bytes are frames and
  not a document, so they are never also written as a ``.body``: a ``.body`` that held an
  ``event:`` line would be a file the wire-fidelity walk could not parse, and the walk reads
  every ``.body`` there is rather than a list somebody maintains.
* ``<name>.bin`` - the served bytes, exactly, for a case whose route answers something other than
  canonical JSON (ticket 181, decision 65's second named exception: ``GET /v1/outputs/{id}``).
  Same reasoning as ``.sse``: a non-JSON ``.body`` is a file both the wire-fidelity and the
  panel-states walks would try to parse as one.

Nothing under that directory is written at test time. ``--record-dashboard-fixtures``, declared
in ``tests/conftest.py``, is the only door, and the comparison run asserts the directory's digest
is unchanged after it has read every file (AC3).

The one normalization is ``/health``'s ``database_path``: it is a temporary directory here and an
operator's home there, so it is replaced by one fixed placeholder on **both** sides. Nothing else
is normalized - not whitespace, not key order, not a float's spelling - because a byte comparison
that normalizes is a subset comparison wearing its name.

Two shapes no solver produces are planted through the packages' own writers rather than run: an
eval run that recorded no case (FR-47) and an improvement run interrupted before it measured
anything (FR-46). Everything else is the CLI an operator types. Hand-written inputs are not
recorded here at all: the transport's unreachable body is ticket 101's and the dropped-connection
sequences are cut from these frames by ticket 111.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sqlite3
import time
import types
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import OFFLINE_PROVIDER, RECORD_DASHBOARD_FIXTURES
from harnessed import clock, config, proc, shipped
from harnessed.acp import PROTOCOL_VERSION, ledger, probes
from harnessed.acp import config as acp_config
from harnessed.acp import outputs as acp_outputs
from harnessed.acp import pending as acp_pending
from harnessed.acp import repo as acp_repo
from harnessed.acp.delegate import request_payload
from harnessed.cli import routed, run_cmd
from harnessed.cli.app import app
from harnessed.config import MODEL_VAR
from harnessed.errors import DelegationError
from harnessed.events import read
from harnessed.events.bus import EventBus
from harnessed.gateway import create_app
from harnessed.improve import steps
from harnessed.loop import runs
from harnessed.memory import consolidation, db, episodic, semantic
from harnessed.providers import pricing, quirks
from harnessed.providers.fake import FakeProvider
from harnessed.providers.fake_decider import FakeDecider
from harnessed.providers.jev import Choice, Decisions, Question
from harnessed.router import decisions as router_decisions
from harnessed.router import route as router_route
from harnessed.serialize import canonical
from harnessed.tasks import schedule as task_schedule
from harnessed.tasks import store as task_store
from harnessed.tools import default_registry
from support.raising_provider import RaisingProvider
from test_gateway_parity import plant_eval_run

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The latency the recorder declares for every decision, and the real delay the test injects.
DECIDER_LATENCY = "0.000"
DELAY_SECONDS = 0.05

#: Where the record lives. Read by ``dashboard/tests/unit/wire-fidelity.test.tsx``; committed.
FIXTURES = REPO_ROOT / "dashboard" / "tests" / "fixtures" / "gateway"

EVIDENCE_DIR = REPO_ROOT / "evidence" / "100"

#: The route whose body names a path that differs per machine, and the one placeholder that
#: stands in for it on both sides of the comparison.
HEALTH_ROUTE = "/v1/health"
DATABASE_PATH_PLACEHOLDER = "<database_path>"

#: An id no ledger holds, in each id's own spelling, for the 404 of every parametrized route.
UNHELD_SESSION = "run_00000000000000000000000000"
UNHELD_EVAL_RUN = "eval_00000000000000000000000000"
UNHELD_IMPROVEMENT_RUN = "imp_00000000000000000000000000"
UNHELD_FACT = "mem_00000000000000000000000000"

#: The prompt the committed fake corpus answers, so ``harnessed run`` completes offline.
PROMPT = "ping"

#: The suite the cycles and the eval runs measure, and the second suite a comparison is refused
#: across (FR-62's suite mismatch).
SUITE = "smoke"
OTHER_SUITE = "regression"

#: One fact, in the operator's own words, and the term that retrieves it.
FACT_TEXT = "The gateway is read-only and answers only GET."
FACT_QUERY = "gateway"

#: The planted comparison pair: a soft drop on one case, a hard regression on another, one case
#: only the before side holds and one only the after side does. Six decimal places on the soft
#: drop, because a rounding pass anywhere between the row and the wire would show in those
#: digits (the pair ticket 098 planted, for the same reason).
PLANTED_BEFORE = "eval_recorded_before"
PLANTED_AFTER = "eval_recorded_after"
PLANTED_ZERO_CASE = "eval_recorded_zero"
PLANTED_OTHER_SUITE = "eval_recorded_other_suite"
BEFORE_CASES = (("case-a", 0.987654, True), ("case-b", 1.0, True), ("case-c", 0.5, True))
AFTER_CASES = (("case-a", 0.876543, True), ("case-b", 0.0, False), ("case-d", 0.9, True))

#: The three homes the record is taken over. The seeded one holds everything an operator would
#: have; the empty one is migrated and holds nothing, which is what makes each list route's
#: empty answer recordable; the third has no database file at all, which is the only honest way
#: to produce decision 24's 500 ``StorageError`` - the gateway never creates the file.
SEEDED, EMPTY, NO_DATABASE = "seeded", "empty", "no-database"

#: A fourth home holding stored prices and nothing else (ticket 147). Its own home because a price
#: in the seeded one would move every recorded ``/v1/spend`` body; the rows go in through the
#: refresh's own ``pricing.parse`` and ``pricing.upserts`` over a recorded catalog, the way
#: ``tests/test_gateway_parity.py::seed_prices`` does, since the refresh itself needs the network.
PRICED = "priced"
RECORDED_CATALOG = REPO_ROOT / "tests" / "fixtures" / "catalogs" / "openrouter.json"

#: The one key the provider recording sees, so ``/v1/providers`` carries a configured keyed row
#: beside the unconfigured ones. A placeholder, set in the environment and never on the wire.
RECORDED_KEY_VAR = "OPENROUTER_API_KEY"
RECORDED_KEY = "sk-or-" + "placeholder-for-the-recording"

#: A fifth home holding recorded agent probes, read beside its own ``agents.toml`` (ticket 148).
#: The registry is read from the working directory, so this home is recorded from a directory of
#: its own and every other home still sees none (``empty.agents`` is the ``[]`` that leaves).
#: Every command is one no ``PATH`` holds, so each fingerprint is taken over a ``None``
#: executable and the recording is the same bytes on every machine.
PROBED = "probed"

#: A sixth home holding consolidation batches and nothing else (ticket 249, FR-152): one done, one
#: failed, one episode still waiting. Its own home because a consolidated fact in the seeded one
#: would move every recorded ``/v1/memory/totals`` and ``/v1/sessions`` body.
CONSOLIDATED = "consolidated"
RECORDED_AGENT_COMMAND = "harnessed-recording-agent-not-on-path"

#: A seventh home holding three tasks and the sessions they ran in, and nothing else (ticket 256,
#: FR-153, FR-154): one paused `permission`, one paused `uncertain`, one `done` unattended. Its own
#: home because a pause points at a `permission_decisions` or a `decisions` row, and either in the
#: seeded home would move `spend.body` and `permissions.pending.body`, which unit tests read.
TASKED = "tasked"

#: An eighth and a ninth home, each one schedule recorded under a v7 backend and nothing else
#: (ticket 319, FR-178): `tasked.schedules.launchd` and `tasked.schedules.systemd`. Their own homes
#: because the tasked home holds one live install and a second would replace it.
LAUNCHD, SYSTEMD = "launchd", "systemd"
#: An eighth home (ticket 316, FR-175): one message the composition ran with no Decider on the fake
#: provider, recorded from an app whose settings hold a Groq key and no OpenRouter key. Its own
#: home because routing is read from the settings at request time and a decision row in the seeded
#: home would move every recorded decisions and spend body.
ROUTING_OFF = "routing-off"
ROUTING_OFF_SESSION = "run_" + "ROUTINGOFF".rjust(25, "0")

#: The session whose message triggered the recorded batches (ticket 249); the events table of the
#: consolidated home never held it, which is the truth of a batch run long after its message.
CONSOLIDATION_TRIGGER = "run_000000000000000000TRIGGER0"

#: One agent per card word, in the registry's order: live, stale, failed, unproven.
# An ``acp`` entry is ``confined`` only with a ``session_meta`` (ticket 213), so each recorded
# agent carries one and the recorded ``confined`` booleans stay as committed.
RECORDED_SESSION_META = {"recorded": {"options": {"settingSources": []}}}
RECORDED_AGENTS = (
    acp_config.AgentSpec(
        name="alpha",
        command=RECORDED_AGENT_COMMAND,
        args=("--acp",),
        session_meta=RECORDED_SESSION_META,
    ),
    acp_config.AgentSpec(
        name="bravo",
        command=RECORDED_AGENT_COMMAND,
        args=("--acp", "--new"),
        session_meta=RECORDED_SESSION_META,
    ),
    acp_config.AgentSpec(
        name="charlie",
        command=RECORDED_AGENT_COMMAND,
        args=("--acp",),
        session_meta=RECORDED_SESSION_META,
    ),
    acp_config.AgentSpec(
        name="delta",
        command=RECORDED_AGENT_COMMAND,
        args=("--acp",),
        session_meta=RECORDED_SESSION_META,
    ),
)


def seed_probes(home: Path, directory: Path) -> None:
    """Write the registry, then record probes the way ``agents test`` does, minus the spawn.

    ``bravo`` passed against its entry before ``--new`` was added, so its proof is stale;
    ``charlie``'s newest probe is a failure; ``delta`` was never tested.
    """
    directory.mkdir()
    (directory / acp_config.DEFAULT_PATH.name).write_text(
        "".join(
            f"[agents.{spec.name}]\n"
            f"command = {json.dumps(spec.command)}\n"
            f"args = {json.dumps(list(spec.args))}\n"
            f"session_meta = {acp_config._toml_value(spec.session_meta)}\n\n"
            for spec in RECORDED_AGENTS
        ),
        encoding="utf-8",
    )
    live, stale, failed, _ = RECORDED_AGENTS
    before = dataclasses.replace(stale, args=("--acp",))
    proofs = (
        (live, probes.PASSED, "The agent completed the handshake and returned a summary."),
        (before, probes.PASSED, "The agent completed the handshake and returned a summary."),
        (failed, probes.FAILED, "the agent answered initialize with protocol version 99"),
    )
    with closing(db.connect(home / db.DATABASE_FILENAME)) as conn:
        for spec, status, detail in proofs:
            probes.record(
                conn,
                agent=spec.name,
                fingerprint=probes.fingerprint(spec, shutil.which(spec.command)),
                status=status,
                protocol_version=PROTOCOL_VERSION,
                detail=detail,
            )


#: The pinned router model text a recorded probe carries, so `/v1/router`'s `model` figure is
#: recorded from the same literal every recording of it reads (ticket 186, FR-121).
ROUTER_MODEL = "typesafe/jev-1.13-20260917"


def seed_consolidation(home: Path) -> None:
    """Two batches through consolidation's own pass, one episode left waiting (ticket 249).

    The first batch is the fake provider's ``_consolidate`` answer, so it writes a fact; the
    second meets a provider that raises, so it settles ``failed`` and writes none. Each settles
    as a one-event session of its own, which is what the sessions listing of this home records.
    """
    settings = dataclasses.replace(config.load(), consolidate_after=2, small_model="fake-1")
    conn = db.connect(home / db.DATABASE_FILENAME)
    try:
        for number in range(5):
            episodic.put(
                conn, f"run_00000000000000000CONSOL{number}", "run", f"the operator note {number}"
            )
        for provider in (FakeProvider(settings), RaisingProvider()):
            consolidation.consolidate(
                conn,
                provider=provider,
                settings=settings,
                now=clock.now(),
                size=2,
                bus_factory=lambda: EventBus(conn=conn),
                session_id=CONSOLIDATION_TRIGGER,
            )
    finally:
        conn.close()


#: The three sessions the tasked home's tasks ran in, sortable and machine-independent (the
#: `TASK_SESSION_ID` style); the task ids are minted under `seeded_ids`.
TASKED_PERMISSION_SESSION = "run_" + "TASKPERM".rjust(25, "0")
TASKED_UNCERTAIN_SESSION = "run_" + "TASKUNSR".rjust(25, "0")
TASKED_DONE_SESSION = "run_" + "TASKDONE".rjust(25, "0")
#: The session of the task that ended ``failed`` (ticket 292): its permission request expired.
TASKED_FAILED_SESSION = "run_" + "TASKFAIL".rjust(25, "0")
#: Two continuations that stopped uncertain at ``done`` (ticket 291, decision 101): one after 281
#: (the stored confidence is ``done``'s own probability) and one before it (a stored 78% beside a
#: ``done`` option priced 82%), each against the task floor of 80%.
TASKED_UNCERTAIN_DONE_SESSION = "run_" + "TASKUDON".rjust(25, "0")
TASKED_UNCERTAIN_CONFIDENCE_SESSION = "run_" + "TASKUCNF".rjust(25, "0")

#: Fixed strings for the two `repo_records` rows: no git process runs, so the bytes are the same
#: on every machine (ticket 256).
RECORDED_HEAD_BEFORE = "1" * 40
RECORDED_HEAD_AFTER = "2" * 40
RECORDED_DIRTY_PATHS = ("notes/todo.md", "notes/ideas.md")


@dataclasses.dataclass(frozen=True)
class Tasked:
    """The ids the tasked home minted, in the order the cases name them."""

    permission_task_id: str
    uncertain_task_id: str
    done_task_id: str
    failed_task_id: str


def tasked_message(
    conn: sqlite3.Connection,
    session_id: str,
    task_id: str,
    text: str,
    *,
    first: bool,
    schedule_id: str | None = None,
) -> tuple[EventBus, int]:
    """One message of a task: `run.started`, the task's own `task_messages` row, and the hop-0
    decision that names its text, all through the writers the task composition calls."""
    bus = EventBus(run_id=session_id, conn=conn)
    seq = bus.publish("run.started", {"provider": OFFLINE_PROVIDER}).seq
    task_store.begin_message(
        conn,
        task_id,
        run_id=session_id,
        message_seq=seq,
        author="task",
        first=first,
        started_by="hand" if schedule_id is None else "schedule",
        schedule_id=schedule_id,
    )
    router_decisions.record_decision(
        conn,
        run_id=session_id,
        message_seq=seq,
        task=text,
        model="recorded-router",
        hop=0,
        latency_seconds=DECIDER_LATENCY,
        outcome="routed",
        route=PLANTED_AGENT,
        handler="agent",
        confidence=Decimal("0.9"),
        floor=Decimal("0.6"),
        needs_memory=None,
        memory_kind=None,
        stop_reason=None,
        fallback_cause=None,
        state_truncated=False,
        answered=None,
    )
    return bus, seq


def tasked_repo_hop(
    conn: sqlite3.Connection,
    bus: EventBus,
    seq: int,
    text: str,
    *,
    summary: str,
    record: acp_repo.Record,
) -> None:
    """One hop's output row and its `repo_records` row, tied by the hop's `task_hash`."""
    task_hash = ledger.task_hash(agent=PLANTED_AGENT, cwd=PLANTED_CWD, task=text, acceptance="")
    acp_outputs.record_hop(
        conn,
        run_id=bus.run_id,
        message_seq=seq,
        hop=0,
        task_hash=task_hash,
        summary=summary,
        cwd=Path.cwd(),
        before={},
        produces=None,
        outputs_glob=None,
    )
    acp_repo.write(conn, task_hash, 1, record)


@dataclasses.dataclass(frozen=True)
class PlantedInstall:
    """The entry a scheduler install row records, from fixed strings: no path of this machine."""

    entry_name: str = "HarnessedTaskRunner"
    python: str = "python"
    arguments: str = "-m harnessed task run --due"
    working_dir: str = "/work/notes"
    fingerprint: str = "0" * 64


def tasked_uncertain_continuation(
    conn: sqlite3.Connection, session_id: str, task_id: str, *, done_probability: Decimal
) -> None:
    """One continuation message whose hop-0 decision routed ``done`` and stopped uncertain with a
    stored confidence of 78% against the 80% floor, through the decisions writer. ``done``'s own
    option is priced ``done_probability``: equal to the stored 78% it is the post-281 shape, apart
    from it the pre-281 one."""
    bus = EventBus(run_id=session_id, conn=conn)
    seq = bus.publish("run.started", {"provider": OFFLINE_PROVIDER}).seq
    conn.execute(
        "INSERT INTO task_messages (run_id, message_seq, task_id, kind) "
        "VALUES (?, ?, ?, 'continuation')",
        (session_id, seq, task_id),
    )
    stored = Decimal("0.78")
    answered = Decisions(
        answers={
            "route": Choice(
                value="done",
                confidence=stored,
                probabilities={"done": done_probability, PLANTED_AGENT: Decimal("0.1")},
            ),
        },
        id="recorded-decision",
        model="recorded-router",
        usage={},
        cost="0.000010",
    )
    router_decisions.record_decision(
        conn,
        run_id=session_id,
        message_seq=seq,
        task="summarise the repository",
        model="recorded-router",
        hop=0,
        latency_seconds=DECIDER_LATENCY,
        outcome="uncertain",
        route="done",
        handler=None,
        confidence=stored,
        floor=Decimal("0.8"),
        needs_memory=Decimal("0.2"),
        memory_kind="episodic",
        stop_reason="below_floor",
        fallback_cause=None,
        state_truncated=False,
        answered=answered,
    )
    bus.publish("run.finished", {"guardrail": None, "stop_reason": "end_turn"})


def seed_schedule_backend(home: Path, where: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """One schedule, advanced once, with a live install recorded under ``where`` (ticket 319).

    The row goes in through the installer's own record writer, as the tasked home's does.
    """
    conn = db.connect(home / db.DATABASE_FILENAME)
    try:
        with monkeypatch.context() as pinned:
            pinned.setattr(clock, "utc_offset_minutes", lambda: 0)
            schedule_id = task_schedule.add(conn, every="6h")["schedule_id"]
        task_schedule.advance(conn, str(schedule_id))
        task_schedule.record_install(conn, where, PlantedInstall())
    finally:
        conn.close()


def seed_routing_off(home: Path) -> None:
    """One message run through the composition with no Decider, on the fake provider (ticket 316).

    Written by ``run_cmd.solve_with_memory`` itself over a session that holds ``decider=None``, so
    ``run.started`` says ``routing: off`` because the composition said it, not because SQL here did.
    """
    settings = config.load(provider=OFFLINE_PROVIDER)
    conn = db.connect(home / db.DATABASE_FILENAME)
    try:
        session = run_cmd.Session(
            settings=settings,
            conn=conn,
            bus=EventBus(run_id=ROUTING_OFF_SESSION, conn=conn),
            tools=default_registry(settings.home),
            provider=FakeProvider(settings),
        )
        run_cmd.solve_with_memory(session, PROMPT)
    finally:
        conn.close()


def seed_tasks(home: Path, monkeypatch: pytest.MonkeyPatch) -> Tasked:
    """Three tasks and their sessions, one of each kind a paused task reads (ticket 256).

    Written through the writers the run itself calls (`task_store`, `acp/pending.py`, the router
    ledger, `acp/repo.py::write`), and the ledger columns 241's planter sets by `UPDATE` are set
    the same way. The clock is advanced a second between the two pauses so `paused_at` orders
    them (permission first), then put back so no later home reads a moved clock.
    """
    started = clock.now()
    conn = db.connect(home / db.DATABASE_FILENAME)
    try:
        permission_text, uncertain_text, done_text = (
            "run the release checks",
            "tidy the notes",
            "summarise the repository",
        )
        permission_id, uncertain_id, done_id = (
            str(task_store.queue(conn, text, via="cli")["task_id"])
            for text in (permission_text, uncertain_text, done_text)
        )
        # `queue` makes the working directory absolute on this host; a recording holds no host path.
        conn.execute("UPDATE tasks SET cwd = ?", (PLANTED_CWD,))
        conn.commit()

        # (a) paused `permission`: the earliest `paused_at`.
        bus, seq = tasked_message(
            conn, TASKED_PERMISSION_SESSION, permission_id, permission_text, first=True
        )
        request = acp_pending.record(
            conn,
            session_id=TASKED_PERMISSION_SESSION,
            agent=PLANTED_AGENT,
            tool_kind="execute",
            description="git log --oneline -20",
            ceiling_at="2026-01-01T12:10:00Z",
            message_seq=1,
            hop=0,
            task_hash=PLANTED_TASK_HASH,
            raw_input={"command": "git log --oneline -20"},
            waiter=PLANTED_WAITER,
        )
        # The live frame, through the payload function `acp/delegate.py` publishes with.
        bus.publish("acp.permission.request", request_payload(PLANTED_AGENT, request))
        task_store.pause_for_permission(conn, permission_id, request.permission_id)
        bus.publish("run.finished", {"guardrail": None, "stop_reason": "end_turn"})

        # (b) paused `uncertain`: a hop refused `dirty_tree`, then the router's own 41% stop.
        clock.freeze(started + timedelta(seconds=1))
        bus, seq = tasked_message(
            conn, TASKED_UNCERTAIN_SESSION, uncertain_id, uncertain_text, first=True
        )
        routed.settle(
            conn,
            run_id=bus.run_id,
            message_seq=seq,
            hop=0,
            result=routed.FAILED,
            reason="dirty_tree",
        )
        tasked_repo_hop(
            conn,
            bus,
            seq,
            uncertain_text,
            summary="the working tree is dirty (reason: dirty_tree)",
            record=acp_repo.refuse(
                acp_repo.Start(
                    cwd=PLANTED_CWD,
                    head=RECORDED_HEAD_BEFORE,
                    head_short="1111111",
                    branch="main",
                    dirty=True,
                    dirty_paths=RECORDED_DIRTY_PATHS,
                )
            ),
        )
        router_decisions.record_decision(
            conn,
            run_id=bus.run_id,
            message_seq=seq,
            task=uncertain_text,
            model="recorded-router",
            hop=1,
            latency_seconds=DECIDER_LATENCY,
            outcome="uncertain",
            route=PLANTED_AGENT,
            handler=None,
            confidence=Decimal("0.41"),
            floor=Decimal("0.6"),
            needs_memory=None,
            memory_kind=None,
            stop_reason=None,
            fallback_cause=None,
            state_truncated=False,
            answered=None,
        )
        task_store.checkpoint(
            conn,
            uncertain_id,
            run_id=bus.run_id,
            message_seq=seq,
            started_at=clock.isoformat(clock.now()),
            nanos=0,
            unpriced=0,
            ended="uncertain",
            uncertain={
                "route": PLANTED_AGENT,
                "confidence": "0.4100",
                "confidence_percent": "41%",
                "floor": "0.6000",
                "floor_percent": "60%",
            },
        )
        bus.publish("run.finished", {"guardrail": None, "stop_reason": "end_turn"})

        # One schedule (pinned to UTC so the bytes are the same on every machine), advanced once so
        # `last_run_at` is served, and one live install row built from fixed strings (ticket 258).
        with monkeypatch.context() as pinned:
            pinned.setattr(clock, "utc_offset_minutes", lambda: 0)
            schedule_id = task_schedule.add(conn, every="6h")["schedule_id"]
        task_schedule.advance(conn, str(schedule_id))
        task_schedule.record_install(conn, "task_scheduler", PlantedInstall())

        # (c) `done` unattended: a first and a continuation message, no human, no pause. Started by
        # the schedule.
        bus, seq = tasked_message(
            conn, TASKED_DONE_SESSION, done_id, done_text, first=True, schedule_id=str(schedule_id)
        )
        tasked_repo_hop(
            conn,
            bus,
            seq,
            done_text,
            summary="two notes were summarised",
            record=acp_repo.Record(
                head_before=RECORDED_HEAD_BEFORE,
                head_after=RECORDED_HEAD_AFTER,
                head_before_short="1111111",
                head_after_short="2222222",
                branch="main",
                diff_stat="2 files changed, 9 insertions(+), 1 deletion(-)",
                paths=("notes/todo.md", "notes/ideas.md"),
                started_dirty=False,
            ),
        )
        routed.settle(conn, run_id=bus.run_id, message_seq=seq, hop=0, result=routed.SUCCEEDED)
        bus.publish("run.finished", {"guardrail": None, "stop_reason": "end_turn"})
        bus, seq = tasked_message(conn, TASKED_DONE_SESSION, done_id, done_text, first=False)
        # The continuation the router routed `done`: nothing left to run, so no reply and a served
        # reason the task is finished (decision 102's `task.done.finished`).
        conn.execute(
            "UPDATE decisions SET route = 'done' WHERE run_id = ? AND message_seq = ?",
            (bus.run_id, seq),
        )
        routed.settle(conn, run_id=bus.run_id, message_seq=seq, hop=0, result=routed.SUCCEEDED)
        bus.publish("run.finished", {"guardrail": None, "stop_reason": "end_turn"})
        conn.execute(
            "UPDATE tasks SET state = 'done', stop_reason = 'finished', "
            "started_at = ?, finished_at = ?, used_iterations = 4, "
            "used_seconds = '12.5', used_dollars = '0.012000', used_nanos = 12000000 "
            "WHERE task_id = ?",
            ("2026-01-01T10:00:00Z", "2026-01-01T10:00:13Z", done_id),
        )
        # (e) a task that ended `failed` / `ceiling` (ticket 292): queued last so no earlier id
        # moves, its permission request expired through the writer a dead waiter's sweep calls,
        # and `checkpoint` reads that ceiling denial and ends the task.
        failed_id = str(task_store.queue(conn, "archive the old logs", via="cli")["task_id"])
        # Queued a second before the other three, so the listing (newest first) puts it last and
        # no row a UI test reads by position moves.
        conn.execute(
            "UPDATE tasks SET cwd = ?, created_at = ? WHERE task_id = ?",
            (PLANTED_CWD, "2026-01-01T11:59:59Z", failed_id),
        )
        conn.commit()
        failed_started = "2026-01-01T12:00:00Z"
        expired = acp_pending.record(
            conn,
            session_id=TASKED_FAILED_SESSION,
            agent=PLANTED_AGENT,
            tool_kind="execute",
            description="rm -rf logs/old",
            ceiling_at="2026-01-01T12:10:00Z",
            message_seq=1,
            hop=0,
            task_hash=PLANTED_TASK_HASH,
        )
        acp_pending.expire(conn, expired.permission_id)
        task_store.checkpoint(
            conn,
            failed_id,
            run_id=TASKED_FAILED_SESSION,
            message_seq=1,
            started_at=failed_started,
            nanos=0,
            unpriced=0,
            ended="ok",
        )
        conn.execute(
            "UPDATE tasks SET started_at = ?, finished_at = ? WHERE task_id = ?",
            ("2026-01-01T12:00:00Z", "2026-01-01T12:00:05Z", failed_id),
        )
        # (d) two continuations that stopped uncertain at `done` (ticket 291), post- and pre-281.
        tasked_uncertain_continuation(
            conn, TASKED_UNCERTAIN_DONE_SESSION, done_id, done_probability=Decimal("0.78")
        )
        tasked_uncertain_continuation(
            conn, TASKED_UNCERTAIN_CONFIDENCE_SESSION, done_id, done_probability=Decimal("0.82")
        )
        conn.commit()
    finally:
        conn.close()
        clock.freeze(started)
    return Tasked(permission_id, uncertain_id, done_id, failed_id)


def seed_router_probe(home: Path, *, status: str, cost: str | None, detail: str) -> None:
    """Append one router probe to ``home``'s ledger, the way ``harnessed doctor --live`` does."""
    with closing(db.connect(home / db.DATABASE_FILENAME)) as conn:
        router_decisions.record_probe(
            conn,
            model=ROUTER_MODEL,
            status=status,
            latency_seconds="0.842",
            cost=cost,
            detail=detail,
        )


pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")

cli = CliRunner(mix_stderr=False)


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- the record's file layout -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Case:
    """One recorded request: what to ask, over which home, and under what name."""

    name: str
    home: str
    path: str
    stream: bool = False
    #: The route answers bytes rather than canonical JSON (ticket 181): recorded as ``.bin``,
    #: never ``.body``, so the JSON-only walks (wire-fidelity, panel-states) skip it.
    binary: bool = False
    headers: Mapping[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Recorded:
    """One response as the record carries it: the meta, and the bytes under one of three names."""

    case: Case
    status: int
    content_type: str
    payload: bytes
    #: What arrived on the wire, before the one normalization. Never written to disk; it is what
    #: makes "only the database path is normalized" a checkable claim rather than a promise.
    raw: bytes = b""

    @property
    def meta(self) -> dict[str, object]:
        """The three facts about the exchange that are not the bytes themselves."""
        return {
            "content_type": self.content_type,
            "route": self.case.path,
            "status": self.status,
        }

    @property
    def payload_suffix(self) -> str:
        """``.sse`` for a stream's frames, ``.bin`` for non-JSON bytes, ``.body`` otherwise."""
        if self.case.stream:
            return ".sse"
        if self.case.binary:
            return ".bin"
        return ".body"

    def files(self) -> dict[str, bytes]:
        """Every file this case owns, keyed by name relative to the fixture directory."""
        return {
            f"{self.case.name}.meta.json": (canonical(self.meta) + "\n").encode("utf-8"),
            f"{self.case.name}{self.payload_suffix}": self.payload,
        }


def digest_of(directory: Path) -> str:
    """A hash over every file in ``directory``, name and bytes, so "unchanged" is checkable."""
    running = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            running.update(path.relative_to(directory).as_posix().encode("utf-8"))
            running.update(path.read_bytes())
    return running.hexdigest()


def write_record(recorded: Sequence[Recorded], directory: Path) -> list[str]:
    """Rewrite ``directory`` from ``recorded``. Only ever reached under the option.

    The directory is emptied first, so a case that was renamed or dropped leaves no orphan
    behind for the wire-fidelity walk to keep reading.
    """
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    written: list[str] = []
    for item in recorded:
        for name, payload in item.files().items():
            (directory / name).write_bytes(payload)
            written.append(name)
    return sorted(written)


def mismatches(recorded: Recorded, directory: Path) -> list[str]:
    """Every disagreement between ``recorded`` and the committed files, each naming its file."""
    found: list[str] = []
    for name, expected in recorded.files().items():
        path = directory / name
        if not path.is_file():
            found.append(f"{name}: not recorded. Re-record with {RECORD_DASHBOARD_FIXTURES}.")
            continue
        actual = path.read_bytes()
        if actual != expected:
            found.append(
                f"{name}: the emitter no longer produces the recorded bytes.\n"
                f"  recorded: {actual!r}\n"
                f"  emitted:  {expected!r}\n"
                f"  Change the emitter, then re-record with {RECORD_DASHBOARD_FIXTURES}."
            )
    return found


# ---- the one normalization ----------------------------------------------------------------------


def normalized(body: bytes, database_path: Path) -> bytes:
    """``body`` with the database path replaced by the placeholder, and nothing else touched.

    The path is a temporary directory here and an operator's home there, so it is the one value
    in the whole record that cannot be recorded as sent. Two bodies name it: ``/health``, whose
    ``database_path`` **is** the path, and the 500 ``StorageError``, whose message quotes it
    inside a sentence. So the substitution is made on the path's JSON spelling with the quotes
    stripped - the escaping the encoder chose, matched wherever it appears - rather than on the
    whole value, and the replacement is still one fixed placeholder. Nothing else is normalized,
    which ``test_the_database_path_is_the_only_value_normalized`` checks by finding the raw
    response and the recorded one identical everywhere else.
    """
    spelled = canonical(str(database_path))[1:-1].encode("utf-8")
    return body.replace(spelled, DATABASE_PATH_PLACEHOLDER.encode("utf-8"))


# ---- the seeded home ----------------------------------------------------------------------------


def run_cli(argv: list[str]) -> str:
    """One CLI command, asserted to have succeeded and written nothing to stderr."""
    result = cli.invoke(app, argv)
    assert result.exit_code == 0, f"{argv} exited {result.exit_code}\n{result.stderr}"
    assert result.stderr == "", f"{argv} wrote to stderr: {result.stderr}"
    return result.stdout


def sessions_held(conn: sqlite3.Connection) -> set[str]:
    """Every session id the events table holds, through the events package's own listing."""
    return {row.session_id for row in read.sessions(conn, limit=1000)}


@dataclasses.dataclass(frozen=True)
class Seeded:
    """The ids the seeding minted, captured as it ran rather than guessed from a listing."""

    session_id: str
    case_run_key: str
    eval_run_ids: tuple[str, ...]
    zero_case_eval_run_id: str
    promoted_cycle_id: str
    dry_run_cycle_id: str
    undecided_cycle_id: str
    baseline_only_cycle_id: str
    improve_session_id: str
    fact_id: str
    output_id: str
    failed_session_id: str
    #: The planted `done` task (ticket 241), the one whose session `?session=` answers.
    task_id: str


@contextmanager
def frozen_decider_timer() -> Iterator[None]:
    """Hold the router's stopwatch still, so a recorded decision carries ``DECIDER_LATENCY``.

    ``router/route.py`` times the decider with ``time.perf_counter`` and stores the elapsed
    seconds, so a recording would otherwise hold how fast the recording machine was (ticket 225).
    Only the router's own ``time`` name is swapped: the interpreter's clock stays real.
    """
    still = types.SimpleNamespace(perf_counter=lambda: 0.0)
    real = router_route.time
    router_route.time = still  # type: ignore[assignment]
    try:
        yield
    finally:
        router_route.time = real


def seed(home: Path) -> Seeded:
    """Everything an operator would have done, in one fixed order, over ``home``.

    The CLI does the work wherever the CLI can do it. The two shapes it cannot produce - an eval
    run that recorded no case, and an improvement run interrupted before it measured anything -
    are written through ``evals``' and ``improve``'s own writers, never by SQL spelled here.
    """
    run_cli(["memory", "add", FACT_TEXT])
    conn = db.connect()
    try:
        (fact,) = semantic.search(conn, FACT_QUERY, 1)
    finally:
        conn.close()

    with frozen_decider_timer():
        ran = run_cli(["run", PROMPT, "--json"])
    session_id = str(json.loads(ran)["run_key"])

    eval_run_ids = tuple(
        str(json.loads(run_cli(["eval", "run", SUITE, "--fresh", "--json"]))["eval_run_id"])
        for _ in range(2)
    )

    run_cli(["improve", "run", "--suite", SUITE, "--dry-run", "--fresh", "--json"])

    conn = db.connect()
    try:
        before_improve = sessions_held(conn)
        dry_run_cycle_id = str(steps.latest(conn, SUITE))
    finally:
        conn.close()

    promoted = run_cli(["improve", "run", "--suite", SUITE, "--fresh", "--json"])
    promoted_cycle_id = str(json.loads(promoted)["improvement_run_id"])
    assert json.loads(promoted)["promoted"] is True, promoted

    conn = db.connect()
    try:
        (improve_session_id,) = sessions_held(conn) - before_improve

        zero_case = plant_eval_run(conn, PLANTED_ZERO_CASE, cases=())
        plant_eval_run(conn, PLANTED_BEFORE, cases=BEFORE_CASES)
        plant_eval_run(conn, PLANTED_AFTER, cases=AFTER_CASES)
        plant_eval_run(conn, PLANTED_OTHER_SUITE, cases=(("case-a", 1.0, True),), suite=OTHER_SUITE)

        # A delegation output (ticket 181, FR-127), planted through `acp/outputs.py`'s own writer
        # rather than by SQL spelled here, on the run the CLI already seeded.
        (output_id,) = acp_outputs.record_hop(
            conn,
            run_id=session_id,
            message_seq=1,
            hop=0,
            task_hash=None,
            summary="ticket 181 - a planted delegation output, for the dashboard fixture record",
            cwd=Path.cwd(),
            before={},
            produces=None,
            outputs_glob=None,
        )

        undecided_cycle_id, _ = steps.start(conn, SUITE, fresh=True)
        baseline = steps.get(conn, promoted_cycle_id, steps.BASELINE, SUITE)
        assert baseline is not None, "the promoted cycle recorded no baseline to copy"
        baseline_only_cycle_id, _ = steps.start(conn, SUITE, fresh=True)
        steps.put(conn, baseline_only_cycle_id, steps.BASELINE, SUITE, baseline.payload)

        case_run_key = next(
            row.run_key for row in runs.listing(conn, limit=1000) if row.key_kind == "case_run"
        )
        failed_session_id = plant_failed_hop(conn)
        plant_permissions(conn, session_id)
        task_id = plant_tasks(conn)
    finally:
        conn.close()

    return Seeded(
        session_id=session_id,
        case_run_key=case_run_key,
        eval_run_ids=eval_run_ids,
        zero_case_eval_run_id=zero_case,
        promoted_cycle_id=promoted_cycle_id,
        dry_run_cycle_id=dry_run_cycle_id,
        undecided_cycle_id=undecided_cycle_id,
        baseline_only_cycle_id=baseline_only_cycle_id,
        improve_session_id=improve_session_id,
        fact_id=fact.mem_id,
        output_id=output_id,
        failed_session_id=failed_session_id,
        task_id=task_id,
    )


#: Sorts before every minted id, so the planted session lists last and the newest session, the
#: latest run and the recorded stream's session stay the ones the record already held.
FAILED_SESSION_ID = "run_000000000000000000FAILED00"


def plant_failed_hop(conn: sqlite3.Connection) -> str:
    """A message whose agent hop wrote files and was refused (ticket 232, migration 018).

    Planted through the writers the run itself calls: ``run.started``, the router's own
    ``record_decision`` for an agent route, ``routed.settle`` for the failed hop, then the pair
    ``settle_raised`` publishes. A session is the only shape the CLI cannot produce offline, since
    no recorded agent writes a file inside a read-only working directory.
    """
    bus = EventBus(run_id=FAILED_SESSION_ID, conn=conn)
    announced = bus.publish("run.started", {"provider": OFFLINE_PROVIDER})
    router_decisions.record_decision(
        conn,
        run_id=bus.run_id,
        message_seq=announced.seq,
        task="summarise the repository",
        model="recorded-router",
        hop=0,
        latency_seconds=DECIDER_LATENCY,
        outcome="routed",
        route="clau" + "de-code",
        handler="agent",
        confidence=Decimal("0.9"),
        floor=Decimal("0.5"),
        needs_memory=None,
        memory_kind=None,
        stop_reason=None,
        fallback_cause=None,
        state_truncated=False,
        answered=None,
    )
    routed.settle(
        conn,
        run_id=bus.run_id,
        message_seq=announced.seq,
        hop=0,
        result=routed.FAILED,
        reason="wrote_files",
    )
    run_cmd.settle_raised(bus, DelegationError("the agent wrote files (reason: wrote_files)"))
    return bus.run_id


#: The agent a planted permission request names: an entry name, which is data and never a branch.
PLANTED_AGENT = "recorded-agent"
#: What a planted request holds of the delegation and the waiting process (ticket 288): a recording
#: carries no measured value (MUST rule 36), so the waiter is named here and never read.
PLANTED_TASK_HASH = "0123456789abcdef0123456789abcdef"
PLANTED_WARNING = "ask degraded to allowlist: no one could be asked"
PLANTED_WAITER = proc.Identity(4242, "2026-01-01T12:00:00Z")


def plant_permissions(conn: sqlite3.Connection, session_id: str) -> None:
    """One permission decision per state, through ``acp/pending.py``'s own writers (ticket 234).

    ``pending`` and ``expired`` carry a ceiling; ``allowed`` and ``refused`` are the configuration's
    own answers and carry none; ``approved`` and ``denied`` are an operator's, one from each door.
    """
    ceiling = "2026-01-01T12:10:00Z"
    plan = (
        ("pending", {"ceiling_at": ceiling}),
        ("approved", {"ceiling_at": ceiling}),
        ("denied", {"ceiling_at": ceiling}),
        ("expired", {"ceiling_at": ceiling}),
        ("allowed", {"answer": "approve", "rule": "allowlist", "warning": PLANTED_WARNING}),
        ("refused", {"answer": "deny", "rule": "deny_list"}),
    )
    rows = {
        name: acp_pending.record(
            conn,
            session_id=session_id,
            agent=PLANTED_AGENT,
            tool_kind="execute",
            description=f"a planted {name} request",
            message_seq=1,
            hop=0,
            task_hash=PLANTED_TASK_HASH,
            raw_input={"command": f"echo {name}"},
            waiter=PLANTED_WAITER,
            **fields,  # type: ignore[arg-type]
        )
        for name, fields in plan
    }
    acp_pending.answer(
        conn, rows["approved"].permission_id, "approve", rule="operator", via="console"
    )
    acp_pending.answer(conn, rows["denied"].permission_id, "deny", rule="operator", via="cli")
    acp_pending.answer(conn, rows["expired"].permission_id, "deny", rule="ceiling", via=None)


#: The session the planted `done` task owns. It is not in the events table: a task's session is
#: its own column, and `GET /v1/tasks?session=` answers from the ledger.
TASK_SESSION_ID = "run_00000000000000000TASKED00"
PLANTED_CWD = "/work/notes"


def plant_tasks(conn: sqlite3.Connection) -> str:
    """Two tasks through the store's own writer (ticket 241): one still `queued` and one `done`
    with no human message and no pause. Returns the `done` task's id.

    A paused task is not planted: its pause fields point at a `decisions` or a
    `permission_decisions` row, and either would move `spend.body` or `permissions.pending.body`,
    which the dashboard's unit tests read. `tests/test_gateway_tasks.py` proves both pauses.
    The ledger columns a checkpoint writes are set directly, because running a task is ticket
    240's and a recording of the read needs the rows, not a run.
    """
    task_store.queue(conn, "tidy the notes", via="cli")
    done = str(task_store.queue(conn, "summarise the repository", via="console")["task_id"])
    conn.execute(
        "UPDATE tasks SET state = 'done', stop_reason = 'finished', session_id = ?, "
        "started_by = 'hand', started_at = ?, finished_at = ?, used_iterations = 4, "
        "used_seconds = '12.5', used_dollars = '0.012000', used_nanos = 12000000 "
        "WHERE task_id = ?",
        (TASK_SESSION_ID, "2026-01-01T10:00:00Z", "2026-01-01T10:00:13Z", done),
    )
    # `queue` makes the working directory absolute on this host; a recording holds no host path.
    conn.execute("UPDATE tasks SET cwd = ?", (PLANTED_CWD,))
    conn.commit()
    return done


def cases_for(seeded: Seeded, tasked: Tasked) -> tuple[Case, ...]:
    """Every route and state the record covers, as data.

    Read as a table: the 200 of every registered route, each list route's empty answer, each
    parametrized route's 404, a malformed ``?limit=``, the 500 from the home with no database,
    the four comparison refusals, and the two stream shapes.
    """
    return (
        # ---- the seeded home: every route answering 200 ----------------------------------------
        Case("health", SEEDED, HEALTH_ROUTE),
        Case("runs.totals", SEEDED, "/v1/runs/totals"),
        Case("runs.turn-seconds", SEEDED, "/v1/runs/turn_seconds"),
        Case("spend", SEEDED, "/v1/spend"),
        Case("spend.session", SEEDED, f"/v1/spend?session={seeded.session_id}"),
        Case("events.totals", SEEDED, "/v1/events/totals"),
        Case("events.gate", SEEDED, "/v1/events/gate"),
        Case("memory.totals", SEEDED, "/v1/memory/totals"),
        Case("memory.consolidation", CONSOLIDATED, "/v1/memory/consolidation"),
        Case("sessions.listing.consolidated", CONSOLIDATED, "/v1/sessions"),
        Case("providers", SEEDED, "/v1/providers"),
        Case("agents", PROBED, "/v1/agents"),
        Case("router", SEEDED, "/v1/router"),
        Case("router.probed", PROBED, "/v1/router"),
        Case("router.failed", EMPTY, "/v1/router"),
        Case("router.routing-off", ROUTING_OFF, "/v1/router"),
        Case(
            "sessions.messages.routing-off",
            ROUTING_OFF,
            f"/v1/sessions/{ROUTING_OFF_SESSION}/messages",
        ),
        Case("prices", PRICED, "/v1/prices"),
        Case("outputs.get", SEEDED, f"/v1/outputs/{seeded.output_id}", binary=True),
        Case("runs.listing", SEEDED, "/v1/runs"),
        Case("runs.session", SEEDED, f"/v1/runs/{seeded.session_id}"),
        Case("runs.case-run", SEEDED, f"/v1/runs/{seeded.case_run_key}"),
        Case("runs.trace", SEEDED, f"/v1/runs/{seeded.session_id}/trace"),
        Case("eval.runs.listing", SEEDED, "/v1/eval/runs"),
        Case("eval.suites.listing", SEEDED, "/v1/eval/suites"),
        Case("eval.run.measured", SEEDED, f"/v1/eval/runs/{seeded.eval_run_ids[0]}"),
        Case("eval.run.zero-case", SEEDED, f"/v1/eval/runs/{seeded.zero_case_eval_run_id}"),
        Case(
            "eval.compare.regression",
            SEEDED,
            f"/v1/eval/compare?before={PLANTED_BEFORE}&after={PLANTED_AFTER}",
        ),
        Case(
            "eval.compare.zero-case-side",
            SEEDED,
            f"/v1/eval/compare?before={PLANTED_ZERO_CASE}&after={PLANTED_AFTER}",
        ),
        Case("improve.cycle.promoted", SEEDED, f"/v1/improve/cycles/{seeded.promoted_cycle_id}"),
        Case("improve.cycle.dry-run", SEEDED, f"/v1/improve/cycles/{seeded.dry_run_cycle_id}"),
        Case("improve.cycle.undecided", SEEDED, f"/v1/improve/cycles/{seeded.undecided_cycle_id}"),
        Case(
            "improve.cycle.baseline-only",
            SEEDED,
            f"/v1/improve/cycles/{seeded.baseline_only_cycle_id}",
        ),
        Case("promotions", SEEDED, "/v1/promotions"),
        Case("memory.facts", SEEDED, f"/v1/memory/facts?q={FACT_QUERY}"),
        Case("memory.fact", SEEDED, f"/v1/memory/facts/{seeded.fact_id}"),
        Case("memory.episodes", SEEDED, f"/v1/memory/episodes/{seeded.session_id}"),
        Case("memory.procedures", SEEDED, "/v1/memory/procedures"),
        Case("sessions.listing", SEEDED, "/v1/sessions"),
        Case("sessions.events", SEEDED, f"/v1/sessions/{seeded.session_id}/events"),
        Case("sessions.decisions", SEEDED, f"/v1/sessions/{seeded.session_id}/decisions"),
        Case("sessions.messages", SEEDED, f"/v1/sessions/{seeded.session_id}/messages"),
        Case(
            "sessions.messages.unrouted",
            SEEDED,
            f"/v1/sessions/{seeded.improve_session_id}/messages",
        ),
        Case(
            "sessions.messages.failed",
            SEEDED,
            f"/v1/sessions/{seeded.failed_session_id}/messages",
        ),
        Case("messages.recent", SEEDED, "/v1/messages"),
        Case("permissions.pending", SEEDED, "/v1/permissions?state=pending"),
        Case("permissions.session", SEEDED, f"/v1/permissions?session={seeded.session_id}"),
        Case("tasks.listing", SEEDED, "/v1/tasks"),
        Case("tasks.needs-human", SEEDED, "/v1/tasks?state=needs_human"),
        Case("tasks.session", SEEDED, f"/v1/tasks?session={TASK_SESSION_ID}"),
        Case("tasks.session.none", SEEDED, f"/v1/tasks?session={seeded.session_id}"),
        Case("tasks.totals", SEEDED, "/v1/tasks/totals"),
        Case("tasks.done", SEEDED, f"/v1/tasks/{seeded.task_id}"),
        Case("schedules.listing", SEEDED, "/v1/schedules"),
        Case("sessions.stream", SEEDED, f"/v1/sessions/{seeded.session_id}/stream", stream=True),
        Case(
            "improve.sessions.stream",
            SEEDED,
            f"/v1/sessions/{seeded.improve_session_id}/stream",
            stream=True,
        ),
        # ---- the seeded home: absence and refusal ----------------------------------------------
        Case("notfound.runs.run", SEEDED, f"/v1/runs/{UNHELD_SESSION}"),
        Case("notfound.spend.session", SEEDED, f"/v1/spend?session={UNHELD_SESSION}"),
        Case("notfound.runs.trace", SEEDED, f"/v1/runs/{UNHELD_SESSION}/trace"),
        Case("notfound.eval.run", SEEDED, f"/v1/eval/runs/{UNHELD_EVAL_RUN}"),
        Case("notfound.improve.cycle", SEEDED, f"/v1/improve/cycles/{UNHELD_IMPROVEMENT_RUN}"),
        Case("notfound.memory.fact", SEEDED, f"/v1/memory/facts/{UNHELD_FACT}"),
        Case("notfound.memory.episodes", SEEDED, f"/v1/memory/episodes/{UNHELD_SESSION}"),
        Case("notfound.sessions.events", SEEDED, f"/v1/sessions/{UNHELD_SESSION}/events"),
        Case("notfound.sessions.decisions", SEEDED, f"/v1/sessions/{UNHELD_SESSION}/decisions"),
        Case("notfound.sessions.messages", SEEDED, f"/v1/sessions/{UNHELD_SESSION}/messages"),
        Case("notfound.sessions.stream", SEEDED, f"/v1/sessions/{UNHELD_SESSION}/stream"),
        Case("notfound.permissions.session", SEEDED, f"/v1/permissions?session={UNHELD_SESSION}"),
        Case("notfound.tasks.session", SEEDED, f"/v1/tasks?session={UNHELD_SESSION}"),
        Case("notfound.tasks.task", SEEDED, "/v1/tasks/task_00000000000000000000000000"),
        Case("notfound.route", SEEDED, "/v1/nothing-answers-here"),
        Case(
            "notfound.eval.compare.unknown-before",
            SEEDED,
            f"/v1/eval/compare?before={UNHELD_EVAL_RUN}&after={PLANTED_AFTER}",
        ),
        Case(
            "notfound.eval.compare.unknown-after",
            SEEDED,
            f"/v1/eval/compare?before={PLANTED_BEFORE}&after={UNHELD_EVAL_RUN}",
        ),
        Case("badrequest.runs.limit", SEEDED, "/v1/runs?limit=nought"),
        Case("badrequest.sessions.limit", SEEDED, "/v1/sessions?limit=0"),
        Case(
            "badrequest.eval.compare.suite-mismatch",
            SEEDED,
            f"/v1/eval/compare?before={PLANTED_BEFORE}&after={PLANTED_OTHER_SUITE}",
        ),
        Case(
            "badrequest.eval.compare.missing-parameter",
            SEEDED,
            f"/v1/eval/compare?before={PLANTED_BEFORE}",
        ),
        Case(
            "badrequest.sessions.stream.cursor",
            SEEDED,
            f"/v1/sessions/{seeded.session_id}/stream",
            headers={"Last-Event-ID": "not-a-cursor"},
        ),
        Case("badrequest.messages.limit", SEEDED, "/v1/messages?limit=0"),
        Case("badrequest.permissions.state", SEEDED, "/v1/permissions?state=maybe"),
        Case("badrequest.tasks.state", SEEDED, "/v1/tasks?state=maybe"),
        Case("badrequest.tasks.limit", SEEDED, "/v1/tasks?limit=0"),
        # ---- the empty home: every list route's empty answer -----------------------------------
        Case("empty.runs.listing", EMPTY, "/v1/runs"),
        Case("empty.eval.runs.listing", EMPTY, "/v1/eval/runs"),
        # The seeded work suites over a ledger that never ran any: every `latest_*` is null.
        Case("eval.suites.never-run", EMPTY, "/v1/eval/suites"),
        Case("empty.sessions.listing", EMPTY, "/v1/sessions"),
        Case("empty.promotions", EMPTY, "/v1/promotions"),
        Case("empty.memory.facts", EMPTY, f"/v1/memory/facts?q={FACT_QUERY}"),
        Case("empty.memory.procedures", EMPTY, "/v1/memory/procedures"),
        # The six totals over the empty home: a served "0" per count, and each documented null.
        Case("empty.spend", EMPTY, "/v1/spend"),
        Case("empty.runs.turn-seconds", EMPTY, "/v1/runs/turn_seconds"),
        Case("empty.runs.totals", EMPTY, "/v1/runs/totals"),
        Case("empty.events.totals", EMPTY, "/v1/events/totals"),
        Case("empty.events.gate", EMPTY, "/v1/events/gate"),
        Case("empty.memory.totals", EMPTY, "/v1/memory/totals"),
        Case("empty.memory.consolidation", EMPTY, "/v1/memory/consolidation"),
        Case("empty.agents", EMPTY, "/v1/agents"),
        Case("empty.prices", EMPTY, "/v1/prices"),
        Case("empty.messages.recent", EMPTY, "/v1/messages"),
        Case("empty.permissions", EMPTY, "/v1/permissions?state=pending"),
        Case("empty.tasks", EMPTY, "/v1/tasks"),
        Case("empty.tasks.totals", EMPTY, "/v1/tasks/totals"),
        Case("empty.schedules", EMPTY, "/v1/schedules"),
        # ---- the tasked home: a paused task of each kind and a task's own messages (ticket 256) -
        Case("tasked.tasks.listing", TASKED, "/v1/tasks"),
        Case("tasked.tasks.needs-human", TASKED, "/v1/tasks?state=needs_human"),
        Case("tasked.tasks.totals", TASKED, "/v1/tasks/totals"),
        Case("tasked.tasks.permission", TASKED, f"/v1/tasks/{tasked.permission_task_id}"),
        Case("tasked.tasks.uncertain", TASKED, f"/v1/tasks/{tasked.uncertain_task_id}"),
        Case("tasked.tasks.done", TASKED, f"/v1/tasks/{tasked.done_task_id}"),
        Case("tasked.tasks.failed", TASKED, "/v1/tasks?state=failed"),
        Case("tasked.schedules.listing", TASKED, "/v1/schedules"),
        Case("tasked.schedules.launchd", LAUNCHD, "/v1/schedules"),
        Case("tasked.schedules.systemd", SYSTEMD, "/v1/schedules"),
        Case("tasked.messages.recent", TASKED, "/v1/messages"),
        Case(
            "tasked.sessions.messages.permission",
            TASKED,
            f"/v1/sessions/{TASKED_PERMISSION_SESSION}/messages",
        ),
        Case(
            "tasked.sessions.messages.uncertain",
            TASKED,
            f"/v1/sessions/{TASKED_UNCERTAIN_SESSION}/messages",
        ),
        Case(
            "tasked.sessions.decisions.uncertain-done",
            TASKED,
            f"/v1/sessions/{TASKED_UNCERTAIN_DONE_SESSION}/decisions",
        ),
        Case(
            "tasked.sessions.decisions.uncertain-confidence",
            TASKED,
            f"/v1/sessions/{TASKED_UNCERTAIN_CONFIDENCE_SESSION}/decisions",
        ),
        Case(
            "tasked.sessions.messages.done",
            TASKED,
            f"/v1/sessions/{TASKED_DONE_SESSION}/messages",
        ),
        Case(
            "tasked.sessions.messages.finished",
            TASKED,
            f"/v1/sessions/{TASKED_DONE_SESSION}/messages",
        ),
        Case(
            "tasked.sessions.stream.permission",
            TASKED,
            f"/v1/sessions/{TASKED_PERMISSION_SESSION}/stream",
            stream=True,
        ),
        Case(
            "tasked.permissions.permission",
            TASKED,
            f"/v1/permissions?session={TASKED_PERMISSION_SESSION}",
        ),
        # ---- the home with no database: decision 24's 500 --------------------------------------
        Case("nodatabase.runs.listing", NO_DATABASE, "/v1/runs"),
    )


def exchange(client: TestClient, case: Case, database_path: Path) -> Recorded:
    """One exchange, as the record will carry it, beside the bytes that actually arrived."""
    response = client.get(case.path, headers=dict(case.headers))
    return Recorded(
        case=case,
        status=response.status_code,
        content_type=response.headers["content-type"],
        payload=normalized(response.content, database_path),
        raw=response.content,
    )


@dataclasses.dataclass(frozen=True)
class Bench:
    """The three homes, and every response taken over them in one pass."""

    seeded: Seeded
    tasked: Tasked
    recorded: tuple[Recorded, ...]

    def by_name(self, name: str) -> Recorded:
        return next(item for item in self.recorded if item.case.name == name)


@pytest.fixture
def bench(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Bench:
    """Seed the homes, then walk every case over each one. Built once per test that asks.

    The model is pinned to the fake provider's own default (ticket 131). ``clean_env`` clears the
    environment, but ``config.load`` still reads the checkout's ``.env``, and from FR-72 on the
    model id is on the wire: without the pin an operator's ``HARNESSED_MODEL`` lands in a
    committed recording and the drift check fails on every machine but theirs.
    """
    monkeypatch.setenv(MODEL_VAR, str(quirks.default_model_for(OFFLINE_PROVIDER)))
    seeded = seed(tmp_harnessed_home)
    seed_work_suites(tmp_path / "suites", monkeypatch)

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    db.connect(empty_home / db.DATABASE_FILENAME).close()
    # The empty home's own router_probes row (ticket 186): a failed probe is the newest and only
    # one, so `/v1/router` over it reads `probed: true, status: "failed"` while every other empty
    # list route on this home stays empty - inserting into a table no other case here reads.
    seed_router_probe(
        empty_home, status=router_decisions.FAILED, cost=None, detail="fail: DecisionTimeout"
    )

    no_database_home = tmp_path / "no-database-home"
    no_database_home.mkdir()

    priced_home = tmp_path / "priced-home"
    priced_home.mkdir()
    catalog = pricing.parse(
        "openrouter",
        RECORDED_CATALOG.read_bytes(),
        source_url=pricing.catalog_url("openrouter"),
        fetched_at=clock.isoformat(clock.now()),
    )
    priced = db.connect(priced_home / db.DATABASE_FILENAME)
    try:
        db.write(priced, pricing.upserts(catalog))
    finally:
        priced.close()

    probed_home = tmp_path / "probed-home"
    probed_home.mkdir()
    registry_dir = tmp_path / "probed-registry"
    seed_probes(probed_home, registry_dir)
    seed_router_probe(
        probed_home,
        status=router_decisions.PASSED,
        cost="0.0031",
        detail="pass: latency 0.842s, cost 0.0031",
    )

    consolidated_home = tmp_path / "consolidated-home"
    consolidated_home.mkdir()
    seed_consolidation(consolidated_home)

    # Seeded last, so no id minted before it (every other home's) moves (ticket 256).
    tasked_home = tmp_path / "tasked-home"
    tasked_home.mkdir()
    tasked = seed_tasks(tasked_home, monkeypatch)

    backend_homes = {}
    for backend in (LAUNCHD, SYSTEMD):
        backend_homes[backend] = tmp_path / f"{backend}-home"
        backend_homes[backend].mkdir()
        seed_schedule_backend(backend_homes[backend], backend, monkeypatch)
    routing_off_home = tmp_path / "routing-off-home"
    routing_off_home.mkdir()
    seed_routing_off(routing_off_home)

    # `/v1/providers` reads the environment through `config.load`, which also reads `.env` in the
    # working directory: the operator's own file must not reach a committed recording (ticket 147).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(RECORDED_KEY_VAR, RECORDED_KEY)

    paths = {
        SEEDED: tmp_harnessed_home / db.DATABASE_FILENAME,
        EMPTY: empty_home / db.DATABASE_FILENAME,
        NO_DATABASE: no_database_home / db.DATABASE_FILENAME,
        PRICED: priced_home / db.DATABASE_FILENAME,
        PROBED: probed_home / db.DATABASE_FILENAME,
        CONSOLIDATED: consolidated_home / db.DATABASE_FILENAME,
        TASKED: tasked_home / db.DATABASE_FILENAME,
        LAUNCHD: backend_homes[LAUNCHD] / db.DATABASE_FILENAME,
        SYSTEMD: backend_homes[SYSTEMD] / db.DATABASE_FILENAME,
        ROUTING_OFF: routing_off_home / db.DATABASE_FILENAME,
    }
    #: What the app's settings hold while a home is recorded: the routing-off app has a Groq key
    #: and nothing for OpenRouter, the one state `/v1/router` reads `routing: "off"` from.
    home_env = {
        ROUTING_OFF: {"HARNESSED_PROVIDER": "groq", "GROQ_API_KEY": "placeholder-for-the-recording"}
    }
    #: Where each home is recorded from: `/v1/agents` reads `agents.toml` in the working directory.
    working_dirs = {PROBED: registry_dir}
    assert not paths[NO_DATABASE].exists(), "the 500 case needs a home with no database file"

    recorded: list[Recorded] = []
    for home, database_path in paths.items():
        monkeypatch.chdir(working_dirs.get(home, tmp_path))
        app_over = create_app(database_path)
        with monkeypatch.context() as scoped:
            for name, value in home_env.get(home, {}).items():
                scoped.setenv(name, value)
            if home in home_env:
                scoped.delenv(RECORDED_KEY_VAR)
            with TestClient(app_over, raise_server_exceptions=False) as client:
                recorded += [
                    exchange(client, case, database_path)
                    for case in cases_for(seeded, tasked)
                    if case.home == home
                ]
    assert not paths[NO_DATABASE].exists(), "the gateway created the database file it was handed"
    providers = next(item for item in recorded if item.case.name == "providers")
    assert RECORDED_KEY.encode("utf-8") not in providers.raw, "a key reached the providers body"
    return Bench(seeded=seeded, tasked=tasked, recorded=tuple(recorded))


WORK_PENDING = """\
suite: work-notes
kind: work
description: recorded from real sessions
cases:
  - {id: work-001-a, input: summarise the note, handler: groq_chat, reference: ok, verdict: pending}
  - {id: work-002-b, input: list the tasks, handler: groq_chat, reference: ok, verdict: accept}
"""

WORK_COMPLETE = """\
suite: work-reports
kind: work
description: recorded from real sessions
cases:
  - {id: work-001-a, input: draft the report, handler: groq_chat, reference: ok, verdict: accept}
"""


def seed_work_suites(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A temporary suites root of its own: one work suite awaiting a verdict, one complete.

    The recorder never reads ``corpus/suites/work-email.yaml`` (ticket 238). The committed smoke
    suite is copied in so the listing also carries a suite whose latest run is a served one,
    without planting a run that would move ``/v1/eval/runs``.
    """
    root.mkdir()
    (root / "work-notes.yaml").write_text(WORK_PENDING, encoding="utf-8")
    (root / "work-reports.yaml").write_text(WORK_COMPLETE, encoding="utf-8")
    (root / "smoke.yaml").write_bytes((shipped.suites_dir() / "smoke.yaml").read_bytes())
    monkeypatch.setattr(shipped, "suites_dir", lambda: root)


def test_the_recorded_decision_does_not_depend_on_how_slow_the_decider_is(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 225: ``latency_seconds`` is the recorder's declared figure, never a measured one.

    The decider call is made to take a real fifty milliseconds, far over the three-decimal
    rounding that made the committed ``0.000`` a property of the machine that recorded it.
    """
    monkeypatch.setenv(MODEL_VAR, str(quirks.default_model_for(OFFLINE_PROVIDER)))
    real_decide = FakeDecider.decide

    def slow_decide(self: FakeDecider, state: str, questions: tuple[Question, ...]) -> Decisions:
        time.sleep(DELAY_SECONDS)
        return real_decide(self, state, questions)

    monkeypatch.setattr(FakeDecider, "decide", slow_decide)
    seeded = seed(tmp_harnessed_home)
    database_path = tmp_harnessed_home / db.DATABASE_FILENAME
    with TestClient(create_app(database_path)) as client:
        body = client.get(f"/v1/sessions/{seeded.session_id}/decisions").content
    assert body == (FIXTURES / "sessions.decisions.body").read_bytes()
    assert f'"latency_seconds":"{DECIDER_LATENCY}"'.encode() in body


# ---- AC 1, AC 3, AC 4: the record is the emitters' own bytes ------------------------------------


def test_every_recorded_response_is_the_bytes_the_emitters_produce(
    bench: Bench, request: pytest.FixtureRequest
) -> None:
    """The drift check, and - only under the option - the recorder.

    One test for the whole set, because the seeded home is one bench and a per-case parametrize
    would rebuild it once per route. Every disagreement is collected and reported together with
    its file name, so a re-record is one pass rather than one per failure.
    """
    recording = bool(request.config.getoption(RECORD_DASHBOARD_FIXTURES))
    if recording:
        written = write_record(bench.recorded, FIXTURES)
        assert written, "the recorder wrote nothing"

    before = digest_of(FIXTURES)
    found = [line for item in bench.recorded for line in mismatches(item, FIXTURES)]
    assert found == [], "\n".join(found)

    # AC3's other half: a comparison run writes nothing, ever.
    assert digest_of(FIXTURES) == before

    names = sorted({item.case.name for item in bench.recorded})
    sizes = sorted(
        f"{path.name}  {path.stat().st_size} bytes" for path in FIXTURES.iterdir() if path.is_file()
    )
    capture(
        "fixtures.txt",
        "\n".join(
            [
                f"{len(names)} recorded cases over {FIXTURES.relative_to(REPO_ROOT).as_posix()}",
                f"recorded: {'yes' if recording else 'no (compared only)'}",
                f"directory digest: {before}",
                "",
                *sizes,
                "",
                *seeded_failure_report(),
            ]
        )
        + "\n",
    )


def test_the_database_path_is_the_only_value_normalized(
    bench: Bench, tmp_harnessed_home: Path
) -> None:
    """AC4: on ``/health`` the placeholder stands in for ``database_path`` and nothing else moves.

    The claim is checked against the bytes that actually arrived, so "nothing else is
    normalized" is read off a comparison rather than off this file's docstring: every recorded
    body must equal its raw response except where the database path was spelled, and exactly two
    cases may differ at all.
    """
    health = bench.by_name("health")
    payload = json.loads(health.payload)

    assert payload["database_path"] == DATABASE_PATH_PLACEHOLDER
    assert json.loads(health.raw)["database_path"] == str(
        (tmp_harnessed_home / db.DATABASE_FILENAME).resolve()
    )
    assert sorted(payload) == ["database_path", "route_count", "schema_version"]
    assert json.loads(health.raw)["route_count"] == payload["route_count"]
    assert json.loads(health.raw)["schema_version"] == payload["schema_version"]

    moved = sorted(item.case.name for item in bench.recorded if item.payload != item.raw)
    assert moved == ["health", "nodatabase.runs.listing"], moved
    for item in bench.recorded:
        if item.case.name not in moved:
            assert item.payload == item.raw, item.case.name
        assert DATABASE_PATH_PLACEHOLDER not in item.raw.decode("utf-8")


# ---- AC 2: what the recorded set covers ---------------------------------------------------------


def meta_by_name(directory: Path = FIXTURES) -> dict[str, dict[str, object]]:
    """Every committed case, read from its ``.meta.json``: the record as its own index."""
    found: dict[str, dict[str, object]] = {}
    for path in sorted(directory.glob("*.meta.json")):
        found[path.name[: -len(".meta.json")]] = json.loads(path.read_text(encoding="utf-8"))
    return found


#: Decision 24's route table, as the coverage assertion reads it: a template, and how many of
#: its recorded cases must answer 200. Every row must be covered, which is what makes a new
#: route with no recording a failure here rather than a gap nobody notices.
REGISTERED_ROUTES: tuple[str, ...] = (
    "/v1/health",
    "/v1/eval/suites",
    "/v1/runs",
    "/v1/runs/totals",
    "/v1/runs/turn_seconds",
    "/v1/spend",
    "/v1/runs/{run_key}",
    "/v1/runs/{run_key}/trace",
    "/v1/eval/runs",
    "/v1/eval/runs/{eval_run_id}",
    "/v1/eval/compare",
    "/v1/improve/cycles/{improvement_run_id}",
    "/v1/promotions",
    "/v1/memory/facts",
    "/v1/memory/totals",
    "/v1/memory/consolidation",
    "/v1/memory/facts/{mem_id}",
    "/v1/memory/episodes/{session_id}",
    "/v1/memory/procedures",
    "/v1/sessions",
    "/v1/events/totals",
    "/v1/events/gate",
    "/v1/sessions/{session_id}/events",
    "/v1/sessions/{session_id}/stream",
    "/v1/prices",
    "/v1/providers",
    "/v1/agents",
    "/v1/router",
    "/v1/sessions/{session_id}/decisions",
    "/v1/outputs/{output_id}",
    "/v1/sessions/{session_id}/messages",
    "/v1/messages",
    "/v1/permissions",
    "/v1/tasks",
    "/v1/tasks/totals",
    "/v1/tasks/{task_id}",
    "/v1/schedules",
)

#: The names AC2 spells out, each beside the status it must carry. A rename here is a rename in
#: the record and in the wire-fidelity walk, which is the point of holding it as data.
REQUIRED_CASES: tuple[tuple[str, int], ...] = (
    ("runs.totals", 200),
    ("runs.turn-seconds", 200),
    ("spend", 200),
    ("spend.session", 200),
    ("events.totals", 200),
    ("events.gate", 200),
    ("memory.totals", 200),
    ("memory.consolidation", 200),
    ("sessions.listing.consolidated", 200),
    ("providers", 200),
    ("prices", 200),
    ("empty.prices", 200),
    ("agents", 200),
    ("empty.agents", 200),
    ("router", 200),
    ("router.probed", 200),
    ("router.failed", 200),
    ("router.routing-off", 200),
    ("sessions.messages.routing-off", 200),
    ("outputs.get", 200),
    ("runs.session", 200),
    ("runs.case-run", 200),
    ("eval.run.zero-case", 200),
    ("eval.runs.listing", 200),
    ("improve.cycle.undecided", 200),
    ("improve.cycle.baseline-only", 200),
    ("improve.cycle.dry-run", 200),
    ("improve.cycle.promoted", 200),
    ("sessions.listing", 200),
    ("sessions.decisions", 200),
    ("sessions.messages", 200),
    ("sessions.messages.unrouted", 200),
    ("sessions.messages.failed", 200),
    ("messages.recent", 200),
    ("permissions.pending", 200),
    ("permissions.session", 200),
    ("tasks.listing", 200),
    ("tasks.needs-human", 200),
    ("tasks.session", 200),
    ("tasks.session.none", 200),
    ("tasks.totals", 200),
    ("tasks.done", 200),
    ("schedules.listing", 200),
    ("sessions.stream", 200),
    ("improve.sessions.stream", 200),
    ("eval.compare.regression", 200),
    ("eval.compare.zero-case-side", 200),
    ("empty.runs.listing", 200),
    ("empty.eval.runs.listing", 200),
    ("empty.sessions.listing", 200),
    ("empty.promotions", 200),
    ("empty.memory.facts", 200),
    ("empty.memory.procedures", 200),
    ("empty.spend", 200),
    ("empty.runs.turn-seconds", 200),
    ("empty.runs.totals", 200),
    ("empty.events.totals", 200),
    ("empty.events.gate", 200),
    ("empty.memory.totals", 200),
    ("empty.memory.consolidation", 200),
    ("empty.messages.recent", 200),
    ("empty.permissions", 200),
    ("empty.tasks", 200),
    ("empty.tasks.totals", 200),
    ("empty.schedules", 200),
    ("notfound.runs.run", 404),
    ("notfound.spend.session", 404),
    ("notfound.runs.trace", 404),
    ("notfound.eval.run", 404),
    ("notfound.improve.cycle", 404),
    ("notfound.memory.fact", 404),
    ("notfound.memory.episodes", 404),
    ("notfound.sessions.events", 404),
    ("notfound.sessions.decisions", 404),
    ("notfound.sessions.messages", 404),
    ("notfound.sessions.stream", 404),
    ("notfound.permissions.session", 404),
    ("notfound.tasks.session", 404),
    ("notfound.tasks.task", 404),
    ("notfound.route", 404),
    ("notfound.eval.compare.unknown-before", 404),
    ("notfound.eval.compare.unknown-after", 404),
    ("badrequest.runs.limit", 400),
    ("badrequest.sessions.limit", 400),
    ("badrequest.messages.limit", 400),
    ("badrequest.permissions.state", 400),
    ("badrequest.tasks.state", 400),
    ("badrequest.tasks.limit", 400),
    ("badrequest.eval.compare.suite-mismatch", 400),
    ("badrequest.eval.compare.missing-parameter", 400),
    ("tasked.tasks.listing", 200),
    ("tasked.tasks.needs-human", 200),
    ("tasked.tasks.totals", 200),
    ("tasked.tasks.permission", 200),
    ("tasked.tasks.uncertain", 200),
    ("tasked.tasks.done", 200),
    ("tasked.tasks.failed", 200),
    ("tasked.schedules.listing", 200),
    ("tasked.schedules.launchd", 200),
    ("tasked.schedules.systemd", 200),
    ("tasked.messages.recent", 200),
    ("tasked.sessions.messages.permission", 200),
    ("tasked.sessions.messages.uncertain", 200),
    ("tasked.sessions.decisions.uncertain-done", 200),
    ("tasked.sessions.decisions.uncertain-confidence", 200),
    ("tasked.sessions.messages.done", 200),
    ("tasked.sessions.messages.finished", 200),
    ("tasked.sessions.stream.permission", 200),
    ("tasked.permissions.permission", 200),
    ("nodatabase.runs.listing", 500),
)


def template_of(route: str) -> str:
    """Which registered route a recorded path was a request to. The longest match wins."""
    stem = route.partition("?")[0]
    segments = stem.strip("/").split("/")
    for template in REGISTERED_ROUTES:
        parts = template.strip("/").split("/")
        if len(parts) != len(segments):
            continue
        if all(
            wanted.startswith("{") or wanted == given
            for wanted, given in zip(parts, segments, strict=True)
        ):
            return template
    return ""


def test_the_recorded_set_covers_every_route_and_every_state_the_ticket_names() -> None:
    """AC2, read off the committed record rather than off this run's bench."""
    meta = meta_by_name()
    assert meta, f"nothing recorded under {FIXTURES}"

    statuses = {name: int(str(entry["status"])) for name, entry in meta.items()}
    for name, status in REQUIRED_CASES:
        assert name in meta, f"{name} is not in the record"
        assert statuses[name] == status, f"{name} recorded {statuses[name]}, not {status}"

    answered_200 = {
        template_of(str(entry["route"])) for name, entry in meta.items() if statuses[name] == 200
    }
    assert set(REGISTERED_ROUTES) - answered_200 == set(), "a registered route has no 200 recorded"

    # Every recorded route is one of the registered ones, or the deliberate router miss.
    unmatched = {
        str(entry["route"]) for entry in meta.values() if not template_of(str(entry["route"]))
    }
    assert unmatched == {str(meta["notfound.route"]["route"])}


def test_the_recorded_bodies_carry_the_nulls_the_emitters_produce() -> None:
    """AC2's null half, and the one the emitters cannot produce, said out loud.

    ``score`` is null for the run that recorded no case (FR-47) and the cycle counts are null
    for the one that measured nothing (FR-46), both asserted on the committed bytes. ``usage``
    and ``stop_reason`` are asserted the other way: the journal writes both columns for every
    turn a provider ever answered, so no run the emitters produce carries a null there, and
    recording one would mean planting a row no build writes.
    """
    zero_case = json.loads((FIXTURES / "eval.run.zero-case.body").read_bytes())
    assert zero_case["score"] is None
    assert zero_case["case_count"] == 0

    listed = json.loads((FIXTURES / "eval.runs.listing.body").read_bytes())
    assert [row["eval_run_id"] for row in listed["runs"] if row["score"] is None] != []

    undecided = json.loads((FIXTURES / "improve.cycle.undecided.body").read_bytes())
    assert [name for name in undecided if undecided[name] is None] != []

    measured = json.loads((FIXTURES / "eval.run.measured.body").read_bytes())
    assert measured["score"] is not None, "a change that nulled every score would pass above"

    for name in ("runs.session", "runs.case-run"):
        payload = json.loads((FIXTURES / f"{name}.body").read_bytes())
        assert payload["usage"] is not None, f"{name} recorded a usage no emitter produces"
        assert payload["stop_reason"] is not None


def recorded_body(name: str) -> dict[str, object]:
    """One committed body, decoded from its open file handle (the parser's quarantine)."""
    with (FIXTURES / f"{name}.body").open("rb") as handle:
        return dict(json.load(handle))


def test_the_tasked_record_holds_a_paused_task_of_each_kind_and_the_tasks_own_messages() -> None:
    """Ticket 256, FR-153/FR-154: read off the committed bytes, with a reason beside every null."""
    for kind in ("permission", "uncertain", "done", "finished"):
        body = recorded_body(f"tasked.sessions.messages.{kind}")
        assert body["task"] is not None, f"{kind}: the session's task is null"
        messages = body["messages"]
        assert isinstance(messages, list) and messages
        for message in messages:
            assert message["author"] == "task", f"{kind}: {message['author']}"
            assert message["task_message"] in ("first", "continuation")

    finished = recorded_body("tasked.sessions.messages.finished")["messages"]
    assert [m["reply_reason"] for m in finished] == [None, "the task is finished"]
    assert finished[1]["reply"] is None

    recent = recorded_body("tasked.messages.recent")["messages"]
    assert isinstance(recent, list) and recent
    assert all(row["author"] == "task" and row["task_id"] for row in recent)

    waiting = recorded_body("tasked.tasks.needs-human")["tasks"]
    assert isinstance(waiting, list) and len(waiting) == 2
    permission, uncertain = waiting
    assert (permission["pause_reason"], uncertain["pause_reason"]) == ("permission", "uncertain")
    assert permission["paused_at"] < uncertain["paused_at"], "the permission pause is the earliest"
    for field in ("pause_agent", "pause_tool_kind", "pause_description"):
        assert permission[field] is not None, field
    for field in ("pause_route", "pause_confidence_percent", "pause_floor_percent"):
        assert uncertain[field] is not None, field
    assert (uncertain["pause_confidence_percent"], uncertain["pause_floor_percent"]) == (
        "41%",
        "60%",
    )
    for row in waiting:
        for name, value in row.items():
            if value is None and not name.endswith("_reason"):
                assert row.get(f"{name}_reason"), f"{row['task_id']}.{name} is null with no reason"

    totals = recorded_body("tasked.tasks.totals")
    assert (totals["needs_human"], totals["completed_unattended"], totals["done"]) == (
        "2",
        "1",
        "1",
    )
    done = recorded_body("tasked.tasks.done")
    assert (done["human_messages"], done["pauses"], done["stop_reason"]) == ("0", "0", "finished")
    assert all(
        recorded_body(f"tasked.tasks.{kind}")["cwd"] == PLANTED_CWD
        for kind in ("permission", "done")
    )


def test_the_tasked_record_holds_a_schedule_a_live_install_and_a_task_it_started() -> None:
    """Ticket 258, FR-154/FR-155: the schedule read has a row, every column non-null; V6-6's
    handler and outcome are served on each task row."""
    rows = recorded_body("tasked.schedules.listing")["schedules"]
    assert isinstance(rows, list) and len(rows) == 1
    for name, value in rows[0].items():
        if not name.endswith("_reason"):
            assert value is not None, f"schedules.{name} is null"
    assert (rows[0]["installed"], rows[0]["where"]) == ("true", "task_scheduler")
    for backend in ("launchd", "systemd"):
        rows_b = recorded_body(f"tasked.schedules.{backend}")["schedules"]
        assert isinstance(rows_b, list) and len(rows_b) == 1
        assert (rows_b[0]["installed"], rows_b[0]["where"]) == ("true", backend)
        for name, value in rows_b[0].items():
            if not name.endswith("_reason"):
                assert value is not None, f"{backend}: schedules.{name} is null"
    assert recorded_body("empty.schedules")["schedules"] == []
    assert recorded_body("schedules.listing")["schedules"] == []

    done = recorded_body("tasked.tasks.done")
    assert (done["started_by"], done["schedule_id"]) == ("schedule", rows[0]["schedule_id"])
    assert (done["handler"], done["outcome"]) == ("agent", "routed")
    tasks = recorded_body("tasked.tasks.listing")["tasks"]
    assert isinstance(tasks, list)
    assert {row["task_id"]: row["outcome"] for row in tasks}[done["task_id"]] == "routed"


def test_the_tasked_home_holds_one_failed_task() -> None:
    """Ticket 292: a task ended ``failed`` by an expired permission, served by its own filter."""
    failed = recorded_body("tasked.tasks.failed")["tasks"]
    assert isinstance(failed, list) and len(failed) == 1
    assert (failed[0]["state"], failed[0]["stop_reason"]) == ("failed", "ceiling")
    listed = recorded_body("tasked.tasks.listing")["tasks"]
    assert isinstance(listed, list)
    assert failed[0]["task_id"] in {row["task_id"] for row in listed}
    assert recorded_body("tasked.tasks.totals")["failed"] == "1"


def test_the_recorded_improve_stream_runs_through_its_own_finish() -> None:
    """AC2's last row: the improve session's frames, terminal on ``improve.run.finished``."""
    frames = (FIXTURES / "improve.sessions.stream.sse").read_text(encoding="utf-8")
    events = [
        line.removeprefix("event: ") for line in frames.splitlines() if line.startswith("event: ")
    ]

    assert events[0] == "improve.run.started"
    assert events[-1] == "improve.run.finished"
    assert events.count("eval.run.finished") == 3, "baseline, candidate and holdout"
    assert events.count("improve.run.finished") == 1


# ---- AC 5: a recorded body altered by one byte fails, naming that file --------------------------

#: The case the drift proof tampers with. Any recorded body would do; this one is the shortest
#: whose bytes a reviewer can read at a glance.
DRIFT_CASE = "health"


def seeded_failure_report(directory: Path = FIXTURES) -> list[str]:
    """The drift proof as evidence: the byte changed, and the line the comparison reported.

    Run over a copy under a temporary directory, never the committed file - a proof that edits
    the record would be a proof that leaves the record edited.
    """
    lines: list[str] = []
    for name, changed, reported in _drift_trials(directory):
        first, *rest = reported.splitlines()
        lines += [f"seeded drift in {name}: {changed}", f"  reported: {first}", *rest]
    return lines


def _drift_trials(directory: Path) -> Iterator[tuple[str, str, str]]:
    """One trial: alter a byte in a copy of the record and yield what the comparison said."""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "gateway"
        shutil.copytree(directory, copy)
        name, changed, reported = _tamper_and_compare(copy)
        yield name, changed, reported


def _tamper_and_compare(copy: Path) -> tuple[str, str, str]:
    """Alter one byte of ``DRIFT_CASE``'s body in ``copy`` and report the one mismatch."""
    body = copy / f"{DRIFT_CASE}.body"
    original = body.read_bytes()
    meta = json.loads((copy / f"{DRIFT_CASE}.meta.json").read_text(encoding="utf-8"))
    recorded = Recorded(
        case=Case(DRIFT_CASE, SEEDED, HEALTH_ROUTE),
        status=int(str(meta["status"])),
        content_type=str(meta["content_type"]),
        payload=original,
    )
    assert mismatches(recorded, copy) == [], "the untampered copy must compare clean"

    altered = original[:-2] + bytes([original[-2] + 1]) + original[-1:]
    body.write_bytes(altered)
    found = mismatches(recorded, copy)
    assert len(found) == 1, found
    return (
        body.name,
        f"byte {len(original) - 2}: {original[-2:-1]!r} -> {altered[-2:-1]!r}",
        found[0],
    )


def test_a_recorded_body_altered_by_one_byte_fails_naming_that_file(tmp_path: Path) -> None:
    """AC5, against a copy in ``tmp_path``: the committed record is read and never written."""
    copy = tmp_path / "gateway"
    shutil.copytree(FIXTURES, copy)
    committed = digest_of(FIXTURES)

    name, changed, reported = _tamper_and_compare(copy)

    assert name == f"{DRIFT_CASE}.body"
    assert name in reported
    assert RECORD_DASHBOARD_FIXTURES in reported or "Re-record" in reported
    assert changed
    assert digest_of(FIXTURES) == committed, "the drift proof touched the committed record"


def test_the_recorder_rewrites_only_when_it_is_asked(tmp_path: Path) -> None:
    """AC3 from the other side: ``write_record`` is the only writer, and it does rewrite.

    Proven on a copy, so the door the option opens is shown to work without the option being
    the thing under test in the committed directory.
    """
    copy = tmp_path / "gateway"
    shutil.copytree(FIXTURES, copy)
    body = copy / f"{DRIFT_CASE}.body"
    recorded = Recorded(
        case=Case(DRIFT_CASE, SEEDED, HEALTH_ROUTE),
        status=200,
        content_type="application/json",
        payload=b'{"database_path":"<database_path>","route_count":1,"schema_version":1}',
    )

    body.write_bytes(b"{}")
    assert mismatches(recorded, copy) != []

    written = write_record([recorded], copy)
    assert written == [f"{DRIFT_CASE}.body", f"{DRIFT_CASE}.meta.json"]
    assert mismatches(recorded, copy) == []
    # The rewrite emptied the directory first, so a dropped case leaves no orphan behind.
    assert sorted(path.name for path in copy.iterdir()) == written


def test_the_record_is_checked_out_as_the_bytes_it_was_recorded_as() -> None:
    """A byte comparison is worth nothing if a checkout can rewrite the bytes (ticket 117's
    lesson, in the one place a newline is load-bearing).

    ``core.autocrlf=true`` is the default on Windows, and it would check the ``.sse`` frames and
    every ``.meta.json``'s trailing newline out with CRLF - which is 92 unreadable byte diffs
    rather than one named cause. ``.gitattributes`` pins the directory ``-text``; this asserts the
    outcome that pin exists for, so a checkout that lost it fails here and says why.
    """
    corrupted = [path.name for path in sorted(FIXTURES.iterdir()) if b"\r\n" in path.read_bytes()]
    assert corrupted == [], (
        f"{len(corrupted)} recorded files hold CRLF: {corrupted[:3]}. The record is bytes, not "
        f"text - check `.gitattributes` still pins "
        f"{FIXTURES.relative_to(REPO_ROOT).as_posix()}/** as -text, then re-record."
    )


def test_the_recorded_permission_frame_carries_the_rows_exchange() -> None:
    """Ticket 288: the card is placed from the frame, so its ``message_seq`` is the row's."""
    frames = (FIXTURES / "tasked.sessions.stream.permission.sse").read_text(encoding="utf-8")
    (data,) = (
        json.loads(line[len("data: ") :])
        for block in frames.split("\n\n")
        if "event: acp.permission.request" in block
        for line in block.splitlines()
        if line.startswith("data: ")
    )
    served = json.loads((FIXTURES / "tasked.permissions.permission.body").read_text("utf-8"))
    (row,) = served["permissions"]
    assert (str(data["message_seq"]), str(data["hop"])) == (row["message_seq"], row["hop"])
    assert data["permission_id"] == row["permission_id"]
    assert row["detail"] and row["task_hash"] and row["waiter_pid"] == "4242"


def test_the_tasked_record_holds_an_uncertain_stop_of_each_compared_figure() -> None:
    """Ticket 291 (FR-171, decision 101): a stop after 281 compared ``done``'s own probability, a
    stop before it compared the confidence, and the paused task's row carries its own figure."""
    served = {
        name: json.loads((FIXTURES / f"tasked.sessions.decisions.{name}.body").read_bytes())
        for name in ("uncertain-done", "uncertain-confidence")
    }
    (after,) = served["uncertain-done"]["decisions"]
    (before,) = served["uncertain-confidence"]["decisions"]
    assert (after["confidence_percent"], after["floor_percent"]) == ("78%", "80%")
    assert (before["confidence_percent"], before["floor_percent"]) == ("78%", "80%")
    assert after["compared"] == "done_probability" and after["compared_reason"] is None
    assert before["compared"] == "confidence" and before["compared_reason"] is None
    assert {o["option"]: o["probability_percent"] for o in before["options"]}["done"] == "82%"
    paused = json.loads((FIXTURES / "tasked.tasks.uncertain.body").read_bytes())
    assert paused["pause_compared"] == "confidence"
    messages = json.loads((FIXTURES / "tasked.sessions.messages.uncertain.body").read_bytes())
    hops = messages["messages"][0]["hops"]
    assert [h["compared"] for h in hops if h["compared"]] == ["confidence"]
