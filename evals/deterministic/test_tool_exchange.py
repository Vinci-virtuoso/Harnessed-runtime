"""AC 8 and AC 10 - the two-turn tool exchange, offline against a script and live against a model.

This is the file the whole suite exists for, and the two tiers below the banner are the reason
waku keeps them in one file: the offline tier shows what *our code* guarantees, and the live tier
shows what only a real model can tell you. Reading them one after the other is how you see which
is which.

The offline tier asserts **four binary facts and no score**:

1. the tool fired, and it was the tool that was asked for;
2. the arguments carried, unchanged, from the model's request to the handler;
3. the loop ended on the named guardrail rather than by running out of iterations;
4. the artifact reached the database - a ``tool.call.finished`` row on this run's bus.

The live tier drives the same real loop over every case in ``dataset.jsonl`` and asserts the
expectation the case declares. Both tiers record their outcome through ``results.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.helpers import (
    Case,
    ask,
    live_eval,
    load_dataset,
    make_harness,
    text_response,
    tool_response,
)
from evals.results import CaseResult
from harnessed.loop.guardrails import NO_MORE_TOOL_CALLS
from harnessed.providers.types import ToolCall

WRITE_PATH = "notes/colour.md"
WRITE_BODY = "blue"

# ---- offline tier - a scripted exchange, every fact binary --------------------------------------


def scripted_write_then_answer() -> list[object]:
    return [
        tool_response("fs_write", {"path": WRITE_PATH, "content": WRITE_BODY}),
        text_response(f"I wrote {WRITE_BODY} to {WRITE_PATH}."),
    ]


def events_named(conn: object, run_id: str, name: str) -> list[dict[str, object]]:
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT payload FROM events WHERE run_id = ? AND name = ? ORDER BY seq", (run_id, name)
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


@pytest.fixture
def exchange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from evals.helpers import ScriptedClient

    client = ScriptedClient(scripted_write_then_answer())
    session = make_harness(tmp_path / "home", monkeypatch, client=client)
    result = ask(session, "Write the colour blue into notes/colour.md, then tell me you did.")
    return session, result


def test_fact_one_the_tool_fired(exchange, record) -> None:  # type: ignore[no-untyped-def]
    session, result = exchange
    fired = tuple(call.name for call in result.tool_calls)
    record(
        CaseResult(
            id="eval_offline_tool_fired",
            tier="offline",
            passed=fired == ("fs_write",),
            expected="fs_write",
            observed=", ".join(fired) or "no tool call",
        )
    )
    assert fired == ("fs_write",)


def test_fact_two_the_arguments_carried(exchange, record) -> None:  # type: ignore[no-untyped-def]
    session, result = exchange
    arguments = dict(result.tool_calls[0].arguments)
    record(
        CaseResult(
            id="eval_offline_arguments_carried",
            tier="offline",
            passed=arguments.get("path") == WRITE_PATH,
            expected=WRITE_PATH,
            observed=str(arguments.get("path")),
        )
    )
    assert arguments == {"path": WRITE_PATH, "content": WRITE_BODY}


def test_fact_three_the_loop_ended_on_the_named_guardrail(exchange, record) -> None:  # type: ignore[no-untyped-def]
    session, result = exchange
    record(
        CaseResult(
            id="eval_offline_guardrail",
            tier="offline",
            passed=result.guardrail == NO_MORE_TOOL_CALLS,
            expected=NO_MORE_TOOL_CALLS,
            observed=result.guardrail,
        )
    )
    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.iterations == 2


def test_fact_four_the_artifact_reached_the_database(exchange, record) -> None:  # type: ignore[no-untyped-def]
    session, result = exchange
    finished = events_named(session.conn, session.bus.run_id, "tool.call.finished")
    record(
        CaseResult(
            id="eval_offline_artifact_in_database",
            tier="offline",
            passed=len(finished) == 1,
            expected="one tool.call.finished row",
            observed=f"{len(finished)} rows",
        )
    )
    assert [row["name"] for row in finished] == ["fs_write"]


def test_the_artifact_also_reached_the_sandbox_and_nowhere_else(exchange) -> None:  # type: ignore[no-untyped-def]
    """The fifth fact, kept out of the four because it is about the sandbox, not the loop."""
    session, _ = exchange
    written = session.settings.home / WRITE_PATH
    assert written.read_text(encoding="utf-8") == WRITE_BODY


def test_an_exhausted_script_is_a_test_bug_not_a_silent_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One response for a two-turn exchange must fail loudly, or a short script would look green."""
    from evals.helpers import ScriptedClient, ScriptExhaustedError

    client = ScriptedClient([tool_response("fs_write", {"path": "a.md", "content": "x"})])
    session = make_harness(tmp_path / "home", monkeypatch, client=client)
    with pytest.raises(ScriptExhaustedError):
        ask(session, "write something")


def test_the_registry_the_loop_saw_is_the_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duck-typed stand-in registry would make every assertion above meaningless.

    Reads ``loop_requests``, not ``requests[0]``. Since ticket 044 the eval path runs the product's
    own composition, and that asks the provider for the retrieval gate's decision first - a
    request carrying no tools at all, because the gate is not the loop.
    """
    from evals.helpers import ScriptedClient

    client = ScriptedClient([text_response("nothing to do")])
    session = make_harness(tmp_path / "home", monkeypatch, client=client)
    ask(session, "say nothing")
    declared = {schema.name for schema in client.loop_requests[0].tools}
    assert {"fs_read", "fs_write"} <= declared


def test_an_unknown_tool_comes_back_as_a_result_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four outcomes, one shape. The loop has no branch for this and must not grow one."""
    from evals.helpers import ScriptedClient

    client = ScriptedClient([tool_response("no_such_tool", {}), text_response("sorry")])
    session = make_harness(tmp_path / "home", monkeypatch, client=client)
    result = ask(session, "use a tool that does not exist")
    assert result.guardrail == NO_MORE_TOOL_CALLS
    failed = events_named(session.conn, session.bus.run_id, "tool.call.failed")
    assert len(failed) == 1


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=dict(arguments))


def case_named(case_id: str) -> Case:
    """One committed case, by id. Raises rather than skipping if the dataset stopped carrying it."""
    return next(case for case in load_dataset() if case.id == case_id)


WRITE_CASE = "eval_write_colour_note"
PLAIN_CASE = "eval_plain_colour_answer"


def test_a_case_that_got_what_it_asked_for_reports_no_failures() -> None:
    """:func:`live_failures` is the live tier's whole judgement, and it is pure. So it is tested
    here, offline, where a key is not needed: only the *calling* of a real model needs one, and a
    grader that has never been run against a wrong answer is not a grader."""
    case = case_named(WRITE_CASE)
    assert live_failures(case, (call("fs_write", path="notes/colour.md", content="blue"),)) == []


def test_the_wrong_tool_is_reported_by_both_names() -> None:
    case = case_named(WRITE_CASE)
    (problem,) = live_failures(case, (call("fs_read", path="notes/colour.md"),))
    assert "fs_write" in problem
    assert "fs_read" in problem


def test_no_tool_at_all_is_reported_rather_than_passing_quietly() -> None:
    case = case_named(WRITE_CASE)
    (problem,) = live_failures(case, ())
    assert "no tool call" in problem


def test_a_missing_needle_is_named_and_the_match_is_case_insensitive() -> None:
    case = case_named(WRITE_CASE)
    assert live_failures(case, (call("fs_write", path="NOTES/COLOUR.MD", content="BLUE"),)) == []
    (problem,) = live_failures(case, (call("fs_write", path="notes/colour.md", content="red"),))
    assert "blue" in problem


def test_too_few_calls_is_its_own_failure() -> None:
    case = replace_min_calls(case_named(WRITE_CASE), minimum=2)
    (problem,) = live_failures(case, (call("fs_write", path="notes/colour.md", content="blue"),))
    assert "at least 2" in problem


def test_a_no_tool_case_fails_when_the_model_reaches_for_one() -> None:
    case = case_named(PLAIN_CASE)
    assert live_failures(case, ()) == []
    (problem,) = live_failures(case, (call("fs_read", path="anything"),))
    assert "expected no tool" in problem


def replace_min_calls(case: Case, *, minimum: int) -> Case:
    return Case(
        id=case.id,
        input=case.input,
        expect_tool=case.expect_tool,
        expect_in_args=case.expect_in_args,
        expect_min_tool_calls=minimum,
        setup_fact=case.setup_fact,
    )


# ---- live tier - the same loop, a real model, one case per dataset line -------------------------
#
# Everything above this line is what our own code guarantees. Everything below it is what only a
# real model can tell you, and it is skipped - with a reason - until both gates are open.


@live_eval
@pytest.mark.parametrize("case", load_dataset(), ids=lambda case: case.id)
def test_live_case_meets_its_declared_expectation(  # type: ignore[no-untyped-def]
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record
) -> None:
    session = make_harness(
        tmp_path / "home",
        monkeypatch,
        provider=None,
        keep=("HARNESSED_MODEL", "HARNESSED_BASE_URL"),
    )
    if case.setup_fact is not None:
        seeded = session.settings.home / case.setup_fact["path"]
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(case.setup_fact["content"], encoding="utf-8")

    result = ask(session, case.input)
    fired = tuple(call.name for call in result.tool_calls)
    failures = live_failures(case, result.tool_calls)
    record(
        CaseResult(
            id=case.id,
            tier="live",
            passed=not failures,
            expected=case.expect_tool or "no tool",
            observed=", ".join(fired) or "no tool call",
            detail="; ".join(failures),
        )
    )
    assert not failures, "; ".join(failures)


def live_failures(case: Case, calls: tuple[ToolCall, ...]) -> list[str]:
    """Every declared expectation this case missed, as sentences. Empty means the case passed.

    Collected rather than asserted one at a time so a live run reports the whole picture of one
    expensive call instead of the first thing that went wrong.
    """
    problems: list[str] = []
    fired = tuple(call.name for call in calls)
    if case.expect_tool is None:
        if fired:
            problems.append(f"expected no tool, got {', '.join(fired)}")
        return problems

    matching = [call for call in calls if call.name == case.expect_tool]
    if not matching:
        problems.append(f"expected {case.expect_tool}, got {', '.join(fired) or 'no tool call'}")
        return problems
    if len(calls) < case.expect_min_tool_calls:
        problems.append(f"expected at least {case.expect_min_tool_calls} calls, got {len(calls)}")
    rendered = json.dumps(dict(matching[0].arguments), sort_keys=True).lower()
    problems.extend(
        f"{needle!r} missing from the arguments of {case.expect_tool}"
        for needle in case.expect_in_args
        if needle.lower() not in rendered
    )
    return problems
