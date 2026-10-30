"""Ticket 241 (FR-143, decisions 76 and 86): unattended is a ledger fact, with the tasks reads.

``GET /v1/tasks``, ``/v1/tasks/{task_id}`` and ``/v1/tasks/totals`` render ``tasks/store.py`` and
nothing else. Rows are seeded through ``store.queue`` and the same ledger columns a checkpoint
writes (``human_messages``, ``pauses``, ``state``), because a read is proved against the rows, not
against a run; running a task is ticket 240's.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnessed import clock
from harnessed.acp import pending
from harnessed.cli.app import app as cli_app
from harnessed.events.bus import EventBus
from harnessed.gateway import create_app
from harnessed.loop import messages
from harnessed.memory import db
from harnessed.tasks import store

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence" / "241"
WITHIN_S = 0.2
SESSION_A = "run_00000000000000241SESSIONA"
SESSION_B = "run_00000000000000291SESSIONB"
UNHELD = "run_00000000000000241UNHELD00"


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_harnessed_home: Path) -> TestClient:
    return TestClient(
        create_app(tmp_harnessed_home / db.DATABASE_FILENAME), raise_server_exceptions=False
    )


def evidence(name: str, text: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(text, encoding="utf-8")


def tick(seconds: int) -> None:
    from datetime import timedelta

    clock.freeze(clock.now() + timedelta(seconds=seconds))


def add(conn: sqlite3.Connection, text: str = "tidy the notes", **columns: object) -> str:
    """Queue one task, then set the ledger columns a checkpoint would have written."""
    task_id = str(store.queue(conn, text, via="cli")["task_id"])
    if columns:
        names = ", ".join(f"{name} = ?" for name in columns)
        conn.execute(f"UPDATE tasks SET {names} WHERE task_id = ?", (*columns.values(), task_id))
    tick(1)
    return task_id


def read(client: TestClient, path: str) -> dict[str, object]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


# ---- the list ------------------------------------------------------------------------------------


def test_tasks_come_newest_first_as_strings_with_a_reason_beside_each_null(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    first = add(conn)
    second = add(conn, "second", state="done", stop_reason="finished", used_iterations=3)
    body = read(client, "/v1/tasks")
    rows = body["tasks"]
    assert isinstance(rows, list)
    assert [row["task_id"] for row in rows] == [second, first]
    row = rows[0]
    assert row["used_iterations"] == "3"
    assert row["used_seconds"] == "0.0" and row["used_dollars"] == "0.000000"
    assert row["queued_via"] == "cli" and row["human_messages"] == "0" and row["pauses"] == "0"
    assert row["started_by"] is None and row["started_by_reason"]
    assert row["schedule_id"] is None and row["schedule_id_reason"]
    assert row["entry"] is None and row["entry_reason"]
    assert row["stop_reason"] == "finished" and row["stop_reason_reason"] is None
    assert all(value is None or isinstance(value, str) for value in row.values())
    evidence("list.json", json.dumps(body, indent=2, sort_keys=True))


def test_a_bad_state_is_400_a_bad_limit_400_and_an_unknown_session_404(
    client: TestClient,
) -> None:
    state = client.get("/v1/tasks?state=maybe")
    assert (state.status_code, state.json()["error"]) == (400, "ParameterError")
    for limit in ("0", "-1", "many"):
        bad = client.get(f"/v1/tasks?limit={limit}")
        assert (bad.status_code, bad.json()["error"]) == (400, "LimitError")
    assert client.get(f"/v1/tasks?session={UNHELD}").status_code == 404
    assert client.get("/v1/tasks/task_missing").status_code == 404


def test_the_limit_bounds_the_listing(conn: sqlite3.Connection, client: TestClient) -> None:
    for number in range(3):
        add(conn, f"task {number}")
    assert len(read(client, "/v1/tasks?limit=2")["tasks"]) == 2  # type: ignore[arg-type]


def test_needs_human_is_longest_waiting_first_and_every_other_listing_newest_first(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    late = add(conn, "late", state="needs_human", pause_reason="uncertain", paused_at="2026-01-03")
    early = add(
        conn, "early", state="needs_human", pause_reason="uncertain", paused_at="2026-01-01"
    )
    mid = add(conn, "mid", state="needs_human", pause_reason="uncertain", paused_at="2026-01-02")
    waiting = read(client, "/v1/tasks?state=needs_human")["tasks"]
    assert [row["task_id"] for row in waiting] == [early, mid, late]  # type: ignore[index]
    everything = read(client, "/v1/tasks")["tasks"]
    assert [row["task_id"] for row in everything] == [mid, early, late]  # type: ignore[index]


def test_a_session_that_is_a_tasks_answers_that_task_else_an_empty_list(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    mine = add(conn, session_id=SESSION_A)
    add(conn, "another", session_id="run_00000000000000241SESSIONB")
    EventBus(run_id="run_00000000000000241PLAINSS", conn=conn).publish("run.started", {})
    only = read(client, f"/v1/tasks?session={SESSION_A}")["tasks"]
    assert [row["task_id"] for row in only] == [mine]  # type: ignore[index]
    assert read(client, "/v1/tasks?session=run_00000000000000241PLAINSS") == {"tasks": []}


# ---- the pause fields ----------------------------------------------------------------------------


def test_a_permission_pause_serves_the_request_it_waits_on(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    request = pending.record(
        conn,
        session_id=SESSION_A,
        agent="recorded-agent",
        tool_kind="edit",
        description="edit a.py",
    )
    task_id = add(
        conn,
        session_id=SESSION_A,
        state="needs_human",
        pause_reason="permission",
        paused_at="2026-01-01",
        pending_permission_id=request.permission_id,
        paused_message_seq=1,
    )
    row = read(client, f"/v1/tasks/{task_id}")
    assert row["pending_permission_id"] == request.permission_id
    assert (row["pause_agent"], row["pause_tool_kind"], row["pause_description"]) == (
        "recorded-agent",
        "edit",
        "edit a.py",
    )
    assert row["pause_route"] is None and row["pause_route_reason"]


def test_an_uncertain_pause_serves_the_decision_it_stopped_at(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    for idx, (confidence, percent) in enumerate((("0.9000", "90%"), ("0.4100", "41%"))):
        conn.execute(
            "INSERT INTO decisions (run_id, idx, message_seq, hop, task, outcome, route, handler, "
            "confidence, confidence_percent, floor, floor_percent, state_truncated, "
            "latency_seconds, model, created_at) VALUES "
            "(?, ?, 5, ?, 'x', 'uncertain', 'recorded-agent', "
            "NULL, ?, ?, '0.7000', '70%', 0, '0.1', 'm', '2026-01-01T00:00:00Z')",
            (SESSION_A, idx + 1, idx, confidence, percent),
        )
    task_id = add(
        conn,
        session_id=SESSION_A,
        state="needs_human",
        pause_reason="uncertain",
        paused_at="2026-01-01",
        paused_message_seq=5,
    )
    row = read(client, f"/v1/tasks/{task_id}")
    assert row["pause_route"] == "recorded-agent"
    assert (row["pause_confidence"], row["pause_confidence_percent"]) == ("0.4100", "41%")
    assert (row["pause_floor"], row["pause_floor_percent"]) == ("0.7000", "70%")
    assert row["pause_agent"] is None and row["pause_agent_reason"]
    assert (row["pause_compared"], row["pause_compared_reason"]) == ("confidence", None)
    assert row["pause_agent"] is None


def _pause_at_done(conn: sqlite3.Connection, session: str, stored: str, done: str) -> str:
    """A task paused uncertain at its continuation's hop 0, which routed ``done`` with ``stored``
    as its confidence while its ``done`` option was priced ``done`` (ticket 291)."""
    conn.execute(
        "INSERT INTO decisions (run_id, idx, message_seq, hop, task, outcome, route, handler, "
        "confidence, confidence_percent, floor, floor_percent, state_truncated, "
        "latency_seconds, model, created_at) VALUES "
        "(?, 0, 7, 0, 'x', 'uncertain', 'done', NULL, ?, '78%', '0.8000', '80%', 0, '0.1', 'm', "
        "'2026-01-01T00:00:00Z')",
        (session, stored),
    )
    conn.execute(
        "INSERT INTO decision_options (run_id, idx, question, position, option, probability, "
        "probability_percent) VALUES (?, 0, 'route', 0, 'done', ?, '0%')",
        (session, done),
    )
    task_id = add(
        conn,
        session_id=session,
        state="needs_human",
        pause_reason="uncertain",
        paused_at="2026-01-01",
        paused_message_seq=7,
    )
    conn.execute(
        "INSERT INTO task_messages (run_id, message_seq, task_id, kind) "
        "VALUES (?, 7, ?, 'continuation')",
        (session, task_id),
    )
    return task_id


def test_an_uncertain_pause_at_a_done_continuation_serves_which_figure_it_compared(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    after = _pause_at_done(conn, SESSION_A, "0.7800", "0.7800")
    before = _pause_at_done(conn, SESSION_B, "0.7800", "0.8200")
    assert read(client, f"/v1/tasks/{after}")["pause_compared"] == "done_probability"
    assert read(client, f"/v1/tasks/{before}")["pause_compared"] == "confidence"
    listed = {row["task_id"]: row for row in client.get("/v1/tasks").json()["tasks"]}
    assert listed[after]["pause_compared"] == "done_probability"
    assert listed[before]["pause_compared"] == "confidence"


def test_a_task_that_is_not_paused_uncertain_serves_no_compared_figure_and_says_why(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    task_id = add(conn, session_id=SESSION_A, state="running")
    row = read(client, f"/v1/tasks/{task_id}")
    assert row["pause_compared"] is None and row["pause_compared_reason"]


def _decide(
    conn: sqlite3.Connection, idx: int, seq: int, handler: str | None, outcome: str, result: str
) -> None:
    conn.execute(
        "INSERT INTO decisions (run_id, idx, message_seq, hop, task, outcome, route, handler, "
        "result, state_truncated, latency_seconds, model, created_at) VALUES "
        "(?, ?, ?, 0, 'x', ?, 'recorded-agent', ?, ?, 0, '0.1', 'm', '2026-01-01T00:00:00Z')",
        (SESSION_A, idx, seq, outcome, handler, result),
    )


def test_a_task_row_names_the_last_messages_handler_and_outcome(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    _decide(conn, 1, 1, "agent", "routed", "succeeded")
    _decide(conn, 2, 2, "groq", "routed", "succeeded")
    task_id = add(conn, session_id=SESSION_A, last_message_seq=2)
    row = read(client, f"/v1/tasks/{task_id}")
    assert (row["handler"], row["outcome"]) == ("groq", "routed")
    assert row["handler_reason"] is None and row["outcome_reason"] is None
    listed = read(client, "/v1/tasks")["tasks"]
    assert isinstance(listed, list)
    assert (listed[0]["handler"], listed[0]["outcome"]) == ("groq", "routed")
    assert store.get(conn, task_id) is not None


def test_a_failed_hop_makes_the_outcome_failed(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    _decide(conn, 1, 1, "groq", "routed", "failed")
    _decide(conn, 2, 1, "agent", "routed", "succeeded")
    task_id = add(conn, session_id=SESSION_A, last_message_seq=1)
    row = read(client, f"/v1/tasks/{task_id}")
    assert (row["handler"], row["outcome"]) == ("agent", "failed")


def test_a_queued_task_has_no_handler_or_outcome_and_says_why(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    row = read(client, f"/v1/tasks/{add(conn)}")
    assert row["handler"] is None and row["handler_reason"]
    assert row["outcome"] is None and row["outcome_reason"]


# ---- unattended ----------------------------------------------------------------------------------


def test_unattended_is_true_for_a_clean_done_false_after_a_pause_and_null_before_done(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    clean = add(conn, state="done", stop_reason="finished")
    resumed = add(conn, state="done", stop_reason="finished", pauses=1)
    spoken = add(conn, state="done", stop_reason="finished", human_messages=1)
    running = add(conn, state="running")
    served = {
        task_id: read(client, f"/v1/tasks/{task_id}")
        for task_id in (clean, resumed, spoken, running)
    }
    assert served[clean]["unattended"] == "true"
    assert served[resumed]["unattended"] == "false"
    assert served[spoken]["unattended"] == "false"
    assert served[running]["unattended"] is None
    assert served[running]["unattended_reason"] == "not done yet"
    totals = read(client, "/v1/tasks/totals")
    assert totals["completed_unattended"] == "1"


def test_the_row_and_the_total_and_the_messages_read_share_one_expression() -> None:
    assert "UNATTENDED_SQL" in dir(store)
    assert store.UNATTENDED_SQL == messages._UNATTENDED_SQL


# ---- the totals ----------------------------------------------------------------------------------


def test_totals_count_every_state_all_time(conn: sqlite3.Connection, client: TestClient) -> None:
    assert read(client, "/v1/tasks/totals") == {
        "completed_unattended": "0",
        "needs_human": "0",
        "running": "0",
        "queued": "0",
        "done": "0",
        "failed": "0",
        "cancelled": "0",
        "counted_at": clock.isoformat(clock.now()),
    }
    add(conn)
    add(conn, state="done", stop_reason="finished")
    add(conn, state="done", stop_reason="finished", pauses=2)
    add(conn, state="failed", stop_reason="budget")
    add(conn, state="cancelled", stop_reason="cancelled")
    add(conn, state="running")
    add(conn, state="needs_human", pause_reason="uncertain", paused_at="2026-01-01")
    body = read(client, "/v1/tasks/totals")
    assert {key: body[key] for key in body if key != "counted_at"} == {
        "completed_unattended": "1",
        "needs_human": "1",
        "running": "1",
        "queued": "1",
        "done": "2",
        "failed": "1",
        "cancelled": "1",
    }


# ---- the twins -----------------------------------------------------------------------------------


def test_the_twins_print_the_routes_bytes(
    conn: sqlite3.Connection, client: TestClient, tmp_harnessed_home: Path
) -> None:
    task_id = add(conn, session_id=SESSION_A)
    add(conn, state="done", stop_reason="finished")
    runner = CliRunner(mix_stderr=False)
    for argv, path in (
        (["task", "list", "--json"], "/v1/tasks"),
        (["task", "list", "--session", SESSION_A, "--json"], f"/v1/tasks?session={SESSION_A}"),
        (
            ["task", "list", "--state", "done", "--limit", "1", "--json"],
            "/v1/tasks?state=done&limit=1",
        ),
        (["task", "show", task_id, "--json"], f"/v1/tasks/{task_id}"),
        (["task", "totals", "--json"], "/v1/tasks/totals"),
    ):
        result = runner.invoke(cli_app, argv)
        assert result.exit_code == 0, result.stderr
        assert result.stdout.encode() == client.get(path).content + b"\n", argv


# ---- 1,000 tasks within 200 ms -------------------------------------------------------------------


def test_each_tasks_read_answers_within_200_ms_over_1000_tasks(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    template = store.queue(conn, "seed", via="cli")
    conn.execute("DELETE FROM tasks")
    columns = [name for name in template if name in _SEEDABLE]
    rows = []
    for number in range(1000):
        state = ("queued", "running", "needs_human", "done", "failed", "cancelled")[number % 6]
        row = dict(template)
        row.update(
            task_id=f"task_{number:06d}",
            state=state,
            created_at=f"2026-01-01T00:{number // 60:02d}:{number % 60:02d}Z",
            session_id=f"run_{number:026d}",
            stop_reason={"done": "finished", "failed": "budget", "cancelled": "cancelled"}.get(
                state
            ),
            pause_reason="uncertain" if state == "needs_human" else None,
            paused_at="2026-01-01" if state == "needs_human" else None,
            paused_message_seq="1" if state == "needs_human" else None,
        )
        rows.append(tuple(row[name] for name in columns))
    conn.executemany(
        f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", rows
    )
    lines = []
    for path in (
        "/v1/tasks?limit=1000",
        "/v1/tasks?state=needs_human&limit=1000",
        "/v1/tasks/totals",
        "/v1/tasks/task_000007",
        "/v1/tasks?session=run_00000000000000000000000007",
    ):
        started = time.perf_counter()
        response = client.get(path)
        took = time.perf_counter() - started
        assert response.status_code == 200, response.text
        lines.append(f"{path}: {took * 1000:.1f} ms")
        assert took < WITHIN_S, f"{path} took {took:.3f}s"
    evidence("timing-1000-tasks.txt", "\n".join(lines) + "\n")


_SEEDABLE = frozenset(
    {
        "task_id",
        "text",
        "cwd",
        "budget_iterations",
        "budget_seconds",
        "budget_dollars",
        "budget_nanos",
        "budget_source",
        "state",
        "stop_reason",
        "pause_reason",
        "paused_at",
        "paused_message_seq",
        "session_id",
        "queued_via",
        "created_at",
    }
)


def test_the_tasks_compared_expression_is_the_routers_word_for_word() -> None:
    from harnessed.router import decisions as router_decisions

    assert store.COMPARED_SQL == router_decisions.COMPARED_SQL
