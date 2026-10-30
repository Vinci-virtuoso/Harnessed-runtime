"""The epsilon rule, the promotion rule and the regression rule, in exactly one module.

Architecture open questions 1 and 2. A second implementation of "is this better" is how a gate
silently loosens, so every comparison of two runs in this repository passes through here (All AI
agents MUST rule 1). The functions are pure: result sets in, a :class:`Decision` out. No clock, no
id, no environment, no file, no socket, and nothing to reach a model with.

**The aggregate.** ``suite_score = mean(case.score)``, a float in ``[0.0, 1.0]`` rounded to 6 dp.
Weighted aggregates are rejected for v1: a weight is a knob that can be tuned until the score
rises, which is precisely the failure mode this milestone exists to prevent.

**The promotion rule**, both conditions required::

    score_ok = (after.score - before.score) > EPSILON or (
        abs(after.score - before.score) <= EPSILON and after.pass_count > before.pass_count
    )
    promote = score_ok and after.regressions == 0

The pass-count tiebreak was ruled in at the gate on 2026-08-22. The loosening is deliberately
narrow: a lower score is always a rejection whatever the pass count does, and the regression
condition is not tie-breakable at all. What the tiebreak buys is the case a grader cannot see - a
procedure that converts a failing case into a passing one has genuinely improved the agent even
when the mean is unmoved, and refusing that is a false negative.

**The single entry point is :func:`promotes`.** :func:`strictly_greater` is exported because a
caller sometimes needs to rank two numbers, but assembling the score half and the regression half
at a call site is an anti-pattern: it silently drops the tiebreak. ``tests/test_scoring.py``
scans ``src/`` for both mistakes.

**A regression** is a case that passed in the baseline and fails in the candidate. Exactly that.
A score drop that still passes is a *soft drop*, which does not block promotion but is always
reported, because silence there would let a candidate quietly erode the suite. A case the
baseline never had is a *new case* and can never be a regression. A case the candidate never ran
is a *missing case*, and the decision fails closed on it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from harnessed.errors import EvalError
from harnessed.evals.graders import SCORE_PRECISION

__all__ = [
    "EPSILON",
    "MINIMUM_LIVE_SAMPLES",
    "RULES",
    "UNDERSAMPLED",
    "CaseOutcome",
    "Comparison",
    "Decision",
    "RunResult",
    "ScoreDelta",
    "compare",
    "mean_score",
    "pass_count",
    "promotes",
    "regressions",
    "strictly_greater",
]

#: The width of the equality band for two scores, declared here and nowhere else in the tree.
#: Scores are rounded to 6 dp before they are compared, so this only has to swallow the float
#: representation error that survives the rounding.
EPSILON: Final = 1e-9

#: The deciding rules a :class:`Decision` may name. Two promote (``score`` is a genuine score
#: win, ``tiebreak`` is the equality-band pass-count gain) and three reject, so a rejection says
#: which condition failed rather than leaving the caller to re-derive it.
#: The rule a promotion is refused under when either side was drawn too few times (ticket 050).
UNDERSAMPLED: Final = "undersampled"

RULES: Final[frozenset[str]] = frozenset(
    {"score", "tiebreak", "no_gain", "regression", "missing_case", UNDERSAMPLED}
)

#: How many draws a run from a sampling source needs before the promoter will compare it.
#:
#: **Why this exists.** The promoter was designed against FakeProvider, which replays a committed
#: corpus and is deterministic by construction. It has no notion of variance because its substrate
#: had none. Then FR-28 proved the same loop drives live vendors - and against Groq the same smoke
#: suite, unchanged, scored 0.2, 0.2, 0.8, 0.8 within four minutes. A promotion comparing one of
#: those draws to another has correct arithmetic and a coin for input.
#:
#: **Why three.** Enough to tell 0.2 from 0.8 and not enough to tell 0.75 from 0.8. It is a
#: judgment call, recorded as one; raise it with a measurement to point at, and never lower it to
#: make a promotion go through. **Why not one.** One would be no minimum, and the caller who
#: knows a source is deterministic passes 1 explicitly - that is the exemption, and it is keyed
#: on the caller's knowledge, not on this constant.
#:
#: The promoter cannot ask which provider scored a run: it has no way to reach one, and that is
#: its guarantee (architecture decision 4). So the *caller* supplies the minimum. This constant
#: is the number a live caller reaches for.
MINIMUM_LIVE_SAMPLES: Final = 3


# ---- what a comparison is made of --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """One case's result in one run: its id, its aggregate score, and whether it passed.

    The pass is not re-derived from the score here. It was decided in ``graders.py`` against the
    case's declared threshold, and carrying it forward is what keeps the regression rule a
    statement about pass/fail rather than a second thresholding.
    """

    case_id: str
    score: float
    passed: bool


@dataclass(frozen=True, slots=True)
class ScoreDelta:
    """One case's score moving between two runs. A regression and a soft drop share this shape."""

    case_id: str
    score_before: float
    score_after: float


@dataclass(frozen=True, slots=True)
class Comparison:
    """Every way a candidate run can differ from its baseline, case by case."""

    regressions: tuple[ScoreDelta, ...] = ()
    soft_drops: tuple[ScoreDelta, ...] = ()
    new_cases: tuple[str, ...] = ()
    missing_cases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunResult:
    """Every case of one run, in a stable order, and how many times each was drawn."""

    """One scored run, reduced to the cases a comparison reads."""

    cases: tuple[CaseOutcome, ...] = ()
    #: How many times each case was scored to produce these numbers (ticket 050). 1 is one draw,
    #: which against a replaying source is exactly the truth and against a live one is a coin.
    samples: int = 1

    @classmethod
    def from_cases(cls, cases: Iterable[CaseOutcome], *, samples: int = 1) -> RunResult:
        """Build a result, rejecting a duplicate case id at construction.

        Two rows for one case id would make ``before`` and ``after`` ambiguous exactly where the
        decision is made, so it is refused here rather than resolved silently.
        """
        ordered = tuple(cases)
        seen: set[str] = set()
        for case in ordered:
            if case.case_id in seen:
                raise EvalError(f"duplicate case id in run result: {case.case_id!r}")
            seen.add(case.case_id)
        return cls(cases=ordered, samples=samples)

    @property
    def score(self) -> float:
        """The run's aggregate: the mean of its case scores, rounded to 6 dp."""
        return mean_score(case.score for case in self.cases)

    @property
    def pass_count(self) -> int:
        """How many of the run's cases passed."""
        return pass_count(self.cases)


# ---- the aggregate -----------------------------------------------------------------------------


def mean_score(scores: Iterable[float]) -> float:
    """The plain mean of ``scores``, a float in ``[0.0, 1.0]`` rounded to 6 decimal places.

    No cases means no evidence, which scores ``0.0``: an empty run can then never beat a
    baseline, which is the fail-closed reading. Any case score outside the unit interval, or one
    that is not a finite number, raises :class:`EvalError` here rather than propagating a
    nonsense aggregate into a promotion decision.
    """
    values = [_admit(score) for score in scores]
    if not values:
        return 0.0
    return round(math.fsum(values) / len(values), SCORE_PRECISION)


def pass_count(cases: Sequence[CaseOutcome]) -> int:
    """How many of ``cases`` passed, which is the tiebreak's only input."""
    return sum(1 for case in cases if case.passed)


def _admit(score: float) -> float:
    value = float(score)
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise EvalError(f"case score outside [0.0, 1.0]: {score!r}")
    return value


# ---- the comparison ----------------------------------------------------------------------------


def strictly_greater(a: float, b: float) -> bool:
    """True only when ``a`` beats ``b`` by more than :data:`EPSILON`.

    The one place in the repository where two scores are ranked. Call this rather than writing
    ``>`` inline, so the epsilon rule lives in exactly one place - and call :func:`promotes` when
    the question is actually whether to promote.
    """
    return a - b > EPSILON


def compare(before: RunResult, after: RunResult) -> Comparison:
    """Sort every case into regression, soft drop, new case or missing case.

    Order follows the candidate run for the cases it has, then the baseline for the ones it does
    not, so the payload reads in the order the runs declare rather than in hash order.
    """
    baseline = {case.case_id: case for case in before.cases}
    candidate = {case.case_id: case for case in after.cases}

    broke: list[ScoreDelta] = []
    dropped: list[ScoreDelta] = []
    added: list[str] = []
    for case in after.cases:
        was = baseline.get(case.case_id)
        if was is None:
            added.append(case.case_id)
        elif was.passed and not case.passed:
            broke.append(_delta(was, case))
        elif strictly_greater(was.score, case.score):
            dropped.append(_delta(was, case))

    missing = [case.case_id for case in before.cases if case.case_id not in candidate]
    return Comparison(
        regressions=tuple(broke),
        soft_drops=tuple(dropped),
        new_cases=tuple(added),
        missing_cases=tuple(missing),
    )


def regressions(before: RunResult, after: RunResult) -> tuple[ScoreDelta, ...]:
    """Exactly the cases that passed in ``before`` and fail in ``after``.

    A score drop that still passes is not here; it is a soft drop on :func:`compare`, reported
    but not blocking. A case the baseline never had is not here either, because there is nothing
    for it to have regressed from.
    """
    return compare(before, after).regressions


# ---- the decision ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether to promote, which rule decided it, and everything the rule looked at.

    ``rule`` is drawn from :data:`RULES`. A tiebreak promotion says ``tiebreak`` and never
    ``score``, so it can never be read back as a score win; a rejection names the condition that
    failed. Truthiness is :attr:`promote`, so ``if promotes(before, after):`` reads correctly
    without the caller reaching past the payload.
    """

    promote: bool
    rule: str
    score_before: float
    score_after: float
    pass_count_before: int
    pass_count_after: int
    #: How many draws each side was, so a reader can tell one draw from a mean of five without
    #: knowing anything else about the run (ticket 050).
    samples_before: int = 1
    samples_after: int = 1
    regressions: tuple[ScoreDelta, ...] = ()
    soft_drops: tuple[ScoreDelta, ...] = ()
    new_cases: tuple[str, ...] = ()
    missing_cases: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.promote


def promotes(before: RunResult, after: RunResult, *, minimum_samples: int = 1) -> Decision:
    """The single entry point for "is the candidate better?".

    Both conditions are required and neither is assembled anywhere else: the score condition (a
    genuine win, or an equality-band pass-count gain) and the regression condition, which is not
    tie-breakable. A missing case fails the decision closed, the same way a missing holdout
    result does under FR-19.
    """
    comparison = compare(before, after)
    score_before, score_after = before.score, after.score
    passes_before, passes_after = before.pass_count, after.pass_count

    won_on_score = strictly_greater(score_after, score_before)
    tied = not won_on_score and not strictly_greater(score_before, score_after)
    won_on_tiebreak = tied and passes_after > passes_before

    # Refused before any score is read. An undersampled comparison cannot be trusted to have
    # found a regression either, so sample count is checked first: the promoter must not act on
    # a coin, and "we saw a regression" from one draw is a coin that landed a particular way.
    if before.samples < minimum_samples or after.samples < minimum_samples:
        rule, promote = UNDERSAMPLED, False
    elif comparison.regressions:
        rule, promote = "regression", False
    elif comparison.missing_cases:
        rule, promote = "missing_case", False
    elif won_on_score:
        rule, promote = "score", True
    elif won_on_tiebreak:
        rule, promote = "tiebreak", True
    else:
        rule, promote = "no_gain", False

    return Decision(
        promote=promote,
        rule=rule,
        score_before=score_before,
        score_after=score_after,
        pass_count_before=passes_before,
        pass_count_after=passes_after,
        regressions=comparison.regressions,
        soft_drops=comparison.soft_drops,
        new_cases=comparison.new_cases,
        missing_cases=comparison.missing_cases,
        samples_before=before.samples,
        samples_after=after.samples,
    )


def _delta(was: CaseOutcome, now: CaseOutcome) -> ScoreDelta:
    return ScoreDelta(case_id=was.case_id, score_before=was.score, score_after=now.score)
