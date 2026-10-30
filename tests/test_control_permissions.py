"""Ticket 234 (FR-130): the operator answers a permission request through the control app or the
CLI, one compare-and-set decides, and the control worker parks runs rather than blocking on them.

The delegation half (the request reaching the operator, the ceiling, the lease) is
``tests/test_permissions.py``'s; this file is the doors.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harnessed import config, control, gateway
from harnessed.acp import pending
from harnessed.cli.app import main
from harnessed.cli.run_cmd import Opening
from harnessed.control import worker
from harnessed.errors import RunInFlight
from harnessed.events.bus import EventBus
from harnessed.memory import db

SESSION = "run_00000000000000234CONTROL0"


@pytest.fixture
def home(clean_env: None, tmp_harnessed_home: Path, frozen_clock: Any) -> Path:
    conn = db.connect()
    EventBus(run_id=SESSION, conn=conn).publish("run.started", {"provider": "fake"})
    conn.close()
    return tmp_harnessed_home


def seed(**fields: Any) -> pending.Row:
    conn = db.connect()
    try:
        return pending.record(
            conn,
            session_id=SESSION,
            agent="probe",
            tool_kind="execute",
            description="run ls",
            ceiling_at="2026-01-01T12:10:00Z",
            **fields,
        )
    finally:
        conn.close()


def answer_from_cli(
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


# ---- the control door ---------------------------------------------------------------------------


def test_the_console_answers_a_pending_request(home: Path) -> None:
    row = seed()
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        response = client.post(
            f"/control/v1/permissions/{row.permission_id}", json={"answer": "approve"}
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"permission_id": row.permission_id, "state": "approved"}
    conn = db.connect()
    stored = pending.get(conn, row.permission_id)
    conn.close()
    assert stored is not None
    assert (stored.answer, stored.rule, stored.answered_via) == ("approve", "operator", "console")


def test_an_answered_id_is_409_and_names_who_answered_from_the_cli(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    row = seed()
    code, out, err = answer_from_cli(
        monkeypatch, capsys, "permissions", "answer", row.permission_id, "approve"
    )
    assert code == 0, err
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        response = client.post(
            f"/control/v1/permissions/{row.permission_id}", json={"answer": "deny"}
        )
    assert response.status_code == 409
    body = response.json()
    assert set(body) == {"error", "message", "answer", "rule", "answered_via", "answered_at"}
    assert body["error"] == "PermissionAnswered"
    assert (body["answer"], body["rule"], body["answered_via"]) == ("approve", "operator", "cli")
    assert body["answered_at"] == "2026-01-01T12:00:00Z"
    assert body["message"] == "already answered: approved from the cli at 12:00"


def test_the_cli_says_who_answered_first_and_exits_1(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    row = seed()
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        assert (
            client.post(
                f"/control/v1/permissions/{row.permission_id}", json={"answer": "approve"}
            ).status_code
            == 200
        )
    code, out, err = answer_from_cli(
        monkeypatch, capsys, "permissions", "answer", row.permission_id, "deny"
    )
    assert code == 1 and out == ""
    assert "already answered: approved from the console at 12:00" in err


def test_an_unknown_id_is_404_and_a_bad_body_is_400(home: Path) -> None:
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        assert (
            client.post("/control/v1/permissions/perm_nope", json={"answer": "deny"}).status_code
            == 404
        )
        row = seed()
        assert (
            client.post(f"/control/v1/permissions/{row.permission_id}", json={}).status_code == 400
        )
        assert (
            client.post(
                f"/control/v1/permissions/{row.permission_id}", json={"answer": "maybe"}
            ).status_code
            == 400
        )


def test_the_cli_refuses_a_word_that_is_not_an_answer(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    row = seed()
    code, _out, _err = answer_from_cli(
        monkeypatch, capsys, "permissions", "answer", row.permission_id, "maybe"
    )
    assert code != 0


# ---- the read door ------------------------------------------------------------------------------


def test_the_gateway_serves_pending_and_per_session_decisions(home: Path) -> None:
    waiting = seed()
    seed(answer="approve", rule="allowlist")
    application = gateway.create_app(home / db.DATABASE_FILENAME)
    with TestClient(application, raise_server_exceptions=False) as client:
        only = client.get("/v1/permissions?state=pending").json()["permissions"]
        assert [item["permission_id"] for item in only] == [waiting.permission_id]
        every = client.get(f"/v1/permissions?session={SESSION}").json()["permissions"]
        assert [item["state"] for item in every] == ["pending", "allowed"]
        assert client.get("/v1/permissions?session=run_nobody").status_code == 404
        assert client.get("/v1/permissions?state=pending&session=run_nobody").status_code == 404
        assert client.get("/v1/permissions").status_code == 400
        empty = client.get("/v1/permissions?state=expired")
        assert empty.status_code == 200 and empty.json() == {"permissions": []}


# ---- the worker ---------------------------------------------------------------------------------


class Bus:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def publish(self, name: str, payload: Any = None) -> None:
        pass


def opener(run_id: str) -> Any:
    def open_it(opened: Any) -> Any:
        opening = Opening(
            settings=SimpleNamespace(provider="fake", routing_on=lambda: True),  # type: ignore[arg-type]
            conn=SimpleNamespace(close=lambda: None),  # type: ignore[arg-type]
            bus=Bus(run_id),  # type: ignore[arg-type]
        )
        opened(opening)
        return opening

    return open_it


def test_three_parked_runs_and_one_active_make_a_new_run_in_flight() -> None:
    held = worker.Worker()
    release = threading.Event()
    with held.executor:
        try:
            for index in range(pending.PARKED_MAX):
                run_id = f"run_parked_{index}"
                held.start(opener(run_id), lambda session, event: release.wait(10))
                held.park(run_id, True)  # the run waits on the operator: it holds a thread only
            held.start(opener("run_active"), lambda session, event: release.wait(10))
            with pytest.raises(RunInFlight) as refused:
                held.start(opener("run_refused"), lambda session, event: None)
            assert refused.value.session_id == "run_active", "names the run holding the place"
            held.park("run_active", True)
            with pytest.raises(RunInFlight) as full:
                held.start(opener("run_refused"), lambda session, event: None)
            assert full.value.session_id.startswith("run_"), "names the run holding a thread"
            assert full.value.session_id in {"run_active", *(f"run_parked_{i}" for i in range(3))}
        finally:
            release.set()


def test_a_parked_run_does_not_hold_the_place_of_the_active_one() -> None:
    held = worker.Worker()
    release = threading.Event()
    with held.executor:
        try:
            held.start(opener("run_first"), lambda session, event: release.wait(10))
            held.park("run_first", True)
            assert (
                held.start(opener("run_second"), lambda session, event: release.wait(10))
                == "run_second"
            )
        finally:
            release.set()


def test_the_worker_has_room_for_the_active_run_and_every_parked_one() -> None:
    assert worker.Worker().executor._max_workers == 1 + pending.PARKED_MAX  # type: ignore[attr-defined]


def test_the_lifespan_wakes_parked_runs_before_it_waits_on_them() -> None:
    held = worker.Worker()
    with held._serving():
        assert not held.stopped.is_set()
    assert held.stopped.is_set(), "set on the way out, before the executor is waited on"


def test_the_ceiling_setting_defaults_to_600_and_reads_its_variable(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_harnessed_home: Path
) -> None:
    assert config.load(provider="fake").permission_ceiling_s == 600
    monkeypatch.setenv("HARNESSED_PERMISSION_CEILING_S", "42")
    assert config.load(provider="fake").permission_ceiling_s == 42
