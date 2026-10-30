"""Ticket 239 (FR-140, decision 76): a task is a ledger row with its own verbs.

``task add`` and ``queue`` write a ``queued`` row with a stored budget, ``task list|show`` print
the rows as strings, and ``task cancel`` moves a queued or waiting task to ``cancelled``. Running a
task is ticket 240's, so nothing here starts one.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.support.task_world import (
    CannedProvider,
    ScriptedDecider,
    answer,
    capture,
    install,
)

from harnessed import clock, config
from harnessed.acp import pending
from harnessed.cli import task_run
from harnessed.cli.app import main
from harnessed.errors import (
    ParameterError,
    PromptError,
    RunInFlight,
    TaskAwaitingAnswer,
    TaskStateError,
)
from harnessed.events import read
from harnessed.memory import db
from harnessed.tasks import store

SESSION = "run_00000000000000239TASKS000"
EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "264"


@pytest.fixture
def home(clean_env: None, tmp_harnessed_home: Path, frozen_clock: Any) -> Path:
    return tmp_harnessed_home


def cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
    code = 0
    try:
        main()
    except SystemExit as exited:
        code = int(exited.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def queue_one(**fields: Any) -> dict[str, str | None]:
    conn = db.connect()
    try:
        return store.queue(conn, "tidy the notes", via="cli", **fields)
    finally:
        conn.close()


def state_of(task_id: str) -> dict[str, str | None]:
    conn = db.connect()
    try:
        row = store.get(conn, task_id)
    finally:
        conn.close()
    assert row is not None
    return row


# ---- the budget defaults ------------------------------------------------------------------------


def test_no_budget_flag_stores_the_settings_defaults_and_says_so(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = config.load()
    assert (
        settings.task_budget_iterations,
        settings.task_budget_seconds,
        settings.task_budget_dollars,
    ) == (20, 600, "0.50")
    code, out, err = cli(monkeypatch, capsys, "task", "add", "tidy the notes", "--json")
    assert code == 0, err
    body = json.loads(out)
    assert body["task_id"].startswith("task_")
    assert {key: body[key] for key in body if key != "task_id"} == {
        "budget_iterations": "20",
        "budget_seconds": "600",
        "budget_dollars": "0.50",
        "budget_source": "default",
    }
    row = state_of(body["task_id"])
    assert (row["budget_iterations"], row["budget_seconds"], row["budget_dollars"]) == (
        "20",
        "600",
        "0.50",
    )
    assert (row["state"], row["queued_via"], row["budget_source"]) == (
        "queued",
        "cli",
        "default",
    )


def test_given_flags_are_stored_with_budget_source_given_and_a_part_left_out_defaults(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = cli(
        monkeypatch,
        capsys,
        "task",
        "add",
        "tidy the notes",
        "--cwd",
        str(home),
        "--entry",
        "claude",
        "--budget-iterations",
        "5",
        "--budget-dollars",
        "1.25",
        "--allow-dirty",
        "--json",
    )
    assert code == 0, err
    body = json.loads(out)
    assert (body["budget_iterations"], body["budget_seconds"], body["budget_dollars"]) == (
        "5",
        "600",
        "1.25",
    )
    assert body["budget_source"] == "given"
    row = state_of(body["task_id"])
    assert (row["entry"], row["allow_dirty"], row["cwd"]) == ("claude", "1", str(home))
    assert row["budget_nanos"] == "1250000000"


def test_the_text_is_scrubbed_and_the_cwd_made_absolute(home: Path) -> None:
    secret = "gsk" + "_" + "AAAABBBBCCCCDDDDEEEEFFFF"
    conn = db.connect()
    try:
        row = store.queue(conn, f"use {secret} to sync", cwd="relative/dir", via="cli")
    finally:
        conn.close()
    assert secret not in str(row["text"])
    assert Path(str(row["cwd"])).is_absolute()


def test_an_empty_text_and_a_bad_budget_are_refused(home: Path) -> None:
    conn = db.connect()
    try:
        with pytest.raises(PromptError):
            store.queue(conn, "   ", via="console")
        with pytest.raises(ParameterError):
            store.queue(conn, "x", budget_iterations=0, via="cli")
        with pytest.raises(ParameterError):
            store.queue(conn, "x", budget_dollars="lots", via="cli")
        assert store.listing(conn) == ()
    finally:
        conn.close()


# ---- list and show ------------------------------------------------------------------------------


def test_list_and_show_print_the_stores_rows_as_strings(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = queue_one()
    second = queue_one()
    code, out, err = cli(monkeypatch, capsys, "task", "list", "--state", "queued", "--json")
    assert code == 0, err
    listed = json.loads(out)["tasks"]
    assert [row["task_id"] for row in listed] == [second["task_id"], first["task_id"]]
    assert all(isinstance(value, str) or value is None for row in listed for value in row.values())
    code, out, err = cli(monkeypatch, capsys, "task", "list", "--limit", "1", "--json")
    assert [row["task_id"] for row in json.loads(out)["tasks"]] == [second["task_id"]]
    code, out, err = cli(monkeypatch, capsys, "task", "show", str(first["task_id"]), "--json")
    assert code == 0, err
    assert json.loads(out)["task_id"] == first["task_id"]
    code, out, err = cli(monkeypatch, capsys, "task", "show", "task_missing", "--json")
    assert code == 1 and "task_missing" in err


# ---- cancel -------------------------------------------------------------------------------------


def test_cancelling_a_running_task_refuses_and_changes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task = queue_one()
    conn = db.connect()
    conn.execute("UPDATE tasks SET state = 'running' WHERE task_id = ?", (task["task_id"],))
    with pytest.raises(TaskStateError):
        store.cancel(conn, str(task["task_id"]))
    conn.close()
    code, out, err = cli(monkeypatch, capsys, "task", "cancel", str(task["task_id"]))
    assert code == 1
    assert "running: it stops at its budget or when it finishes" in err
    assert state_of(str(task["task_id"]))["state"] == "running"


def test_cancelling_a_queued_task_publishes_task_finished_as_its_own_session(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task = queue_one()
    code, out, err = cli(monkeypatch, capsys, "task", "cancel", str(task["task_id"]), "--json")
    assert code == 0, err
    assert json.loads(out) == {"task_id": task["task_id"], "state": "cancelled"}
    row = state_of(str(task["task_id"]))
    assert (row["state"], row["stop_reason"]) == ("cancelled", "cancelled")
    assert row["session_id"] is not None
    conn = db.connect()
    try:
        events = conn.execute(
            "SELECT seq, name FROM events WHERE run_id = ?", (row["session_id"],)
        ).fetchall()
        assert [tuple(e) for e in events] == [(1, "task.finished")]
        assert read.terminal_for(conn, str(row["session_id"])) == "task.finished"
    finally:
        conn.close()
    again = cli(monkeypatch, capsys, "task", "cancel", str(task["task_id"]))
    assert again[0] == 1 and "cancelled" in again[2]


def test_cancelling_a_waiting_task_denies_its_pending_decision(home: Path) -> None:
    task = queue_one()
    conn = db.connect()
    try:
        decision = pending.record(
            conn,
            session_id=SESSION,
            agent="probe",
            tool_kind="execute",
            description="run ls",
            ceiling_at="2026-01-01T12:10:00Z",
        )
        conn.execute(
            "UPDATE tasks SET state = 'needs_human', pause_reason = 'permission', "
            "paused_at = 't', pending_permission_id = ?, session_id = ? WHERE task_id = ?",
            (decision.permission_id, SESSION, task["task_id"]),
        )

        def deny(permission_id: str) -> None:
            pending.answer(conn, permission_id, "deny", rule="operator", via="cli")

        row = store.cancel(conn, str(task["task_id"]), deny=deny)
        assert (row["state"], row["stop_reason"]) == ("cancelled", "cancelled")
        stored = pending.get(conn, decision.permission_id)
        assert stored is not None
        assert (stored.answer, stored.rule, stored.answered_via) == ("deny", "operator", "cli")
        # The parked process publishes task.finished inside its own message (ticket 240).
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE name = 'task.finished'").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_a_finished_task_is_refused_with_its_state(home: Path) -> None:
    task = queue_one()
    conn = db.connect()
    try:
        conn.execute("UPDATE tasks SET state = 'done' WHERE task_id = ?", (task["task_id"],))
        with pytest.raises(TaskStateError, match="done"):
            store.cancel(conn, str(task["task_id"]))
    finally:
        conn.close()


# ---- ticket 240: a task runs to its budget, pauses for the operator and resumes --------------


def events_of(session_id: str) -> list[tuple[str, dict[str, Any]]]:
    conn = db.connect()
    try:
        return [
            (str(name), json.loads(payload))
            for name, payload in conn.execute(
                "SELECT name, payload FROM events WHERE run_id = ? ORDER BY seq", (session_id,)
            )
        ]
    finally:
        conn.close()


def rows(sql: str, *params: Any) -> list[tuple[Any, ...]]:
    conn = db.connect()
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def run_it(task_id: str, **kwargs: Any) -> dict[str, str | None]:
    return task_run.run_task(task_id, **kwargs)


def test_an_unattended_task_runs_message_by_message_to_done_with_no_operator_message(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red first (ticket 240): 0 operator messages, `human_messages` 0, `done` the router's."""
    decider = ScriptedDecider([answer("groq_chat"), answer("groq_chat"), answer("done")])
    provider = CannedProvider()
    install(monkeypatch, decider, provider)
    task_id = str(queue_one()["task_id"])

    final = run_it(task_id)

    assert (final["state"], final["stop_reason"], final["human_messages"]) == (
        "done",
        "finished",
        "0",
    )
    session_id = str(final["session_id"])
    assert final["started_by"] == "hand" and final["started_at"] is not None
    assert final["pid"] is not None
    # every message was written by the task: one task_messages row each, none by the operator
    kinds = rows(
        "SELECT message_seq, kind FROM task_messages WHERE run_id = ? ORDER BY message_seq",
        session_id,
    )
    assert [kind for _, kind in kinds] == ["first", "continuation", "continuation"]
    starts = [
        seq
        for (seq,) in rows(
            "SELECT seq FROM events WHERE run_id = ? AND name = 'run.started' ORDER BY seq",
            session_id,
        )
    ]
    assert starts == [seq for seq, _ in kinds], "0 operator messages: every message has its row"
    # `done` is offered on a continuation's hop-0 question only
    assert "done" not in decider.route_options(0)
    assert decider.route_options(1)[-1] == "done" and decider.route_options(2)[-1] == "done"
    # the continuation is the fixed sentence with the task's text, stored verbatim
    sent = [
        message.content
        for request in provider.requests
        for message in request.messages
        if message.role == "user"
    ]
    assert sent[0].endswith("tidy the notes")
    assert any(store.CONTINUATION.format(text="tidy the notes") in content for content in sent)
    assert "The task:\ntidy the notes" in decider.states[1]
    capture("done.json", final)


def test_the_done_message_settles_with_no_handler_and_the_checkpoint_precedes_run_finished(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decider = ScriptedDecider([answer("groq_chat"), answer("done")])
    provider = CannedProvider()
    install(monkeypatch, decider, provider)
    final = run_it(str(queue_one()["task_id"]))

    assert final["state"] == "done"
    assert len(provider.requests) == 1, "the done message ran no handler"
    events = events_of(str(final["session_id"]))
    names = [name for name, _ in events]
    assert names.count("task.started") == 1
    assert names[-2:] == ["task.finished", "run.finished"], names
    assert events[-1][1] == {"guardrail": None, "iterations": 0, "stop_reason": "task_done"}
    assert events[-2][1] == {
        "task_id": final["task_id"],
        "state": "done",
        "stop_reason": "finished",
    }
    started = next(payload for name, payload in events if name == "task.started")
    assert started == {"task_id": final["task_id"], "started_by": "hand", "schedule_id": None}


def test_a_done_answer_below_the_floor_is_an_uncertain_stop_not_a_finish(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decider = ScriptedDecider([answer("groq_chat"), answer("done", "0.79")])
    install(monkeypatch, decider, CannedProvider())
    final = run_it(str(queue_one()["task_id"]))
    assert (final["state"], final["pause_reason"], final["pauses"]) == (
        "needs_human",
        "uncertain",
        "1",
    )


def test_an_uncertain_stop_pauses_and_a_resume_with_a_message_is_the_operators(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decider = ScriptedDecider(
        [answer("groq_chat"), answer("needs_human", "0.70"), answer("groq_chat"), answer("done")]
    )
    provider = CannedProvider()
    install(monkeypatch, decider, provider)
    task_id = str(queue_one()["task_id"])

    paused = run_it(task_id)

    assert (paused["state"], paused["pause_reason"], paused["pauses"]) == (
        "needs_human",
        "uncertain",
        "1",
    )
    assert paused["paused_at"] is not None and paused["paused_message_seq"] is not None
    assert paused["stop_reason"] is None and paused["human_messages"] == "0"
    session_id = str(paused["session_id"])
    pause_event = next(p for n, p in events_of(session_id) if n == "task.paused")
    assert pause_event == {
        "task_id": task_id,
        "pause_reason": "uncertain",
        "permission_id": None,
        "route": "needs_human",
        "confidence": "0.7000",
        "confidence_percent": "70%",
        "floor": None,
        "floor_percent": None,
    }

    resumed = run_it(task_id, first_message="use the blue folder", author="operator")

    assert (resumed["state"], resumed["human_messages"]) == ("done", "1")
    assert resumed["session_id"] == session_id, "the same session"
    kinds = rows("SELECT kind FROM task_messages WHERE run_id = ? ORDER BY message_seq", session_id)
    assert kinds == [
        ("first",),
        ("continuation",),
        ("continuation",),
    ], "the operator's message has no task_messages row"
    assert any(
        message.content.endswith("use the blue folder")
        for request in provider.requests
        for message in request.messages
        if message.role == "user"
    )
    capture("paused-then-resumed.json", {"paused": paused, "resumed": resumed})


def test_a_resume_without_a_message_sends_the_next_continuation(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decider = ScriptedDecider([answer("needs_human", "0.70"), answer("groq_chat"), answer("done")])
    install(monkeypatch, decider, CannedProvider())
    task_id = str(queue_one()["task_id"])
    paused = run_it(task_id)
    assert paused["pause_reason"] == "uncertain", "paused on its very first message"
    resumed = run_it(task_id)
    assert (resumed["state"], resumed["human_messages"]) == ("done", "0")


def test_a_router_that_cannot_answer_on_a_continuation_pauses_the_task(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harnessed.errors import DecisionUnreachable

    class Unreachable(ScriptedDecider):
        def decide(self, state: str, questions: Any) -> Any:
            if len(self.states) == 1:
                self.states.append(state)
                raise DecisionUnreachable("no route to the router")
            return super().decide(state, questions)

    install(monkeypatch, Unreachable([answer("groq_chat")]), CannedProvider())
    final = run_it(str(queue_one()["task_id"]))
    assert (final["state"], final["pause_reason"]) == ("needs_human", "uncertain")


def test_run_refuses_what_the_state_does_not_allow(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, ScriptedDecider([answer("groq_chat"), answer("done")]), CannedProvider())
    task_id = str(queue_one()["task_id"])
    with pytest.raises(Exception, match="not started|nobody to answer"):
        run_it(task_id, first_message="hello", author="operator")
    final = run_it(task_id)
    with pytest.raises(TaskStateError, match="already finished"):
        run_it(task_id)
    assert final["state"] == "done"
    with pytest.raises(Exception, match="no task"):
        run_it("task_missing")


def test_route_message_decides_before_anything_is_published() -> None:
    base = {"task_id": "task_x", "session_id": "run_x", "pause_reason": None}
    assert task_run.route_message({**base, "state": "done"}) is False
    assert task_run.route_message({**base, "state": "needs_human", "pause_reason": "uncertain"})
    with pytest.raises(TaskAwaitingAnswer, match="waiting for your answer"):
        task_run.route_message({**base, "state": "needs_human", "pause_reason": "permission"})
    with pytest.raises(RunInFlight):
        task_run.route_message({**base, "state": "running"})


def test_a_message_that_raises_fails_the_task_before_run_finished(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Boom(CannedProvider):
        def complete(self, request: Any) -> Any:
            raise TaskStateError("task_x", "x", "the provider fell over")

    install(monkeypatch, ScriptedDecider([answer("groq_chat")]), Boom())
    task_id = str(queue_one()["task_id"])
    with pytest.raises(TaskStateError, match="fell over"):
        run_it(task_id)
    final = state_of(task_id)
    assert (final["state"], final["stop_reason"]) == ("failed", "raised")
    names = [n for n, _ in events_of(str(final["session_id"]))]
    assert names[-3:] == ["error.raised", "task.finished", "run.finished"], names


def test_task_run_prints_its_state_and_takes_a_json_flag(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, ScriptedDecider([answer("groq_chat"), answer("done")]), CannedProvider())
    task_id = str(queue_one()["task_id"])
    code, out, err = cli(monkeypatch, capsys, "task", "run", task_id, "--json")
    assert code == 0, err
    body = json.loads(out)
    assert (body["state"], body["stop_reason"]) == ("done", "finished")
    code, out, _ = cli(monkeypatch, capsys, "task", "run", "task_missing")
    assert code == 1


# ---- a dirty tree is refused unless the operator says otherwise (ticket 245, FR-146) ------------

ACP_STUB = Path(__file__).parent / "support" / "stub_acp_agent.py"


def dirty_repository(tmp_path: Path) -> Path:
    import subprocess

    folder = tmp_path / "repo"
    folder.mkdir()
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for args in (["init", "-q"], ["add", "seed.txt"], ["commit", "-q", "-m", "seed"]):
        if args[0] == "add":
            (folder / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(folder), *identity, *args], check=True)
    (folder / "stray.txt").write_text("x", encoding="utf-8")
    return folder


def route_to_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from harnessed.acp.config import AgentSpec
    from harnessed.cli import run_cmd

    spec = AgentSpec(name="probe", command=sys.executable, args=(str(ACP_STUB),), timeout_s=20.0)
    monkeypatch.setattr(run_cmd, "_load_agents", lambda _path: {"probe": spec})


def test_a_dirty_tree_fails_the_task_dirty_tree_and_the_agent_never_runs(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harnessed.errors import DirtyTreeRefused

    work = dirty_repository(tmp_path)
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "w.txt")
    install(monkeypatch, ScriptedDecider([answer("probe")]), CannedProvider())
    route_to_probe(monkeypatch)
    task_id = str(queue_one(cwd=str(work))["task_id"])
    with pytest.raises(DirtyTreeRefused):
        run_it(task_id)
    final = state_of(task_id)
    assert (final["state"], final["stop_reason"]) == ("failed", "dirty_tree")
    assert not (work / "w.txt").exists()
    assert rows("SELECT result, result_reason FROM decisions WHERE hop = 0") == [
        ("failed", "dirty_tree")
    ]
    names = [n for n, _ in events_of(str(final["session_id"]))]
    assert names[-3:] == ["error.raised", "task.finished", "run.finished"], names
    capture("dirty-task.json", final)


@pytest.mark.parametrize("stored", [True, False])
def test_allow_dirty_on_the_task_or_on_this_run_lets_the_delegation_run(
    stored: bool, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = dirty_repository(tmp_path)
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "w.txt")
    install(
        monkeypatch,
        ScriptedDecider([answer("probe"), answer("groq_chat"), answer("done")]),
        CannedProvider(),
    )
    route_to_probe(monkeypatch)
    task_id = str(queue_one(cwd=str(work), allow_dirty=stored)["task_id"])
    final = run_it(task_id, allow_dirty=not stored)
    assert (final["state"], final["stop_reason"]) == ("done", "finished")
    assert (work / "w.txt").exists()
    assert rows("SELECT started_dirty FROM repo_records") == [(1,)]


def refused_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: Path | None = None
) -> tuple[str, Path]:
    from harnessed.errors import DirtyTreeRefused

    work = work or dirty_repository(tmp_path)
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "w.txt")
    install(monkeypatch, ScriptedDecider([answer("probe")]), CannedProvider())
    route_to_probe(monkeypatch)
    task_id = str(queue_one(cwd=str(work))["task_id"])
    with pytest.raises(DirtyTreeRefused):
        run_it(task_id)
    return task_id, work


def test_a_dirty_tree_failure_reopens_with_allow_dirty_on_its_own_session(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 269: the console's `task run <id> --allow-dirty` line runs."""
    task_id, work = refused_dirty(tmp_path, monkeypatch)
    refused = state_of(task_id)
    install(
        monkeypatch,
        ScriptedDecider([answer("probe"), answer("groq_chat"), answer("done")]),
        CannedProvider(),
    )
    final = run_it(task_id, allow_dirty=True)
    assert (final["state"], final["stop_reason"]) == ("done", "finished")
    assert final["session_id"] == refused["session_id"]
    assert (work / "w.txt").exists()
    assert int(str(final["used_iterations"])) > int(str(refused["used_iterations"]))


@pytest.mark.parametrize("how", ["raised", "budget", "done", "cancelled"])
def test_allow_dirty_does_not_reopen_any_other_terminal_task(
    how: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harnessed.errors import TaskStateError

    task_id = str(queue_one()["task_id"])
    state, reason = {
        "raised": ("failed", "raised"),
        "budget": ("failed", "budget"),
        "done": ("done", "finished"),
        "cancelled": ("cancelled", "cancelled"),
    }[how]
    conn = db.connect()
    conn.execute(
        "UPDATE tasks SET state = ?, stop_reason = ? WHERE task_id = ?", (state, reason, task_id)
    )
    conn.commit()
    conn.close()
    with pytest.raises(TaskStateError, match="already finished"):
        run_it(task_id, allow_dirty=True)


def test_a_dirty_tree_failure_without_the_flag_says_how_to_go_on(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harnessed.errors import TaskStateError

    task_id, _ = refused_dirty(tmp_path, monkeypatch)
    with pytest.raises(TaskStateError) as raised:
        run_it(task_id)
    text = str(raised.value)
    assert "--allow-dirty" in text and "commit" in text and "already finished" not in text
    assert state_of(task_id)["state"] == "failed"


def test_the_cli_flags_carry_allow_dirty_on_task_run_and_on_run(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = dirty_repository(tmp_path)
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "w.txt")
    install(
        monkeypatch,
        ScriptedDecider([answer("probe"), answer("groq_chat"), answer("done")]),
        CannedProvider(),
    )
    route_to_probe(monkeypatch)
    task_id = str(queue_one(cwd=str(work))["task_id"])
    code, out, err = cli(monkeypatch, capsys, "task", "run", task_id, "--allow-dirty", "--json")
    assert code == 0, err
    assert json.loads(out)["state"] == "done"
    assert (work / "w.txt").exists()
    (work / "w.txt").unlink()

    install(monkeypatch, ScriptedDecider([answer("probe"), answer("groq_chat")]), CannedProvider())
    monkeypatch.chdir(work)
    code, _, err = cli(monkeypatch, capsys, "run", "write it")
    assert code == 1 and "uncommitted changes" in err and not (work / "w.txt").exists()
    install(monkeypatch, ScriptedDecider([answer("probe"), answer("groq_chat")]), CannedProvider())
    code, _, err = cli(monkeypatch, capsys, "run", "--allow-dirty", "write it")
    assert code == 0, err
    assert (work / "w.txt").exists()


# ---- ticket 264: the continuation's route question shows the router the work so far -------------


class _ReplyingProvider(CannedProvider):
    """A canned provider whose every reply is ``reply``."""

    def __init__(self, reply: str) -> None:
        super().__init__()
        self.reply = reply

    def complete(self, request: Any) -> Any:
        response = super().complete(request)
        return type(response)(
            response_id=response.response_id,
            model=response.model,
            content=self.reply,
            usage=response.usage,
        )


def _continuation_state(monkeypatch: pytest.MonkeyPatch, reply: str) -> str:
    decider = ScriptedDecider([answer("groq_chat"), answer("done")])
    install(monkeypatch, decider, _ReplyingProvider(reply))
    run_it(str(queue_one()["task_id"]))
    return decider.states[1]


def test_a_short_reply_reaches_the_continuations_route_question_whole(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = "A hash table maps keys to values through a hash function."
    state = _continuation_state(monkeypatch, reply)

    assert f"assistant: {reply}" in state
    assert "Last reply:" not in state, "an intact reply is not quoted twice"
    assert store.CONTINUATION.format(text="tidy the notes") in state
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "short-reply-question.txt").write_text(state + "\n", encoding="utf-8")


def test_a_reply_cut_by_the_session_digest_is_carried_whole_newest_text_kept(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = "Opening sentence. " + "filler " * 80 + "THE ANSWER ENDS HERE."
    state = _continuation_state(monkeypatch, reply)

    assert "THE ANSWER ENDS HERE." not in state.split("Last reply:")[0], "the digest cut it"
    assert "Last reply:\n" in state
    assert state.split("Last reply:\n")[1].rstrip().endswith("THE ANSWER ENDS HERE.")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "long-reply-question.txt").write_text(state + "\n", encoding="utf-8")


def test_the_first_message_of_a_task_has_no_last_reply_section(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decider = ScriptedDecider([answer("groq_chat"), answer("done")])
    install(monkeypatch, decider, _ReplyingProvider("x" * 900))
    run_it(str(queue_one()["task_id"]))

    assert "Last reply:" not in decider.states[0]


# ---- the list's wire shape, built by column (ticket 265) -----------------------------------------


def test_the_listing_wire_is_the_rows_payload_byte_for_byte_with_its_keys_ascending(
    home: Path,
) -> None:
    from harnessed import serialize

    first = queue_one()["task_id"]
    queue_one()
    queue_one()
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE tasks SET state = 'done', human_messages = 2, pauses = 1, "
            "text = 'tidy \"the\"\\nnotes é' WHERE task_id = ?",
            (first,),
        )
        conn.execute("UPDATE tasks SET state = 'running' WHERE task_id != ?", (first,))
        rows = store.listing(conn, limit=10)
        wire = store.listing_wire(conn, limit=10)
        empty = store.listing_wire(conn, state="failed", limit=10)
    finally:
        conn.close()
    assert len(rows) == 3
    assert wire == store.listing_payload(rows)
    assert wire["tasks"] == [store.json_payload(row) for row in rows]  # type: ignore[index]
    assert serialize.canonical_presorted(wire) == serialize.canonical(wire)
    assert sorted(row["unattended"] or "null" for row in wire["tasks"]) == [  # type: ignore[index]
        "false",
        "null",
        "null",
    ]
    assert empty == {"tasks": []}
    assert store.listing_payload(()) == {"tasks": []}


# ---- ticket 268: an agent hop's reply reaches the continuation's route question -----------------

EVIDENCE_268 = Path(__file__).resolve().parents[1] / "evidence" / "268"


def test_an_agent_hops_reply_reaches_the_continuations_route_question(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decider = ScriptedDecider([answer("probe"), answer("groq_chat"), answer("done")])
    install(monkeypatch, decider, CannedProvider())
    route_to_probe(monkeypatch)
    run_it(str(queue_one()["task_id"]))

    state = decider.states[2]
    EVIDENCE_268.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_268 / "agent-hop-continuation-question.txt").write_text(state + "\n", "utf-8")
    assert "stub agent finished the delegated unit of work" in state, state


@pytest.mark.parametrize("ending", [".", "?", "!", ""])
def test_the_continuation_never_doubles_the_tasks_closing_punctuation(ending: str) -> None:
    text = store.continuation_for(f"tidy the notes{ending}")
    assert ".." not in text and "?." not in text and "!." not in text
    assert text.startswith(f"Continue the task: tidy the notes{ending or ''}")
    assert text.endswith(" Your work so far is above; say when it is finished.")
    assert ("tidy the notes" + (ending or ".") + " Your") in text


# ---- the stored dollars round half-up (ticket 280) ----------------------------------------------


@pytest.mark.parametrize(
    ("nanos", "dollars"),
    [(624_500, "0.000625"), (624_499, "0.000624"), (624_501, "0.000625"), (625_500, "0.000626")],
)
def test_a_checkpoints_used_dollars_round_half_up(home: Path, nanos: int, dollars: str) -> None:
    task_id = str(queue_one()["task_id"])
    conn = db.connect()
    try:
        row = store.checkpoint(
            conn,
            task_id,
            run_id="session-280",
            message_seq=1,
            started_at=clock.isoformat(clock.now()),
            nanos=nanos,
            unpriced=0,
            ended="ok",
        )
    finally:
        conn.close()
    assert (row["used_nanos"], row["used_dollars"]) == (str(nanos), dollars)


def test_every_quantize_under_tasks_names_a_rounding_mode() -> None:
    root = Path(store.__file__).parent
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "quantize":
                named = len(node.args) > 1 or any(k.arg == "rounding" for k in node.keywords)
                assert named, f"{path.name}:{node.lineno} quantize has no rounding mode"
