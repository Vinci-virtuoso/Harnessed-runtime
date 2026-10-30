"""FR-20: the promotion decision is arithmetic, and there is no model anywhere near it.

Four claims carry this file, and each is measured against the thing that would quietly break it:

* **Zero model calls, forever.** Measured twice. Structurally, ``improve/promoter.py`` imports
  ``errors`` and ``evals.scoring`` and nothing else, so it has nothing to reach a provider with
  (``tests/test_holdout.py`` owns that scan). Behaviourally, the whole promotion path - decide,
  commit, publish, print - runs with a provider spy installed that fails the test on any call and
  with the provider factory itself replaced by one that fails on construction.
* **The rule is not restated here.** Every accept and every reject in this file is
  :func:`~harnessed.evals.scoring.promotes`'s answer, epsilon and tiebreak and regressions and
  all. What ``promoter.py`` adds is the holdout, the ledger and the words - never a comparison.
* **A promotion never deletes a procedure.** Rows leave ``active_procedures``; nothing ever leaves
  ``procedures``. The previously active version is asserted to be still retrievable *by id* after
  the swap, because rollback and diff are the reason it is kept.
* **Deciding twice decides once.** The second call reports the recorded outcome and changes
  nothing at all - no second promotions row, no second event, no touch of the active set. It is
  the same content-addressed idempotency the candidate store has, moved one step downstream.

The ledger adapter below is the composition root for now: ``improve/promoter.py`` may not import
``memory/``, so the two store callables are injected, and ticket 026 wires the real ones in
``improve/steps.py``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from harnessed.errors import ImproveError
from harnessed.evals.scoring import (
    MINIMUM_LIVE_SAMPLES,
    UNDERSAMPLED,
    CaseOutcome,
    Decision,
    RunResult,
    ScoreDelta,
    promotes,
)
from harnessed.events.bus import EventBus
from harnessed.improve import promoter as promoter_module
from harnessed.improve.promoter import Outcome, decide, describe, promote
from harnessed.memory import db, procedural
from harnessed.providers.types import ChatRequest
from support.spies import ForbiddenProvider, RecordingObserver

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence" / "025"

#: The line the architecture requires on every printed decision under the fake provider, byte for
#: byte. Nobody can screenshot this proof and oversell it.
CAVEAT = "provider=fake - this proves the promotion mechanism, not model quality"

CANDIDATE = "cand_0123456789abcdef"
OTHER_CANDIDATE = "cand_fedcba9876543210"
PROCEDURE = "When the answer must be one object, emit the object and nothing around it."
RETIRED = "Answer briefly."


# ---- fixtures -----------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """The one database, with the two candidate rows a decision is recorded against.

    ``promotions.cand_id`` is a foreign key onto ``candidates`` and foreign keys are on, so a
    decision cannot be recorded for a candidate that was never written. The rows are seeded
    directly rather than through ``improve/candidate.py``: candidate identity is that module's
    subject and ``tests/test_reflection.py`` owns it, while this file is about what happens to a
    candidate that already exists.
    """
    conn = db.connect(tmp_path / "harness.db")
    db.write(
        conn,
        tuple(
            (
                "INSERT INTO candidates (cand_id, text, source_case_ids, provenance, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (cand_id, PROCEDURE, '["b"]', "ticket 025", "2026-08-22T00:00:00Z"),
            )
            for cand_id in (CANDIDATE, OTHER_CANDIDATE)
        ),
    )
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def bus(store: sqlite3.Connection) -> EventBus:
    return EventBus(run_id="run_promoter", conn=store)


@pytest.fixture
def observer(bus: EventBus) -> RecordingObserver:
    return bus.subscribe(RecordingObserver())


@pytest.fixture(autouse=True)
def no_provider(monkeypatch: pytest.MonkeyPatch) -> ForbiddenProvider:
    """Install a provider that fails the test on any call, for every test in this file.

    Autouse rather than per-test: "zero model calls" is a claim about the whole promotion path,
    and a spy that only guards the one test that names it would leave every other path unwatched.
    The factory is replaced too, so constructing a provider is as loud as calling one.
    """
    spy = ForbiddenProvider()
    monkeypatch.setattr("harnessed.providers.registry.create", spy.refuse_to_build)
    return spy


class Ledger:
    """The store side of the promotion, wired onto ``memory/procedural.py``.

    ``improve/promoter.py`` may import ``evals.scoring`` and ``errors`` and nothing else, so the
    lookup and the commit arrive as callables and this is what a composition root passes. It is
    deliberately thin: the row-level work - one transaction, procedure written before the active
    set moves, nothing ever deleted from ``procedures`` - belongs to ``procedural.py``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.commits = 0

    def lookup(self, cand_id: str) -> Outcome | None:
        recorded = procedural.promotion(self.conn, cand_id)
        if recorded is None:
            return None
        return Outcome(
            cand_id=recorded.cand_id,
            decided_at=recorded.decided_at,
            promoted=recorded.promoted,
            rule=recorded.reason,
            score_before=recorded.score_before,
            score_after=recorded.score_after,
            pass_count_before=recorded.passes_before,
            pass_count_after=recorded.passes_after,
            regression_count=recorded.regressions,
            soft_drop_count=recorded.soft_drops,
        )

    def commit(self, outcome: Outcome, text: str) -> Outcome:
        self.commits += 1
        recorded = procedural.record_promotion(
            self.conn,
            cand_id=outcome.cand_id,
            text=text,
            promoted=outcome.promoted,
            reason=outcome.rule,
            score_before=outcome.score_before,
            score_after=outcome.score_after,
            passes_before=outcome.pass_count_before,
            passes_after=outcome.pass_count_after,
            regressions=outcome.regression_count,
            soft_drops=outcome.soft_drop_count,
        )
        return replace(outcome, decided_at=recorded.decided_at)


@pytest.fixture
def ledger(store: sqlite3.Connection) -> Ledger:
    return Ledger(store)


# ---- builders -----------------------------------------------------------------------------------


def outcome(case_id: str, score: float, *, passed: bool) -> CaseOutcome:
    return CaseOutcome(case_id=case_id, score=score, passed=passed)


def run(*cases: CaseOutcome) -> RunResult:
    return RunResult.from_cases(cases)


def clean_holdout() -> RunResult:
    """A holdout run in which every unseen case passed. Present, and carrying no objection."""
    return run(
        outcome("hold_json_only", 1.0, passed=True),
        outcome("hold_tool_choice", 1.0, passed=True),
        outcome("hold_refusal", 1.0, passed=True),
    )


def gain() -> tuple[RunResult, RunResult]:
    """A strict score win with no regression: 0.5 -> 1.0, one case turning from fail to pass."""
    before = run(outcome("a", 1.0, passed=True), outcome("b", 0.0, passed=False))
    after = run(outcome("a", 1.0, passed=True), outcome("b", 1.0, passed=True))
    return before, after


def tiebreak() -> tuple[RunResult, RunResult]:
    """Equal means, one more pass: 0.5 either way, but ``b`` crosses its threshold.

    ``a`` gives back exactly what ``b`` gains, so the aggregate does not move by more than
    ``EPSILON`` and the score condition can only be satisfied by the pass count.
    """
    before = run(outcome("a", 0.8, passed=True), outcome("b", 0.2, passed=False))
    after = run(outcome("a", 0.4, passed=True), outcome("b", 0.6, passed=True))
    return before, after


def decided(before: RunResult, after: RunResult) -> Decision:
    return decide(before, after, clean_holdout())


# ---- AC 1: the promotion path cannot reach a model -----------------------------------------------


def test_the_whole_promotion_path_issues_zero_provider_calls(
    ledger: Ledger, bus: EventBus, no_provider: ForbiddenProvider
) -> None:
    """Decide, commit, publish and print, with a spy that fails the test on any call."""
    before, after = gain()

    result = promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )
    printed = describe(result, provider="fake")

    assert result.promoted is True
    assert CAVEAT in printed
    assert no_provider.calls == 0
    assert no_provider.builds == 0


def test_the_spy_is_wired_to_something_that_would_have_failed() -> None:
    """A spy nobody can trip proves nothing. Trip it directly, once, on purpose."""
    spy = ForbiddenProvider()

    with pytest.raises(AssertionError):
        spy.complete(ChatRequest(model="fake-1", system="", messages=()))

    assert spy.calls == 1


# ---- AC 2: a genuine score win -------------------------------------------------------------------


def test_a_higher_score_with_no_regression_is_promoted() -> None:
    before, after = gain()

    decision = decided(before, after)

    assert decision.promote is True
    assert decision.rule == "score"


def test_the_record_states_both_numbers(ledger: Ledger, bus: EventBus) -> None:
    """ "States both numbers" is about the stored row, not only about the return value."""
    before, after = gain()

    result = promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    recorded = procedural.promotion(ledger.conn, CANDIDATE)
    assert recorded is not None
    assert bool(result) is result.promoted
    assert (recorded.score_before, recorded.score_after) == (0.5, 1.0)
    assert (recorded.passes_before, recorded.passes_after) == (1, 2)
    assert recorded.regressions == 0
    assert (result.score_before, result.score_after) == (0.5, 1.0)


# ---- AC 3 and 4: the equality band ---------------------------------------------------------------


def test_an_equal_score_with_a_higher_pass_count_is_promoted_on_the_tiebreak() -> None:
    before, after = tiebreak()

    decision = decided(before, after)

    assert decision.score_after == decision.score_before
    assert decision.pass_count_after > decision.pass_count_before
    assert decision.promote is True
    assert decision.rule == "tiebreak"


def test_a_tiebreak_promotion_is_never_printed_as_a_score_win(
    ledger: Ledger, bus: EventBus
) -> None:
    """The word ``tiebreak`` is in the output and the words "on score" are not."""
    before, after = tiebreak()

    result = promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )
    printed = describe(result, provider="fake")

    assert result.rule == "tiebreak"
    assert "tiebreak" in printed
    assert "the score did not move" in printed
    assert procedural.promotion(ledger.conn, CANDIDATE) is not None


@pytest.mark.parametrize(
    ("passed_b", "rule"),
    [(False, "no_gain"), (True, "regression")],
    ids=["equal-pass-count", "lower-pass-count"],
)
def test_an_equal_score_without_a_pass_gain_is_rejected(passed_b: bool, rule: str) -> None:
    """Equal or lower is not higher. The tiebreak only fires on a strict pass-count gain.

    The two branches are refused by different conditions and that is not an accident: with the
    same case set on both sides, a *lower* pass count means some case went from passing to
    failing, which is a regression by definition. So the score condition catches the equal case
    and the regression condition catches the lower one, and neither is promoted.
    """
    before = run(outcome("a", 0.8, passed=True), outcome("b", 0.2, passed=passed_b))
    after = run(outcome("a", 0.4, passed=True), outcome("b", 0.6, passed=False))

    decision = decided(before, after)

    assert decision.score_after == decision.score_before
    assert decision.pass_count_after <= decision.pass_count_before
    assert decision.promote is False
    assert decision.rule == rule


# ---- AC 5: a lower score is always a rejection ---------------------------------------------------


def test_a_lower_score_is_rejected_even_with_a_higher_pass_count() -> None:
    """The tiebreak lives inside the equality band and nowhere else."""
    before = run(outcome("a", 1.0, passed=True), outcome("b", 0.9, passed=False))
    after = run(outcome("a", 0.1, passed=True), outcome("b", 0.9, passed=True))

    decision = decided(before, after)

    assert decision.score_after < decision.score_before
    assert decision.pass_count_after > decision.pass_count_before
    assert decision.promote is False
    assert decision.rule == "no_gain"


def test_the_rejection_says_the_score_condition_failed(ledger: Ledger, bus: EventBus) -> None:
    before = run(outcome("a", 1.0, passed=True), outcome("b", 0.9, passed=False))
    after = run(outcome("a", 0.1, passed=True), outcome("b", 0.9, passed=True))

    result = promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )
    printed = describe(result, provider="fake")

    assert result.promoted is False
    assert "the score condition failed" in printed


# ---- AC 6 and 7: the regression condition is not tie-breakable -----------------------------------


def test_a_regression_rejects_a_candidate_that_won_on_score() -> None:
    before = run(outcome("a", 1.0, passed=True), outcome("b", 0.0, passed=False))
    after = run(outcome("a", 0.0, passed=False), outcome("b", 1.0, passed=True))

    decision = decide(before, after, clean_holdout())

    assert decision.promote is False
    assert decision.rule == "regression"


def test_a_regression_rejects_a_candidate_that_won_only_on_the_tiebreak() -> None:
    """Two cases turn fail into pass and one turns pass into fail: net one more pass, one break.

    The mean is unmoved - ``a`` gives back exactly what ``b`` and ``c`` gain - so the only path
    to promotion is the tiebreak, and the tiebreak does not reach the regression condition.
    """
    before = run(
        outcome("a", 0.6, passed=True),
        outcome("b", 0.0, passed=False),
        outcome("c", 0.0, passed=False),
    )
    after = run(
        outcome("a", 0.0, passed=False),
        outcome("b", 0.3, passed=True),
        outcome("c", 0.3, passed=True),
    )

    decision = decide(before, after, clean_holdout())

    assert decision.pass_count_after > decision.pass_count_before
    assert decision.promote is False
    assert decision.rule == "regression"


def test_the_rejection_names_the_regressed_case(ledger: Ledger, bus: EventBus) -> None:
    before = run(outcome("a", 1.0, passed=True), outcome("b", 0.0, passed=False))
    after = run(outcome("a", 0.0, passed=False), outcome("b", 1.0, passed=True))

    result = promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )
    printed = describe(result, provider="fake")

    assert [delta.case_id for delta in result.regressions] == ["a"]
    assert "a" in printed.split("regressed: ")[1]
    assert procedural.active(ledger.conn) == ()


# ---- AC 8: deciding twice decides once -----------------------------------------------------------


def test_the_second_promotion_reports_the_recorded_outcome_and_changes_nothing(
    ledger: Ledger, bus: EventBus, observer: RecordingObserver
) -> None:
    """Idempotent means byte-identical afterwards: no second row, no second event, no swap."""
    before, after = gain()
    arguments = {
        "lookup": ledger.lookup,
        "commit": ledger.commit,
        "publish": bus.publish,
    }

    first = promote(CANDIDATE, PROCEDURE, before, after, clean_holdout(), **arguments)
    after_first = _snapshot(ledger.conn)
    second = promote(CANDIDATE, PROCEDURE, before, after, clean_holdout(), **arguments)

    assert second.replayed is True
    assert first.replayed is False
    assert (second.promoted, second.rule) == (first.promoted, first.rule)
    assert (second.score_before, second.score_after) == (first.score_before, first.score_after)
    assert second.decided_at == first.decided_at
    assert _snapshot(ledger.conn) == after_first
    assert ledger.commits == 1
    assert observer.names.count(promoter_module.PROMOTION_DECIDED) == 1


def test_a_replayed_rejection_is_also_reported_and_not_re_decided(
    ledger: Ledger, bus: EventBus, observer: RecordingObserver
) -> None:
    """A refusal is a recorded decision too, so re-running the command cannot flip it."""
    before = run(outcome("a", 1.0, passed=True))
    after = run(outcome("a", 0.2, passed=True))
    arguments = {"lookup": ledger.lookup, "commit": ledger.commit, "publish": bus.publish}

    promote(CANDIDATE, PROCEDURE, before, after, clean_holdout(), **arguments)
    replayed = promote(CANDIDATE, PROCEDURE, *gain(), clean_holdout(), **arguments)

    assert replayed.replayed is True
    assert replayed.promoted is False
    assert replayed.rule == "no_gain"
    assert observer.names.count(promoter_module.PROMOTION_DECIDED) == 1


# ---- AC 9: a promotion never deletes a procedure -------------------------------------------------


def test_the_previously_active_procedure_survives_the_swap(ledger: Ledger, bus: EventBus) -> None:
    """Rows leave ``active_procedures``. Nothing ever leaves ``procedures``."""
    retired = procedural.put(ledger.conn, RETIRED, source="seed")
    procedural.activate(ledger.conn, retired.proc_id, cand_id=None)
    before, after = gain()

    result = promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    active = procedural.active(ledger.conn)
    assert result.promoted is True
    assert [row.text for row in active] == [PROCEDURE]
    assert procedural.get(ledger.conn, retired.proc_id) == retired
    assert retired.proc_id in {row.proc_id for row in procedural.all(ledger.conn)}


def test_a_rejected_candidate_leaves_the_active_set_untouched(
    ledger: Ledger, bus: EventBus
) -> None:
    retired = procedural.put(ledger.conn, RETIRED, source="seed")
    procedural.activate(ledger.conn, retired.proc_id, cand_id=None)
    before = run(outcome("a", 1.0, passed=True))
    after = run(outcome("a", 0.2, passed=True))

    promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    assert [row.proc_id for row in procedural.active(ledger.conn)] == [retired.proc_id]
    assert procedural.get(ledger.conn, procedural.proc_id_for(PROCEDURE)) is None


def test_activating_the_same_procedure_twice_leaves_one_row(ledger: Ledger) -> None:
    """The swap is idempotent on its own, so a retried commit cannot double the active set."""
    first = procedural.put(ledger.conn, RETIRED, source="seed")

    procedural.activate(ledger.conn, first.proc_id, cand_id=None)
    procedural.activate(ledger.conn, first.proc_id, cand_id=None)

    assert [row.proc_id for row in procedural.active(ledger.conn)] == [first.proc_id]


def test_activating_an_unwritten_procedure_is_refused(ledger: Ledger) -> None:
    """The active set points at procedural memory; it may never name a row that is not there."""
    with pytest.raises(Exception) as raised:
        procedural.activate(ledger.conn, "proc_deadbeefdeadbeef", cand_id=None)

    assert "proc_deadbeefdeadbeef" in str(raised.value)


# ---- AC 10: soft drops are reported --------------------------------------------------------------


def test_soft_drops_are_recorded_as_deltas_and_printed(
    ledger: Ledger, bus: EventBus, observer: RecordingObserver
) -> None:
    """A drop that still passes does not block the promotion, and is never silent."""
    before = run(outcome("a", 0.9, passed=True), outcome("b", 0.0, passed=False))
    after = run(outcome("a", 0.7, passed=True), outcome("b", 1.0, passed=True))

    result = promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )
    printed = describe(result, provider="fake")

    assert result.promoted is True
    assert result.soft_drops == (ScoreDelta(case_id="a", score_before=0.9, score_after=0.7),)
    assert "soft_drops: [{case_id: a, score_before: 0.9, score_after: 0.7}]" in printed

    published = observer.named(promoter_module.PROMOTION_DECIDED)[0]
    assert published.payload["soft_drops"] == [
        {"case_id": "a", "score_before": 0.9, "score_after": 0.7}
    ]
    recorded = procedural.promotion(ledger.conn, CANDIDATE)
    assert recorded is not None
    assert recorded.soft_drops == 1


def test_a_decision_with_no_soft_drops_prints_an_empty_list(ledger: Ledger, bus: EventBus) -> None:
    """Printed either way, so silence is never ambiguous between none and not looked at."""
    result = promote(
        CANDIDATE,
        PROCEDURE,
        *gain(),
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    assert "soft_drops: []" in describe(result, provider="fake")


# ---- AC 11: a missing case fails closed ----------------------------------------------------------


def test_a_case_missing_from_the_candidate_run_fails_closed() -> None:
    """Not scored is not the same as scored well. The mean of what remains cannot decide this."""
    before = run(outcome("a", 0.0, passed=False), outcome("b", 1.0, passed=True))
    after = run(outcome("a", 1.0, passed=True))

    decision = decided(before, after)

    assert decision.missing_cases == ("b",)
    assert decision.promote is False
    assert decision.rule == "missing_case"


def test_the_missing_case_is_named_in_the_output(ledger: Ledger, bus: EventBus) -> None:
    before = run(outcome("a", 0.0, passed=False), outcome("b", 1.0, passed=True))
    after = run(outcome("a", 1.0, passed=True))

    result = promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    assert "missing: b" in describe(result, provider="fake")


@pytest.mark.parametrize("absent", [None, RunResult.from_cases([])], ids=["none", "empty"])
def test_a_missing_holdout_still_fails_closed(absent: RunResult | None) -> None:
    """Ticket 024's rule, re-asserted here because this file owns the decision from now on."""
    before, after = gain()

    decision = decide(before, after, absent)

    assert decision.promote is False
    assert decision.rule == promoter_module.MISSING_HOLDOUT


def test_a_failing_holdout_case_rejects_a_candidate_that_won_the_suite() -> None:
    """The point of an unseen suite: a candidate that gained where it could see, and broke where
    it could not, is refused and the unseen case is named."""
    before, after = gain()
    held = run(
        outcome("hold_json_only", 1.0, passed=True),
        outcome("hold_tool_choice", 0.0, passed=False),
        outcome("hold_refusal", 1.0, passed=True),
    )

    decision = decide(before, after, held)

    assert decision.promote is False
    assert decision.rule == "regression"
    assert [delta.case_id for delta in decision.regressions] == ["hold_tool_choice"]


# ---- AC 12 and 13: the caveat, and the event -----------------------------------------------------


def test_the_fake_provider_caveat_is_printed_byte_for_byte(ledger: Ledger, bus: EventBus) -> None:
    result = promote(
        CANDIDATE,
        PROCEDURE,
        *gain(),
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    assert CAVEAT in describe(result, provider="fake").splitlines()


def test_a_live_provider_prints_no_caveat(ledger: Ledger, bus: EventBus) -> None:
    """The caveat is about what the fake proves. Claiming it under a live model would be false."""
    result = promote(
        CANDIDATE,
        PROCEDURE,
        *gain(),
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    assert CAVEAT not in describe(result, provider="anthropic")


@pytest.mark.parametrize("builder", [gain, tiebreak], ids=["score-win", "tiebreak"])
def test_every_decision_publishes_the_event(
    ledger: Ledger,
    bus: EventBus,
    observer: RecordingObserver,
    builder: Callable[[], tuple[RunResult, RunResult]],
) -> None:
    before, after = builder()

    result = promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    assert observer.names == [promoter_module.PROMOTION_DECIDED]
    payload = observer.named(promoter_module.PROMOTION_DECIDED)[0].payload
    assert payload["cand_id"] == CANDIDATE
    assert payload["promoted"] is result.promoted
    assert payload["rule"] == result.rule


def test_a_rejection_publishes_the_same_event(
    ledger: Ledger, bus: EventBus, observer: RecordingObserver
) -> None:
    """One name for both outcomes, so a listener counts decisions rather than branching."""
    before = run(outcome("a", 1.0, passed=True))
    after = run(outcome("a", 0.2, passed=True))

    promote(
        CANDIDATE,
        PROCEDURE,
        before,
        after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    assert observer.names == [promoter_module.PROMOTION_DECIDED]


def test_promoting_without_a_bus_still_decides(ledger: Ledger) -> None:
    """``publish`` is optional: the decision is the product, the event is the notification."""
    result = promote(
        CANDIDATE, PROCEDURE, *gain(), clean_holdout(), lookup=ledger.lookup, commit=ledger.commit
    )

    assert result.promoted is True


# ---- the shape of the seam -----------------------------------------------------------------------


def test_a_promotion_needs_procedure_text() -> None:
    """A promotion with nothing to activate would leave the active set naming an empty row."""
    with pytest.raises(ImproveError) as raised:
        promote(CANDIDATE, "   ", *gain(), clean_holdout(), lookup=_never, commit=_no_commit)

    assert CANDIDATE in str(raised.value)


def test_a_promotion_needs_a_candidate_id() -> None:
    with pytest.raises(ImproveError):
        promote("", PROCEDURE, *gain(), clean_holdout(), lookup=_never, commit=_no_commit)


def _never(cand_id: str) -> Outcome | None:
    raise AssertionError(f"the store was consulted for {cand_id!r} after a refused argument")


def _no_commit(outcome: Outcome, text: str) -> Outcome:
    raise AssertionError("nothing may be committed after a refused argument")


def _snapshot(conn: sqlite3.Connection) -> tuple[list[tuple[object, ...]], ...]:
    """Every row this ticket can write, so "changes nothing" is checked and not assumed."""
    return tuple(
        [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("promotions", "procedures", "active_procedures", "events")
    )


# ---- evidence ------------------------------------------------------------------------------------


def test_capture_the_decision_transcript(ledger: Ledger, bus: EventBus) -> None:
    """Write ``evidence/025/promotion-decision.txt``: one accepted, one rejected, with the caveat.

    The architecture's evidence rule for a v1 ticket with no visual surface: operator-visible
    terminal output is captured as text at the assertion point. Regenerated on every verify run
    and never committed.
    """
    accepted = promote(
        CANDIDATE,
        PROCEDURE,
        *gain(),
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )
    before = run(outcome("a", 1.0, passed=True), outcome("b", 0.5, passed=True))
    rejected_after = run(outcome("a", 0.0, passed=False), outcome("b", 1.0, passed=True))
    rejected = promote(
        OTHER_CANDIDATE,
        "Never answer.",
        before,
        rejected_after,
        clean_holdout(),
        lookup=ledger.lookup,
        commit=ledger.commit,
        publish=bus.publish,
    )

    transcript = "\n\n".join(
        (describe(accepted, provider="fake"), describe(rejected, provider="fake"))
    )
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "promotion-decision.txt").write_text(transcript + "\n", encoding="utf-8")

    assert transcript.count(CAVEAT) == 2
    assert accepted.promoted is True
    assert rejected.promoted is False


# Ticket 050. A live baseline is one draw, and the promoter must know it.


def _run(score: float, *, samples: int = 1) -> RunResult:
    """One-case run at ``score``, drawn ``samples`` times. The case id is fixed on purpose."""
    return RunResult.from_cases(
        (CaseOutcome(case_id="only", score=score, passed=score >= 1.0),), samples=samples
    )


def test_a_single_draw_from_a_sampled_source_is_refused_not_promoted() -> None:
    """The arithmetic was correct and the input was a coin.

    Against a live model the same suite scored 0.2, 0.2, 0.8, 0.8 on identical input in four
    minutes. A promotion comparing one of those draws to another reads as a measurement. The
    promoter has no model in it (FR-20), so it cannot warn and promote anyway - that would be
    judgment through the back door. It refuses, and names what would lift the refusal.
    """
    decision = promotes(_run(0.8), _run(1.0), minimum_samples=3)
    assert decision.promote is False
    assert decision.rule == UNDERSAMPLED
    assert decision.samples_before == 1 and decision.samples_after == 1


def test_enough_samples_on_both_sides_lets_the_existing_rule_decide() -> None:
    decision = promotes(_run(0.8, samples=3), _run(1.0, samples=3), minimum_samples=3)
    assert decision.promote is True
    assert decision.rule == "score"


def test_the_minimum_applies_to_both_runs_not_the_smaller() -> None:
    """Three draws of the candidate against one draw of the baseline is still a coin toss."""
    decision = promotes(_run(0.8, samples=1), _run(1.0, samples=5), minimum_samples=3)
    assert decision.rule == UNDERSAMPLED


def test_a_minimum_of_one_is_the_deterministic_exemption() -> None:
    """FakeProvider replays. One draw is exactly the truth, and the caller says so with 1."""
    decision = promotes(_run(0.8), _run(1.0), minimum_samples=1)
    assert decision.promote is True and decision.rule == "score"


def test_the_declared_minimum_is_not_one() -> None:
    """The constant exists so a live caller has a number to reach for, and 1 would be no number."""
    assert MINIMUM_LIVE_SAMPLES >= 2


def test_the_promoter_holds_no_route_to_the_router_or_the_decider() -> None:
    """FR-119 (ticket 177): promotion is arithmetic, so no name under ``harnessed.router`` or
    ``harnessed.providers.jev`` is reachable from the promoter module, loaded or imported."""
    names = {
        f"{value.__module__}.{name}" if not isinstance(value, ModuleType) else value.__name__
        for name, value in vars(promoter_module).items()
        if isinstance(value, ModuleType) or hasattr(value, "__module__")
    }
    assert names, "the promoter exposes nothing; the check reads the wrong module"
    reached = sorted(
        name for name in names if name.startswith(("harnessed.router", "harnessed.providers.jev"))
    )
    assert reached == []
