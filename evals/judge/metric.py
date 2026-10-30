"""One way to ask a judge a question, and one way to prove it can answer.

Every ``deepeval`` import in this module is inside a function body. The tier is behind the optional
``[eval]`` extra, an AST scan in the deterministic tier enforces it, and a module-scope import here
would stop the whole suite collecting on a base install.

**Why this module exists.** Ticket 043 established that a judge case is only trustworthy if the
same metric configuration separates a known-good input from a known-bad one by a declared margin -
a judge stuck at any constant passes a one-sided ``score < threshold`` assertion, which is how this
tier once reported a pass while scoring a correct answer and a padded one identically at 0.0.

That rule is enforced here by construction rather than by remembering it. :func:`discriminate` is
the only way to build a judged case, and it cannot be called without both reference outputs. A new
case therefore cannot be added without proving its metric works, and the proof is recorded in the
same results file as the measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evals.judge.judge_model import MARGIN, THRESHOLD
from evals.results import CaseResult

__all__ = ["Judged", "discriminate", "score"]


def score(*, name: str, rubric: str, task: str, output: str) -> tuple[float, str]:
    """Score one output against one rubric. Returns the number and the judge's own reason.

    ``name`` is the metric's label and reaches the judge's prompt, so it is part of the question
    being asked - two cases with the same rubric and different names are two different metrics.
    """
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams

    from evals.judge.judge_model import judge_model

    metric: Any = GEval(
        name=name,
        criteria=rubric,
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        model=judge_model(),
        threshold=THRESHOLD,
    )
    metric.measure(LLMTestCase(input=task, actual_output=output))
    if metric.score is None:
        raise AssertionError(f"the judge returned no score for {name!r} at all")
    if not 0.0 <= float(metric.score) <= 1.0:
        raise AssertionError(f"{name!r} scored {metric.score}, which is outside 0-1")
    return float(metric.score), str(metric.reason or "")


@dataclass(frozen=True, slots=True)
class Judged:
    """One case's measurement, together with the proof that the metric behind it discriminates."""

    name: str
    good: float
    bad: float
    good_reason: str
    bad_reason: str
    observed: float
    observed_reason: str

    @property
    def spread(self) -> float:
        """How far apart the metric put the two references. A constant judge scores zero here."""
        return round(self.good - self.bad, 3)

    @property
    def metric_is_sound(self) -> bool:
        """Whether this metric may be believed at all: both references land, and far apart."""
        return self.good >= THRESHOLD and self.bad < THRESHOLD and self.spread >= MARGIN

    @property
    def passed(self) -> bool:
        """Whether the thing under test cleared the threshold. Meaningless if the metric is not."""
        return self.observed >= THRESHOLD

    def proof(self) -> CaseResult:
        """The metric's own case: what it did to the two references, recorded like any other."""
        return CaseResult(
            id=f"eval_judge_{self.name}_discriminates",
            tier="judge",
            passed=self.metric_is_sound,
            expected=f"good >= {THRESHOLD}, bad < {THRESHOLD}, spread >= {MARGIN}",
            observed=f"good={self.good} bad={self.bad} spread={self.spread}",
            detail=f"good: {self.good_reason} || bad: {self.bad_reason}",
        )

    def measurement(self) -> CaseResult:
        """The case an operator came for: what the real run scored, and why."""
        return CaseResult(
            id=f"eval_judge_{self.name}",
            tier="judge",
            passed=self.passed,
            expected=f"score >= {THRESHOLD}",
            observed=f"{self.observed}",
            detail=self.observed_reason,
        )

    def assert_metric_is_sound(self) -> None:
        """Raise unless this metric earned the right to be believed.

        Three separate failures, because three different broken judges reach this line: one that
        passes everything, one that fails everything, and one that returns any constant at all.
        """
        assert self.good >= THRESHOLD, (
            f"{self.name}: the reference good input scored {self.good}, below the threshold. "
            f"The rubric is asking for something a correct input does not contain. "
            f"Judge reason: {self.good_reason}"
        )
        assert self.bad < THRESHOLD, (
            f"{self.name}: the reference bad input scored {self.bad}, at or above the threshold. "
            f"A metric that passes a deliberately bad input measures nothing. "
            f"Judge reason: {self.bad_reason}"
        )
        assert self.spread >= MARGIN, (
            f"{self.name}: the metric separated its two references by only {self.spread} "
            f"(good={self.good}, bad={self.bad}), under the {MARGIN} margin. A judge returning a "
            f"constant would look exactly like this."
        )


def discriminate(
    *, name: str, rubric: str, task: str, good: str, bad: str, observed: str
) -> Judged:
    """Measure a real output, and in the same breath prove the metric can tell good from bad.

    Three calls to one metric configuration: the known-good reference, the known-bad reference,
    and the thing actually under test. **The references are required arguments** - there is no way
    to get a judged case out of this module without also getting the evidence that the case means
    something.

    Both references are scripted constants rather than solicited from a model. Asking a model to
    be good, or to be bad, is a second thing that can fail, and the claim being made here is about
    the metric.
    """
    good_score, good_reason = score(name=name, rubric=rubric, task=task, output=good)
    bad_score, bad_reason = score(name=name, rubric=rubric, task=task, output=bad)
    observed_score, observed_reason = score(name=name, rubric=rubric, task=task, output=observed)
    return Judged(
        name=name,
        good=good_score,
        bad=bad_score,
        good_reason=good_reason,
        bad_reason=bad_reason,
        observed=observed_score,
        observed_reason=observed_reason,
    )
