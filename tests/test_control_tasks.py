"""Ticket 239 (FR-140): ``POST /control/v1/tasks`` queues a task through the call the CLI makes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harnessed import control
from harnessed.memory import db
from harnessed.tasks import store


@pytest.fixture
def home(clean_env: None, tmp_harnessed_home: Path, frozen_clock: Any) -> Path:
    return tmp_harnessed_home


def test_the_console_queues_a_task_with_the_default_budget(home: Path) -> None:
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        response = client.post("/control/v1/tasks", json={"text": "tidy the notes"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["task_id"].startswith("task_")
    assert {key: body[key] for key in body if key != "task_id"} == {
        "state": "queued",
        "budget_iterations": "20",
        "budget_seconds": "600",
        "budget_dollars": "0.50",
        "budget_source": "default",
        "queued_via": "console",
    }
    conn = db.connect()
    try:
        row = store.get(conn, body["task_id"])
    finally:
        conn.close()
    assert row is not None and row["queued_via"] == "console"


def test_a_budget_in_the_body_is_not_read(home: Path) -> None:
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        response = client.post(
            "/control/v1/tasks", json={"text": "x", "budget_iterations": 1, "budget_dollars": "9"}
        )
    assert response.status_code == 201
    assert response.json()["budget_iterations"] == "20"
    assert response.json()["budget_source"] == "default"


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "  "}, {"text": 3}, [], None])
def test_an_empty_text_is_400_prompt_error_and_writes_nothing(home: Path, body: Any) -> None:
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        response = client.post("/control/v1/tasks", json=body)
    assert response.status_code == 400
    assert response.json()["error"] == "PromptError"
    conn = db.connect()
    try:
        assert store.listing(conn) == ()
    finally:
        conn.close()
