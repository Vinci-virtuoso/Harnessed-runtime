"""Ticket 138 (FR-80, decisions 50 to 52): ``harnessed run --session <id>`` continues a run.

A session is one run continued turn by turn: one run key, one ``run_id`` on the bus, one growing
row set. The follow-up's request is **the stored list plus one** - read back through
``loop/journal.py::messages_for``, the journal's one sanctioned reader - and every
``[redacted:<kind>]`` marker in that list travels into the resumed context as written.

The CLI and the control app are two callers of one composition, ``run_cmd.continue_session``.
The last test here runs identical input through both under FakeProvider and diffs the journal
rows they wrote, which is the only honest form of "one composition".
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from tests.support.scoped_decider import ScopedFakeDecider
from typer.testing import CliRunner

from harnessed.cli import run_cmd
from harnessed.cli.app import app
from harnessed.control.worker import Worker
from harnessed.errors import HarnessError, RunInFlight, SessionNotContinued, SurfaceNotFound
from harnessed.loop import runs
from harnessed.loop.engine import LoopResult
from harnessed.loop.journal import messages_for
from harnessed.memory import db, redact
from harnessed.providers.fake_decider import FakeDecider
from harnessed.providers.translate import journal_message
from harnessed.providers.types import ChatRequest, ChatResponse, Message
from harnessed.tasks import lease

runner = CliRunner(mix_stderr=False)

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "138"

PROMPT = "ping"
FOLLOW_UP = "ping again"

# Assembled from fragments, as ``test_journal.py`` does: a literal that looks like a credential is
# what the repository's own write guard exists to reject.
SECRET = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDDEEEEFFFF"


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


@dataclass(slots=True)
class Seen:
    """What each CLI run handed the loop, and at which ``start_iteration``."""

    requests: list[ChatRequest] = field(default_factory=list)
    starts: list[int] = field(default_factory=list)


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> Seen:
    """Spy on ``run_loop`` in ``run_cmd``'s namespace, without stopping it running."""
    found = Seen()
    real = run_cmd.run_loop

    def spy(model: object, registry: object, request: ChatRequest, **kwargs: Any) -> LoopResult:
        found.requests.append(request)
        found.starts.append(int(kwargs.get("start_iteration", 0)))
        return real(model, registry, request, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(run_cmd, "run_loop", spy)
    return found


def run_json(*args: str) -> dict[str, object]:
    result = runner.invoke(app, ["run", "--json", *args])
    assert result.exit_code == 0, result.output + result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def stored(run_key: str) -> tuple[Message, ...]:
    conn = db.connect()
    try:
        return messages_for(conn, run_key, decode=journal_message)
    finally:
        conn.close()


def events_of(run_key: str) -> list[tuple[int, str]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT seq, name FROM events WHERE run_id = ? ORDER BY seq", (run_key,)
        ).fetchall()
    finally:
        conn.close()
    return [(int(row[0]), str(row[1])) for row in rows]


# ---- the stored list plus one --------------------------------------------------------------------


def test_a_follow_up_sends_the_stored_list_plus_one_on_the_same_run(
    clean_env: None, tmp_harnessed_home: Path, seen: Seen
) -> None:
    first = run_json(PROMPT)
    key = str(first["run_key"])
    before = stored(key)
    assert [message.role for message in before] == ["user", "assistant"]

    second = run_json("--session", key, FOLLOW_UP)

    assert second["run_key"] == key, "a follow-up is more iterations of the same run"
    resumed = seen.requests[-1].messages
    assert resumed[:-1] == before, "the resumed request opens with the stored list, as stored"
    assert len(resumed) == len(before) + 1
    assert resumed[-1].role == "user"
    assert resumed[-1].content.endswith(FOLLOW_UP)
    assert seen.starts == [0, 1], "the follow-up's journal key continues at 2"
    # decision 52: the run's total is read from the ledger, not the message's count.
    assert second["iterations"] == 2
    assert [message.role for message in stored(key)] == ["user", "assistant"] * 2
    capture(
        "stored-list-plus-one.txt",
        f"ticket 138 - harnessed run --session {key} {FOLLOW_UP!r}\n"
        f"stored before: {len(before)} messages\nresumed request: {len(resumed)} messages\n"
        f"resumed[:-1] == stored: {resumed[:-1] == before}\n"
        f"run_payload.iterations after the follow-up: {second['iterations']}\n",
    )


def test_a_seeded_key_resumes_as_its_marker_and_never_as_the_key(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safe direction: the marker is carried into context as written, 0 copies of the key."""
    asked: list[ChatRequest] = []

    class Scripted:
        def complete(self, request: ChatRequest) -> ChatResponse:
            if request.metadata.get("scope") != "_gate":
                asked.append(request)
            return ChatResponse(response_id="r", model="stub", content="noted")

    monkeypatch.setattr(run_cmd, "build_provider", lambda settings: Scripted())
    first = run_json(f"remember this key {SECRET}")
    key = str(first["run_key"])
    run_json("--session", key, "what was the key?")

    resumed = asked[-1]
    text = "\n".join(message.content for message in resumed.messages)
    marker = redact.token("anthropic_key")
    assert marker in resumed.messages[0].content
    assert text.count(SECRET) == 0
    capture(
        "redaction-round-trip.txt",
        "ticket 138 - a seeded key in the first message, resumed\n"
        f"resumed first message: {resumed.messages[0].content}\n"
        f"occurrences of the key in the resumed context: {text.count(SECRET)}\n",
    )


# ---- the follow-up's own gate decision and its own start/finish pair -----------------------------


def test_a_follow_up_publishes_its_own_gate_decision_and_a_second_run_pair(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    key = str(run_json(PROMPT)["run_key"])
    run_json("--session", key, FOLLOW_UP)
    events = events_of(key)
    names = [name for _, name in events]
    assert names.count("run.started") == 2
    assert names.count("run.finished") == 2
    assert names.count("retrieval.gate.decided") == 2, "the gate runs on every message"
    assert [seq for seq, _ in events] == list(range(1, len(events) + 1)), "one seq, no restart"
    second_start = names.index("run.started", 1)
    assert "retrieval.gate.decided" in names[second_start:]
    assert names[-1] == "run.finished"
    capture(
        "follow-up-events.txt",
        f"ticket 138 - events on session {key} after one follow-up\n"
        + "\n".join(f"{seq:3d} {name}" for seq, name in events)
        + "\n",
    )


# ---- a raising composition settles through settle_raised, same as the worker's door -------------


def test_a_raising_run_settles_through_settle_raised_before_exiting_1(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: ``harnessed run`` raising publishes the same pair the control app's worker does."""
    seen: list[str] = []

    def raising(*_args: object, **kwargs: object) -> LoopResult:
        seen.append(str(kwargs["bus"].run_id))  # type: ignore[union-attr]
        raise HarnessError("the composition raised mid-run")

    monkeypatch.setattr(run_cmd, "run_loop", raising)
    result = runner.invoke(app, ["run", PROMPT])
    assert result.exit_code == 1
    assert isinstance(result.exception, HarnessError)

    events = events_of(seen[0])
    names = [name for _, name in events]
    assert names[0] == "run.started"
    assert names[-2:] == ["error.raised", "run.finished"]
    capture(
        "cli-raise-settles.txt",
        f"ticket 151 - harnessed run {PROMPT!r} raising\n"
        + "\n".join(f"{seq:3d} {name}" for seq, name in events)
        + "\n",
    )


def test_the_event_names_module_is_unchanged_by_sessions() -> None:
    """Decision 51: no new event name. ``names.py`` is scope_out and the pair is enough."""
    from harnessed.events.names import NAMES

    assert "run.continued" not in NAMES
    assert {"run.started", "run.finished"} <= set(NAMES)


# ---- ticket 162 (FR-109): loop.route.decided precedes the first loop.iteration.started -----------

EVIDENCE_162 = REPO_ROOT / "evidence" / "162"


def capture_162(name: str, text: str) -> Path:
    EVIDENCE_162.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_162 / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loop_route_decided_precedes_the_first_loop_iteration_started(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    key = str(run_json(PROMPT)["run_key"])
    names = [name for _, name in events_of(key)]
    assert names.count("loop.route.decided") == 1
    assert names.index("loop.route.decided") < names.index("loop.iteration.started")
    capture_162(
        "routed-message-event-order.txt",
        f"ticket 162 - events on session {key}\n" + "\n".join(names) + "\n",
    )


# ---- AC4: an uncertain stop prints the reason, the floor, and the top routes - never an answer --

EVIDENCE_164 = REPO_ROOT / "evidence" / "164"


def capture_164(name: str, text: str) -> Path:
    EVIDENCE_164.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_164 / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def uncertain_decider(monkeypatch: pytest.MonkeyPatch) -> ScopedFakeDecider:
    """Force ticket 164's committed uncertain-stop fixture regardless of the prompt (its own
    fixture note: a declared scope, never the hash fallback ``ping`` already owns)."""
    from harnessed import config

    decider = ScopedFakeDecider(fake=FakeDecider(config.load()), scope="uncertain-stop")
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings, **_kwargs: decider)
    return decider


def test_uncertain_stop_json_shows_the_reason_the_floor_and_the_top_routes(
    clean_env: None, tmp_harnessed_home: Path, uncertain_decider: ScopedFakeDecider
) -> None:
    payload = run_json(PROMPT)
    assert payload["stop_reason"] == "below_floor"
    assert payload["route"] == "groq_memory"
    assert payload["floor"] == "0.5000"
    assert payload["floor_percent"] == "50%"
    assert payload["routes"] == [
        {"route": "groq_memory", "confidence_percent": "43%"},
        {"route": "groq_chat", "confidence_percent": "39%"},
        {"route": "needs_human", "confidence_percent": "18%"},
    ]
    assert "reply" not in payload
    assert "iterations" not in payload, "never an answer"
    capture_164(
        "uncertain-stop-json.txt",
        f"ticket 164 - harnessed run --json {PROMPT!r} under the uncertain-stop fixture\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n",
    )


def test_uncertain_stop_human_readable_never_prints_an_answer(
    clean_env: None, tmp_harnessed_home: Path, uncertain_decider: ScopedFakeDecider
) -> None:
    result = runner.invoke(app, ["run", PROMPT])
    assert result.exit_code == 0, result.output
    assert "stopped: below_floor" in result.output
    assert "groq_memory" in result.output
    assert "50%" in result.output
    capture_164(
        "uncertain-stop-human-readable.txt",
        f"ticket 164 - harnessed run {PROMPT!r} under the uncertain-stop fixture\n{result.output}",
    )


# ---- refusals ------------------------------------------------------------------------------------


def test_an_unknown_session_id_exits_1_with_surface_not_found(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    result = runner.invoke(app, ["run", "--session", "run_nobody", FOLLOW_UP])
    # ``main`` renders a ``HarnessError`` to stderr and exits 1; the runner sees the raise.
    assert result.exit_code == 1
    assert isinstance(result.exception, SurfaceNotFound), result.exception
    assert events_of("run_nobody") == [], "a refused follow-up publishes nothing"


def test_a_case_run_key_is_refused_because_a_case_run_is_not_continued(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    key = "eval_x:smoke-001-capital:1"
    assert runs.key_kind_of(key) == "case_run"
    result = runner.invoke(app, ["run", "--session", key, FOLLOW_UP])
    assert result.exit_code == 1
    assert isinstance(result.exception, SessionNotContinued), result.exception
    assert "case run" in str(result.exception)
    assert events_of(key) == []


def test_continue_session_refuses_a_session_opened_on_another_run(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    key = str(run_json(PROMPT)["run_key"])
    session = run_cmd.open_session()
    try:
        with pytest.raises(SessionNotContinued):
            run_cmd.continue_session(session, key, FOLLOW_UP)
    finally:
        session.conn.close()


# ---- one composition, two callers ----------------------------------------------------------------

JOURNAL_TABLES = {
    "turns": "SELECT * FROM turns ORDER BY iteration",
    "tool_results": "SELECT * FROM tool_results ORDER BY iteration, call_id",
    "run_messages": "SELECT * FROM run_messages ORDER BY idx",
}


def journal_rows(home: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every journal row under ``home``, with the run id column dropped: the two benches mint
    different ids, and everything else must be the same bytes."""
    conn = sqlite3.connect(home / db.DATABASE_FILENAME)
    try:
        return {
            table: [tuple(row[1:]) for row in conn.execute(sql).fetchall()]
            for table, sql in JOURNAL_TABLES.items()
        }
    finally:
        conn.close()


@pytest.fixture
def two_homes(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen_clock: object
) -> Iterator[tuple[Path, Path]]:
    cli, control = tmp_path / "cli", tmp_path / "control"
    cli.mkdir()
    control.mkdir()
    yield cli, control


def test_the_cli_and_the_control_app_write_identical_journal_rows(
    two_homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_home, control_home = two_homes

    monkeypatch.setenv("HARNESSED_HOME", str(cli_home))
    key = str(run_json(PROMPT)["run_key"])
    run_json("--session", key, FOLLOW_UP)

    monkeypatch.setenv("HARNESSED_HOME", str(control_home))
    worker = Worker()
    with worker.executor:
        started = worker.start(
            lambda opened: run_cmd.open_session(opened=opened, routed=True),
            lambda session, event: run_cmd.solve_with_memory(session, PROMPT, announced=event),
        )
        assert worker.run is not None
        worker.run.result()
        again = worker.start(
            lambda opened: run_cmd.open_session(opened=opened, session_id=started, routed=True),
            lambda session, event: run_cmd.continue_session(
                session, started, FOLLOW_UP, announced=event
            ),
        )
        assert again == started, "the follow-up answers with the same session id"
        worker.run.result()

    cli_rows, control_rows = journal_rows(cli_home), journal_rows(control_home)
    assert cli_rows["turns"], "the benches journaled something"
    assert len(cli_rows["run_messages"]) == 4
    assert cli_rows == control_rows
    capture(
        "cli-vs-control-journal.txt",
        "ticket 138 - identical input, FakeProvider, CLI against the control worker\n"
        + "\n".join(
            f"{table}: cli={len(cli_rows[table])} control={len(control_rows[table])} "
            f"identical={cli_rows[table] == control_rows[table]}"
            for table in JOURNAL_TABLES
        )
        + "\n",
    )


# ---- ticket 234 (FR-130, decision 78): every door that starts a message takes the run lease --


def hold_lease_as(session_id: str) -> None:
    conn = db.connect()
    try:
        lease.acquire(conn, session_id)
    finally:
        conn.close()


def lease_holder() -> lease.Lease | None:
    conn = db.connect()
    try:
        return lease.holder(conn)
    finally:
        conn.close()


def test_a_run_holds_the_lease_while_it_works_and_leaves_none_behind(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    during: list[str | None] = []
    real = run_cmd.routed.dispatch

    def watch(*args: Any, **kwargs: Any) -> Any:
        holder = lease_holder()
        during.append(None if holder is None else holder.session_id)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_cmd.routed, "dispatch", watch)
    key = str(run_json(PROMPT)["run_key"])
    assert during == [key], "the run's own session held the lease while it was working"
    assert lease_holder() is None, "and nothing outlives it"


def test_a_live_holder_refuses_harnessed_run_naming_its_session(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    hold_lease_as("run_busy_elsewhere")
    result = runner.invoke(app, ["run", PROMPT])
    assert result.exit_code == 1
    assert isinstance(result.exception, RunInFlight), result.exception
    assert "run_busy_elsewhere" in str(result.exception)
    holder = lease_holder()
    assert holder is not None and holder.session_id == "run_busy_elsewhere", "the holder keeps it"


def test_in_flight_refusal_names_the_running_task_and_what_to_do(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    from harnessed.tasks import store as task_store

    conn = db.connect()
    try:
        row = task_store.queue(conn, "tidy\nthe   notes", via="cli")
        task_id = str(row["task_id"])
        task_store.begin_message(
            conn, task_id, run_id="run_task_session", message_seq=1, author="task", first=True
        )
    finally:
        conn.close()
    hold_lease_as("run_task_session")
    result = runner.invoke(app, ["run", PROMPT])
    assert result.exit_code == 1
    assert isinstance(result.exception, RunInFlight), result.exception
    assert str(result.exception) == (
        f"a task is running: {task_id} tidy the notes; "
        f"wait for it, or harnessed task cancel {task_id}"
    )


def test_a_live_holder_refuses_a_run_the_control_worker_starts(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    hold_lease_as("run_busy_elsewhere")
    worker = Worker()
    with worker.executor:
        worker.start(
            lambda opened: run_cmd.open_session(opened=opened, routed=True),
            lambda session, event: run_cmd.solve_with_memory(session, PROMPT, announced=event),
        )
        assert worker.run is not None
        worker.run.result()
        conn = db.connect()
        try:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM events WHERE run_id = ? ORDER BY seq", (worker.session_id,)
                )
            ]
        finally:
            conn.close()
    assert names[-2:] == ["error.raised", "run.finished"], "the refusal settles the stream"
    holder = lease_holder()
    assert holder is not None and holder.session_id == "run_busy_elsewhere"


def test_eval_and_improve_never_take_the_lease(clean_env: None, tmp_harnessed_home: Path) -> None:
    """A session not opened for routing carries no door and so takes no lease, even when another
    run holds it; and no module on the eval or improve path names the lease at all."""
    hold_lease_as("run_busy_elsewhere")
    session = run_cmd.open_session()
    try:
        assert session.door is None
        memory_run = run_cmd.solve_with_memory(session, PROMPT)
        assert memory_run.result is not None
    finally:
        session.conn.close()
    holder = lease_holder()
    assert holder is not None and holder.session_id == "run_busy_elsewhere"
    root = REPO_ROOT / "src" / "harnessed"
    for path in (*(root / "evals").rglob("*.py"), *(root / "improve").rglob("*.py")):
        assert "tasks.lease" not in path.read_text(encoding="utf-8"), path
    for name in ("eval_cmd.py", "improve_cmd.py"):
        assert "lease" not in (root / "cli" / name).read_text(encoding="utf-8"), name


# ---- ticket 240 (FR-141): `harnessed run --session` on a task's session ---------------------


def test_run_session_on_a_task_paused_uncertain_resumes_it_with_the_operators_text(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.task_world import CannedProvider, ScriptedDecider, answer, install

    from harnessed.cli import task_run
    from harnessed.tasks import store

    install(
        monkeypatch,
        ScriptedDecider([answer("needs_human", "0.60"), answer("groq_chat"), answer("done")]),
        CannedProvider(),
    )
    conn = db.connect()
    task_id = str(store.queue(conn, "tidy the notes", via="cli")["task_id"])
    conn.close()
    paused = task_run.run_task(task_id)
    assert paused["pause_reason"] == "uncertain"

    payload = run_json("--session", str(paused["session_id"]), "use the blue folder")

    assert (payload["state"], payload["human_messages"]) == ("done", "1")


def test_run_session_on_a_task_waiting_for_an_answer_is_refused(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    from harnessed.tasks import store

    conn = db.connect()
    task_id = str(store.queue(conn, "tidy the notes", via="cli")["task_id"])
    session = "run_00000000000000240CLIWAIT00"
    store.begin_message(conn, task_id, run_id=session, message_seq=1, author="task", first=True)
    store.pause_for_permission(conn, task_id, "perm_x")
    conn.close()

    result = runner.invoke(app, ["run", "--session", session, "hurry"])

    assert result.exit_code == 1
    assert type(result.exception).__name__ == "TaskAwaitingAnswer"
    assert "waiting for your answer to the agent's request" in str(result.exception)


# ---- routing off (ticket 316, FR-175, decision 105) -------------------------------------------


def _decisions_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every Decisions call the offline Decider is asked for."""
    calls: list[str] = []
    real = FakeDecider.decide

    def spy(self: FakeDecider, state: str, questions: Any) -> Any:
        calls.append(state)
        return real(self, state, questions)

    monkeypatch.setattr(FakeDecider, "decide", spy)
    return calls


def _first_run_started(run_key: str) -> dict[str, object]:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM events WHERE run_id = ? AND name = 'run.started' ORDER BY seq",
            (run_key,),
        ).fetchone()
    finally:
        conn.close()
    payload = json.loads(row[0])
    assert isinstance(payload, dict)
    return payload


def test_with_no_decider_a_message_makes_no_decisions_call_and_says_routing_off(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _decisions_calls(monkeypatch)
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings: None)

    payload = run_json(PROMPT)

    run_key = str(payload["run_key"])
    names = [name for _seq, name in events_of(run_key)]
    assert calls == []
    assert "loop.route.decided" not in names
    assert _first_run_started(run_key)["routing"] == "off"
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    finally:
        conn.close()
    assert "error.raised" not in names, "routing off is not a fallback and shows no notice"


def test_with_a_decider_run_started_says_routing_on(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _decisions_calls(monkeypatch)

    payload = run_json(PROMPT)

    assert calls, "the offline Decider was asked"
    assert _first_run_started(str(payload["run_key"]))["routing"] == "on"
