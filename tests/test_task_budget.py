"""Ticket 240 (FR-141, decision 77): a task ends on its budget with the figures that ended it.

Each message's own rows are kept at its checkpoint and the budget check reads those same columns,
so what a failed task serves is what stopped it, and a task overspends by at most one message.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from tests.support.task_world import CannedProvider, ScriptedDecider, answer, capture, install

from harnessed.cli import task_run
from harnessed.loop import messages, runs
from harnessed.memory import db
from harnessed.tasks import store


@pytest.fixture
def home(clean_env: None, tmp_harnessed_home: Path, frozen_clock: Any) -> Path:
    return tmp_harnessed_home


def queued(**budget: Any) -> str:
    conn = db.connect()
    try:
        return str(store.queue(conn, "tidy the notes", via="cli", **budget)["task_id"])
    finally:
        conn.close()


def routes(count: int) -> ScriptedDecider:
    return ScriptedDecider([answer("groq_chat") for _ in range(count)])


def test_a_task_that_spends_its_iterations_ends_on_budget_with_those_figures(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, routes(5), CannedProvider())
    final = task_run.run_task(queued(budget_iterations=3))

    assert (final["state"], final["stop_reason"]) == ("failed", "budget")
    assert final["used_iterations"] == "3" and final["budget_iterations"] == "3"
    assert final["finished_at"] is not None
    assert final["unpriced_turns"] == "3", "no price is held, so the turns are counted, not priced"
    assert (final["used_nanos"], final["used_dollars"]) == ("30000", "0.000030"), "three decisions"
    capture("budget.json", final)


def test_a_task_overspends_by_at_most_one_message_and_says_by_how_much(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, routes(5), CannedProvider(tick_s=7.0))
    final = task_run.run_task(queued(budget_seconds=5))

    assert (final["state"], final["stop_reason"]) == ("failed", "budget")
    assert final["used_seconds"] == "7.0", "one message of seven seconds took it past five"
    assert final["used_iterations"] == "1"


def test_a_task_under_every_budget_goes_on_to_the_next_continuation(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decider = ScriptedDecider([answer("groq_chat"), answer("groq_chat"), answer("done")])
    install(monkeypatch, decider, CannedProvider())
    final = task_run.run_task(queued(budget_iterations=10))
    assert (final["state"], final["used_iterations"]) == ("done", "2")


def test_the_dollar_budget_reads_the_priced_nanodollars(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, routes(5), CannedProvider())
    # the router's own reported cost, 0.0000100 a decision, is the only priced spend here
    final = task_run.run_task(queued(budget_dollars="0.000015"))
    assert (final["state"], final["stop_reason"]) == ("failed", "budget")
    assert (final["used_nanos"], final["used_dollars"]) == ("20000", "0.000020")


def _costing(cost: str) -> Any:
    return dataclasses.replace(answer("groq_chat"), cost=cost, usage={"cost": cost})


def test_a_task_adds_each_messages_exact_nanos_and_matches_the_session_total(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0.000028266 + 0.00014535 = 0.000173616: rounding each message first reads 0.000173
    decider = ScriptedDecider([_costing("0.000028266"), _costing("0.00014535"), answer("done")])
    install(monkeypatch, decider, CannedProvider())
    final = task_run.run_task(queued(budget_iterations=10))

    assert final["state"] == "done"
    assert int(str(final["used_nanos"])) == 28266 + 145350 + 10000, "the exact sum, no rounding"
    session = str(final["session_id"])
    conn = db.connect()
    try:
        total = runs.spend_payload(runs.spend(conn, session))["total"]
        assert messages.exact_nanos_for(conn, session, 1) == 28266
    finally:
        conn.close()
    assert final["used_dollars"] == total == "0.000184"
