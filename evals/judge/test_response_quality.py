"""The judge tier: 0-1 percentages against a declared threshold, never 0/1 truths.

Read the deterministic tier first, then this, and the difference is the point. A deterministic eval
says the right tool fired with the right arguments and the artifact exists - a fact. A judge eval
says a reply scored 0.72 against a rubric - an opinion with a number on it. Both are useful and
they are not interchangeable, and **only the first kind may ever gate a promotion**.

Every ``deepeval`` import in this file is inside a test body, so the module imports cleanly on a
base install where the ``[eval]`` extra was never installed. Without both the extra and a
configured key, every test here skips with a reason naming what is missing.

This tier is not part of any ``verify`` command. That is deliberate: a gate whose result is a
percentage is not a gate.

**The first test is what makes the second one mean anything (ticket 043).** A judge that returns
the same number for every input satisfies any assertion of the form ``score < threshold``. This
tier did exactly that on its first live run: a correct answer and a deliberately padded one both
scored 0.0, and it reported one pass. So the discrimination test measures a known-good and a
known-bad output through one metric configuration and asserts the **distance** between them. No
constant clears a distance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evals.helpers import ask, live_eval, make_harness
from evals.judge.judge_model import MARGIN, THRESHOLD, available
from evals.results import CaseResult

needs_judge = pytest.mark.skipif(
    not available(),
    reason=(
        "the judge tier needs the [eval] extra (uv sync --all-extras) and a configured provider "
        "key; deepeval is never a base install"
    ),
)

ANSWER_TASK = "In one sentence, explain what a tool call is to somebody who has never seen one."

#: What the judge is asked. Rewritten in ticket 043.
#:
#: The previous wording asked whether the output explained "that the model asked the harness to run
#: a named function with arguments and got a result back". A judge read that as a demand to
#: **report a call that had happened**, and failed a correct definition for being a definition -
#: with the reason "the input does not request a function call, yet the evaluation expects a brief
#: statement that the model asked the harness to run a named function". Under that rubric no
#: explanation of the concept could ever score above zero, so the metric marked every model bad
#: forever and looked like a model problem.
#:
#: The subject of the sentence is now the explanation rather than the event, and the rubric says
#: outright that no actual call is expected. A rubric is a prompt, and a prompt that can be read
#: two ways will be read the wrong way by some model on some run.
RUBRIC = (
    "The actual output should explain the CONCEPT of a tool call to somebody who has never seen "
    "one. Score it on whether a newcomer would come away understanding that the model can hand "
    "work to the surrounding program - running some named operation such as reading a file or "
    "doing a calculation - and then carry on using whatever that operation returned. Plain "
    "language, about one sentence. The output is a description, not a report: it is NOT expected "
    "to mention any call that actually took place, and it must not be penalised for defining "
    "rather than reporting, nor for its formatting or layout. Penalise vendor jargon, padding, "
    "and anything longer than two sentences."
)

#: A correct answer, phrased as a definition, quoted verbatim from a live reply observed on
#: 2026-08-26. Scripted rather than solicited for the same reason the bad answer is: asking a model
#: to be good is a second thing that can fail, and the claim here is about the metric.
GOOD_ANSWER = (
    "A tool call is a program's way of pausing its own reasoning to automatically run a specific "
    "external function - like reading a file or performing a calculation - and then resume with "
    "the function's result."
)

#: A second correct answer, worded as differently from the first as the concept allows.
#:
#: It exists because :data:`GOOD_ANSWER` is quoted from a real reply, and a rubric rewritten until
#: that one string passes is a rubric fitted to a string rather than to the question. Two good
#: answers that share no phrasing and both clear the threshold is the cheap evidence that the
#: criteria describe the concept. If a future rubric edit passes one and fails the other, the edit
#: has narrowed the question without meaning to.
ALTERNATE_GOOD_ANSWER = (
    "Think of it as the assistant handing a job to the computer it runs on - it says which "
    "operation it wants and what to apply it to, waits for the answer, and keeps going from there."
)

#: The discriminating half. Jargon, padding, and no explanation inside it.
BAD_ANSWER = (
    "Tool calls leverage a synergistic orchestration substrate across the agentic stack, "
    "enabling end-to-end workflow amplification at scale, and much more besides, in ways "
    "that are frankly difficult to summarise briefly."
)


def _measure(output: str) -> tuple[float, str]:
    """Score one output against the rubric; hand back the number and the judge's own reason.

    Both sides of the discrimination test go through this function, which makes "the same metric
    configuration" a property of the code rather than a claim in a docstring. A fresh metric
    object per call, because ``GEval`` stores its last result on itself - the configuration is
    what has to be identical, and here it is built from one expression.
    """
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams

    from evals.judge.judge_model import judge_model

    metric = GEval(
        name="plain explanation",
        criteria=RUBRIC,
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        model=judge_model(),
        threshold=THRESHOLD,
    )
    metric.measure(LLMTestCase(input=ANSWER_TASK, actual_output=output))
    assert metric.score is not None, "the judge returned no score at all"
    assert 0.0 <= metric.score <= 1.0, f"score {metric.score} is outside 0-1"
    return float(metric.score), str(metric.reason or "")


@live_eval
@needs_judge
def test_the_judge_separates_a_good_answer_from_a_bad_one(record: Any) -> None:
    """The tier's own falsifiability check, and the only test here that can catch a stuck judge.

    Three assertions, each failing a different broken judge: one that passes everything fails the
    second, one that fails everything fails the first, and one returning **any** constant fails
    the third. The previous version of this file asserted only the second, which is how a judge
    scoring 0.0 for both inputs reported success.
    """
    good, good_reason = _measure(GOOD_ANSWER)
    other, other_reason = _measure(ALTERNATE_GOOD_ANSWER)
    bad, bad_reason = _measure(BAD_ANSWER)
    spread = min(good, other) - bad

    record(
        CaseResult(
            id="eval_judge_discriminates",
            tier="judge",
            passed=good >= THRESHOLD
            and other >= THRESHOLD
            and bad < THRESHOLD
            and spread >= MARGIN,
            expected=f"both good >= {THRESHOLD}, bad < {THRESHOLD}, spread >= {MARGIN}",
            observed=f"good={good} alternate={other} bad={bad} spread={round(spread, 3)}",
            detail=f"good: {good_reason} || alternate: {other_reason} || bad: {bad_reason}",
        )
    )

    assert good >= THRESHOLD, f"the reference answer scored {good}. Judge reason: {good_reason}"
    assert other >= THRESHOLD, (
        f"the alternate reference answer scored {other}, while the first scored {good}. "
        f"Two correct answers should both clear the threshold; a rubric that passes one wording "
        f"and fails another is fitted to a string. Judge reason: {other_reason}"
    )
    assert bad < THRESHOLD, f"the padded answer scored {bad}. Judge reason: {bad_reason}"
    assert spread >= MARGIN, (
        f"the judge separated the answers by only {round(spread, 3)} "
        f"(good={good}, alternate={other}, bad={bad}). A metric that cannot tell them apart is "
        f"measuring nothing."
    )


@live_eval
@needs_judge
def test_the_model_under_test_is_scored_and_the_score_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: Any
) -> None:
    """Measure the configured model's own answer. The number is the output, not the assertion.

    Deliberately does **not** assert the score clears the threshold. That would make this file
    measure the model rather than the metric, and a vendor swapping a checkpoint would turn the
    tier red for something that is not a defect. The verdict rides in the recorded ``passed``
    field, which is what lands in the results file an operator reads - so a model scoring badly is
    visible without being a build failure.

    What is asserted is that a usable number came back at all; ``_measure`` raises if it did not.
    The discrimination test above is what makes that number trustworthy: without it, this score
    could be a constant and nothing here would say so.
    """
    session = make_harness(
        tmp_path / "home",
        monkeypatch,
        provider=None,
        keep=("HARNESSED_MODEL", "HARNESSED_BASE_URL"),
    )
    result = ask(session, ANSWER_TASK)
    score, reason = _measure(result.reply)

    record(
        CaseResult(
            id="eval_judge_plain_explanation",
            tier="judge",
            passed=score >= THRESHOLD,
            expected=f"score >= {THRESHOLD}",
            observed=f"{score}",
            detail=reason,
        )
    )
