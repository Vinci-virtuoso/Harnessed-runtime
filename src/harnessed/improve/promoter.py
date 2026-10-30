"""The promotion decision: three result sets in, one :class:`Decision` out (FR-19, FR-20).

The candidate is judged on the full suite *and* on a holdout the reflector never read. Both are
required. A decision made on the full suite alone would be a decision made on the only evidence
the reflector had a chance to optimise for, which is the failure this project exists to argue
against, so a missing holdout result does not proceed on partial evidence: it rejects.

**Zero model calls, forever, and structurally so.** This module imports ``evals.scoring`` and
nothing else from the package (``architecture.md``, component boundaries). It cannot reach a
provider because it has nothing to reach one with, which is a stronger guarantee than a test that
counts requests.

**The rule itself lives in :mod:`harnessed.evals.scoring` and is not restated here.** Whether the
candidate beat the baseline is :func:`~harnessed.evals.scoring.promotes`'s question, epsilon,
tiebreak, regressions and all. Reassembling the score half beside the regression half at this
call site is the exact anti-pattern the architecture names, so :func:`decide` delegates whole.

**What a holdout costs.** Every unseen case passes at baseline by construction, so a case that
fails under the candidate regressed, and the rejection names it. A candidate that gained where the
reflector could see and broke where it could not is exactly the failure an unseen suite exists to
catch, and the regression condition is not tie-breakable there either.

**The words are here too, and only the words.** :func:`describe` renders a decision for an
operator - the two scores, the deciding rule, the named cases, the soft drops, and under the fake
provider the caveat that says what this proves. It reads a decision; it never makes one.

**The store is injected.** This module may not import ``memory/``, so :func:`promote` takes a
``lookup`` and a ``commit`` callable and a composition root passes the two that reach
``memory/procedural.py``. Injection is the standing answer to an import-table wall in this
repository - the sandbox root in ``tools/``, the call serializer in ``loop/journal.py``, the
assembler, the envelope builder in ``improve/candidate.py``, and now this.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Final

from harnessed.errors import ImproveError
from harnessed.evals.scoring import Decision, RunResult, ScoreDelta, promotes

__all__ = [
    "CAVEAT",
    "HOLDOUT_REGRESSION",
    "MISSING_HOLDOUT",
    "PROMOTION_DECIDED",
    "Commit",
    "Lookup",
    "Outcome",
    "Publish",
    "decide",
    "describe",
    "promote",
]

#: Published once per decision, promoted or refused, so a listener counts decisions rather than
#: branching on which of two names arrived.
PROMOTION_DECIDED: Final = "improve.promotion.decided"

#: The rule a decision names when the holdout result is absent. Deliberately the *same* word a
#: missing case takes: ``architecture.md`` says a missing case "fails the decision closed, same as
#: a missing holdout result under FR-19", so the two are one rule and not two. The vocabulary is
#: frozen as ``scoring.RULES`` and a decision may never name anything outside it.
MISSING_HOLDOUT: Final = "missing_case"

#: The rule a decision names when the candidate broke a case in the unseen suite. The same word a
#: full-suite regression takes, because it is the same fact: something that worked does not any
#: more. Which suite it happened in is in the named case, not in a second rule word.
HOLDOUT_REGRESSION: Final = "regression"

#: The line every printed decision carries under the fake provider, byte for byte
#: (``architecture.md``, THE CRUX). It scopes the claim: the arithmetic and the mechanism are
#: real, the model behind the numbers is a fixture, and nobody can screenshot this and oversell it.
CAVEAT: Final = "provider=fake - this proves the promotion mechanism, not model quality"

#: The provider name the caveat belongs to. Printed under that provider and no other, because
#: claiming it under a live model would be false in the other direction.
FAKE: Final = "fake"


def decide(
    before: RunResult,
    after: RunResult,
    holdout: RunResult | None,
    *,
    minimum_samples: int = 1,
) -> Decision:
    """Promote the candidate only on the full suite *and* the unseen one.

    Two conditions, and the second is the reason the unseen suite exists. The full-suite
    question is :func:`~harnessed.evals.scoring.promotes`'s and is not restated here. The holdout
    question is asked of the candidate's run alone, because every unseen case passes at baseline
    by construction: a case failing there regressed, and the rejection names it. Both columns of
    that :class:`~harnessed.evals.scoring.ScoreDelta` carry the score actually observed - the
    holdout has one run, so there is no earlier number to report and none is invented.

    ``holdout`` is ``None`` when the unseen suite was never run, and empty when it ran and scored
    nothing. Both are the same fact - no evidence from outside the reflector's view - and both
    reject. Note that an empty result set would otherwise score ``0.0`` and read as a holdout
    failure rather than as a holdout that never happened; refusing it here keeps "we did not look"
    from being recorded as "we looked and it was bad".
    """
    if holdout is None or not holdout.cases:
        return _rejected(before, after)
    decision = promotes(before, after, minimum_samples=minimum_samples)
    unseen = _broken(holdout)
    if not decision.promote or not unseen:
        return decision
    return replace(decision, promote=False, rule=HOLDOUT_REGRESSION, regressions=unseen)


def _broken(holdout: RunResult) -> tuple[ScoreDelta, ...]:
    """Every unseen case the candidate failed, named. A pass/fail read, never a comparison."""
    return tuple(
        ScoreDelta(case_id=case.case_id, score_before=case.score, score_after=case.score)
        for case in holdout.cases
        if not case.passed
    )


def _rejected(before: RunResult, after: RunResult) -> Decision:
    """The fail-closed decision, still carrying every number it did have.

    Fail-closed is not fail-silent. A caller that has to re-derive what the run scored in order to
    report why it was refused is one keystroke away from reimplementing the rule.
    """
    return Decision(
        promote=False,
        rule=MISSING_HOLDOUT,
        score_before=before.score,
        score_after=after.score,
        pass_count_before=before.pass_count,
        pass_count_after=after.pass_count,
    )


@dataclass(frozen=True, slots=True)
class Outcome:
    """One decision as it is recorded and reported: the numbers, the rule, and the detail.

    The counts and the detail are both here because they come from different places. A decision
    just made carries its :class:`~harnessed.evals.scoring.ScoreDelta` tuples; a decision read
    back from the ledger carries the counts alone, since the row stores counts. So
    ``regression_count`` is always meaningful and ``regressions`` is the detail when there was a
    decision to detail - and a caller reading the counts gets the same answer either way.

    ``replayed`` is how the second promotion of one candidate says so. It is not a second
    decision; it is the first one, reported again.
    """

    cand_id: str
    promoted: bool
    rule: str
    score_before: float
    score_after: float
    pass_count_before: int
    pass_count_after: int
    regression_count: int
    soft_drop_count: int
    regressions: tuple[ScoreDelta, ...] = ()
    soft_drops: tuple[ScoreDelta, ...] = ()
    missing_cases: tuple[str, ...] = ()
    #: How many draws each side was (ticket 050), so a recorded outcome can say whether the
    #: numbers it carries were a measurement or a coin.
    samples_before: int = 1
    samples_after: int = 1
    decided_at: str = ""
    replayed: bool = False

    @classmethod
    def of(cls, cand_id: str, decision: Decision) -> Outcome:
        """The outcome a fresh :class:`~harnessed.evals.scoring.Decision` implies."""
        return cls(
            cand_id=cand_id,
            promoted=decision.promote,
            rule=decision.rule,
            score_before=decision.score_before,
            score_after=decision.score_after,
            pass_count_before=decision.pass_count_before,
            pass_count_after=decision.pass_count_after,
            regression_count=len(decision.regressions),
            soft_drop_count=len(decision.soft_drops),
            regressions=decision.regressions,
            soft_drops=decision.soft_drops,
            missing_cases=decision.missing_cases,
            samples_before=decision.samples_before,
            samples_after=decision.samples_after,
        )

    def __bool__(self) -> bool:
        return self.promoted


#: The recorded decision for a candidate id, or ``None`` when there is not one yet.
Lookup = Callable[[str], "Outcome | None"]

#: Record a decision and, on an accept, activate the procedure. Returns the stored outcome, which
#: is where ``decided_at`` comes from: this module has no clock either.
Commit = Callable[["Outcome", str], "Outcome"]

#: How an event reaches the bus, injected for the same reason the store is.
Publish = Callable[[str, Mapping[str, object]], object]


def promote(
    cand_id: str,
    text: str,
    before: RunResult,
    after: RunResult,
    holdout: RunResult | None,
    *,
    lookup: Lookup,
    commit: Commit,
    publish: Publish | None = None,
    minimum_samples: int = 1,
) -> Outcome:
    """Decide once per candidate id, record the decision, and say so.

    Idempotent on the candidate id: a second call finds the recorded outcome, reports it with
    ``replayed=True`` and changes nothing - no second row, no second event, no touch of the
    active set. The candidate id is content-addressed (``improve/candidate.py``), so a resumed
    improvement run re-issuing this call is exactly the case this covers.
    """
    if not cand_id.strip():
        raise ImproveError(
            "a promotion decision is recorded against a candidate id, and none was given. The "
            "id is the candidate's content hash (FR-18), not an optional label."
        )
    if not text.strip():
        raise ImproveError(
            f"candidate {cand_id!r} has no procedure text, so a promotion would activate an "
            f"empty procedure. A candidate is text appended to the system prompt (FR-17)."
        )

    recorded = lookup(cand_id)
    if recorded is not None:
        return replace(recorded, replayed=True)

    outcome = commit(
        Outcome.of(cand_id, decide(before, after, holdout, minimum_samples=minimum_samples)), text
    )
    if publish is not None:
        publish(PROMOTION_DECIDED, _payload(outcome))
    return outcome


def describe(outcome: Outcome, *, provider: str) -> str:
    """The decision as an operator reads it: the numbers, the rule, and what the rule looked at.

    Rendering is separated from deciding so the CLI stays a thin layer over this, and so the
    words a rejection uses are testable without a terminal. Soft drops are printed whether or not
    there are any, because an empty list and a list nobody printed look identical in a transcript.
    """
    lines = [
        f"candidate {outcome.cand_id}: {'promoted' if outcome.promoted else 'rejected'}",
        f"rule: {outcome.rule} - {_because(outcome)}",
        f"score: {outcome.score_before} -> {outcome.score_after}",
        f"passes: {outcome.pass_count_before} -> {outcome.pass_count_after}",
        f"regressions: {outcome.regression_count}",
    ]
    if outcome.regressions:
        lines.append(f"regressed: {', '.join(delta.case_id for delta in outcome.regressions)}")
    if outcome.missing_cases:
        lines.append(f"missing: {', '.join(outcome.missing_cases)}")
    lines.append(f"soft_drops: [{', '.join(_drop(drop) for drop in outcome.soft_drops)}]")
    if provider == FAKE:
        lines.append(CAVEAT)
    return "\n".join(lines)


def _payload(outcome: Outcome) -> dict[str, object]:
    """What the event carries: the decision, in the shape the decision record uses."""
    return {
        "cand_id": outcome.cand_id,
        "promoted": outcome.promoted,
        "rule": outcome.rule,
        "score_before": outcome.score_before,
        "score_after": outcome.score_after,
        "pass_count_before": outcome.pass_count_before,
        "pass_count_after": outcome.pass_count_after,
        "regressions": [delta.case_id for delta in outcome.regressions],
        "soft_drops": [_delta_payload(drop) for drop in outcome.soft_drops],
        "missing_cases": list(outcome.missing_cases),
        "decided_at": outcome.decided_at,
    }


def _delta_payload(delta: ScoreDelta) -> dict[str, object]:
    return {
        "case_id": delta.case_id,
        "score_before": delta.score_before,
        "score_after": delta.score_after,
    }


def _drop(delta: ScoreDelta) -> str:
    return (
        f"{{case_id: {delta.case_id}, score_before: {delta.score_before}, "
        f"score_after: {delta.score_after}}}"
    )


def _because(outcome: Outcome) -> str:
    """One sentence per rule, so a decision never leaves the reader to re-derive it."""
    return _REASONS[outcome.rule]


#: The deciding rule in words. The keys are ``evals/scoring.py::RULES`` and nothing else, so a
#: rule this table cannot explain is a missing entry rather than a silent blank line.
_REASONS: Final[dict[str, str]] = {
    "score": "the aggregate score rose by more than EPSILON and nothing regressed",
    "tiebreak": (
        "the score did not move, one more case passed, and nothing regressed. Promoted on the "
        "pass-count tiebreak, never on score"
    ),
    "no_gain": "the score condition failed: no gain beyond EPSILON and no pass-count tiebreak",
    "regression": "a case that passed before now fails, and the regression condition is not "
    "tie-breakable",
    "missing_case": "a case in the baseline was not scored in the candidate run, so the "
    "decision fails closed",
    "undersampled": "one side or both was drawn fewer times than the declared minimum, so the "
    "numbers compared are a draw and not a measurement. Refused rather than warned: a promoter "
    "that cautions and promotes anyway has judgment in it. Re-run with --samples at or above "
    "the minimum",
}
