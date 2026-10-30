"""The other half of a binary outcome: the tool that correctly did **not** fire.

A suite that only ever asserts "the right tool fired" is passed by a model that reaches for a tool
every single turn. This file is the complement - a question that needs no tool must end with zero
tool calls - plus the second guardrail, which is the only other way ``run_loop`` can end.

Both are still 0-or-1 facts. Nothing here is scored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.helpers import (
    Case,
    ScriptedClient,
    ask,
    live_eval,
    load_dataset,
    make_harness,
    text_response,
    tool_response,
)
from evals.results import CaseResult
from harnessed.loop.guardrails import ITERATION_LIMIT, NO_MORE_TOOL_CALLS

NO_TOOL_CASES = [case for case in load_dataset() if case.expect_tool is None]

# ---- offline tier ------------------------------------------------------------------------------


def test_a_question_that_needs_no_tool_ends_with_no_tool_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record,  # type: ignore[no-untyped-def]
) -> None:
    client = ScriptedClient([text_response("Blue.")])
    session = make_harness(tmp_path / "home", monkeypatch, client=client)
    result = ask(session, "Name any colour. Do not use a tool.")
    record(
        CaseResult(
            id="eval_offline_no_tool_needed",
            tier="offline",
            passed=result.tool_calls == (),
            expected="no tool",
            observed=", ".join(call.name for call in result.tool_calls) or "no tool call",
        )
    )
    assert result.tool_calls == ()
    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.iterations == 1
    assert result.reply == "Blue."


def test_a_model_that_never_stops_is_stopped_by_the_second_guardrail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record,  # type: ignore[no-untyped-def]
) -> None:
    """The ceiling is the only other exit, and it is the one that keeps a live run cheap."""
    limit = 3
    client = ScriptedClient(
        [tool_response("fs_write", {"path": f"loop/{n}.md", "content": str(n)}) for n in range(20)]
    )
    session = make_harness(tmp_path / "home", monkeypatch, client=client, max_iterations=limit)
    result = ask(session, "keep writing files forever")
    record(
        CaseResult(
            id="eval_offline_iteration_limit",
            tier="offline",
            passed=result.guardrail == ITERATION_LIMIT,
            expected=ITERATION_LIMIT,
            observed=result.guardrail,
        )
    )
    assert result.guardrail == ITERATION_LIMIT
    assert result.iterations == limit
    assert len(result.tool_calls) == limit


def test_the_sandbox_holds_only_what_the_run_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that writes nothing leaves nothing behind but the database and its WAL files."""
    client = ScriptedClient([text_response("Nothing to do.")])
    session = make_harness(tmp_path / "home", monkeypatch, client=client)
    ask(session, "say nothing")
    left = {path.name for path in session.settings.home.iterdir()}
    # The database, and since ticket 045 the run's trace under evals/. A trace is not litter:
    # it is the one artefact that answers "why did it do that", and a run that left none would
    # be the unreachable-subsystem defect this milestone has now met four times.
    assert left <= {"harness.db", "harness.db-wal", "harness.db-shm", "evals"}
    traces = sorted((session.settings.home / "evals").glob("*.trace.jsonl"))
    assert len(traces) == 1, f"expected exactly one trace for one run, found {traces}"
    assert "harness.db" in left


# ---- live tier - a real model asked something it must answer without reaching for a tool --------
#
# The offline tier above proves the loop ends cleanly when the script says no tool. Only a live
# model can tell you whether a model asked a plain question reaches for one anyway.


@live_eval
@pytest.mark.parametrize("case", NO_TOOL_CASES, ids=lambda case: case.id)
def test_live_a_plain_question_is_answered_without_a_tool(  # type: ignore[no-untyped-def]
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record
) -> None:
    session = make_harness(
        tmp_path / "home",
        monkeypatch,
        provider=None,
        keep=("HARNESSED_MODEL", "HARNESSED_BASE_URL"),
    )
    result = ask(session, case.input)
    fired = tuple(call.name for call in result.tool_calls)
    record(
        CaseResult(
            id=f"{case.id}_plain",
            tier="live",
            passed=not fired and bool(result.reply.strip()),
            expected="no tool, one non-empty reply",
            observed=", ".join(fired) or f"no tool call, reply of {len(result.reply)} characters",
        )
    )
    assert fired == ()
    assert result.reply.strip()
