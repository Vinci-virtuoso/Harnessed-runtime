"""Q1/Q2: the epsilon rule, the promotion rule and the regression rule live in exactly one module.

Every assertion here is about a pure function over result sets, plus a set of structural scans
that make "exactly one module" a fact about the tree rather than a convention. A second
implementation of "is this better" is how a gate silently loosens (All AI agents MUST rule 1), so
the scans matter as much as the arithmetic.

Each scan is run twice, following ticket 012's discipline: once over the real ``src/`` tree, and
once over a synthetic module that breaks the rule. Without the second half a scan is a row of
assertions that would pass against anything.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from harnessed.errors import EvalError, HarnessError
from harnessed.evals import scoring
from harnessed.evals.graders import SCORE_PRECISION
from harnessed.evals.scoring import (
    EPSILON,
    CaseOutcome,
    Decision,
    RunResult,
    compare,
    mean_score,
    pass_count,
    promotes,
    regressions,
    strictly_greater,
)

SCORING_SOURCE = Path(scoring.__file__)
SRC_ROOT = Path(scoring.__file__).resolve().parents[2]

STDLIB_ALLOWED = {"__future__", "collections", "dataclasses", "math", "typing"}


def run(*cases: tuple[str, float, bool]) -> RunResult:
    """A run result from ``(case_id, score, passed)`` triples, in declared order."""
    return RunResult.from_cases(
        CaseOutcome(case_id=case_id, score=score, passed=passed) for case_id, score, passed in cases
    )


# ---- mean_score -------------------------------------------------------------------------------


def test_mean_score_is_the_plain_mean_of_the_case_scores() -> None:
    assert mean_score([1.0, 0.0]) == 0.5
    assert mean_score([0.25, 0.75, 0.5]) == 0.5


@pytest.mark.parametrize(
    "scores",
    [[0.0], [1.0], [0.0, 1.0], [0.3, 0.3, 0.4], [1.0, 1.0, 1.0], [0.1, 0.2, 0.7]],
)
def test_mean_score_stays_inside_the_unit_interval(scores: list[float]) -> None:
    value = mean_score(scores)
    assert isinstance(value, float)
    assert 0.0 <= value <= 1.0


def test_mean_score_is_rounded_to_six_decimal_places() -> None:
    value = mean_score([1.0, 0.0, 0.0])
    assert value == round(1 / 3, SCORE_PRECISION)
    assert value == round(value, 6)


def test_mean_score_of_no_cases_is_zero_and_can_never_beat_a_baseline() -> None:
    """No cases is no evidence. Scoring it 0.0 keeps an empty run fail-closed and total."""
    assert mean_score([]) == 0.0
    assert strictly_greater(mean_score([]), 0.0) is False


@pytest.mark.parametrize("score", [-0.000001, 1.000001, 2.0, -1.0])
def test_mean_score_rejects_a_case_score_outside_the_unit_interval(score: float) -> None:
    with pytest.raises(EvalError):
        mean_score([0.5, score])


def test_mean_score_rejects_a_score_that_is_not_a_number() -> None:
    with pytest.raises(EvalError):
        mean_score([float("nan")])


def test_scoring_errors_are_harness_error_children_under_eval_error() -> None:
    with pytest.raises(EvalError) as excinfo:
        mean_score([2.0])
    assert isinstance(excinfo.value, HarnessError)


# ---- pass_count -------------------------------------------------------------------------------


def test_pass_count_counts_the_passing_cases_and_nothing_else() -> None:
    result = run(("a", 1.0, True), ("b", 0.9, False), ("c", 0.2, True))
    assert pass_count(result.cases) == 2
    assert result.pass_count == 2


def test_a_run_result_reports_its_own_mean_score() -> None:
    result = run(("a", 1.0, True), ("b", 0.0, False))
    assert result.score == 0.5


def test_a_run_result_rejects_a_duplicate_case_id() -> None:
    with pytest.raises(EvalError, match="a"):
        run(("a", 1.0, True), ("a", 0.5, True))


# ---- strictly_greater and the epsilon ---------------------------------------------------------


def test_epsilon_is_the_declared_value() -> None:
    assert EPSILON == 1e-9


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (0.9, 0.8, True),
        (0.8, 0.9, False),
        (0.5, 0.5, False),
        (0.5 + 1e-10, 0.5, False),
        (0.5 + 1e-8, 0.5, True),
        (0.5 - 1e-10, 0.5, False),
    ],
)
def test_strictly_greater_is_true_only_beyond_the_epsilon(
    a: float, b: float, expected: bool
) -> None:
    assert strictly_greater(a, b) is expected


def test_strictly_greater_is_exactly_the_difference_rule() -> None:
    for a, b in [(0.4, 0.4), (0.400000001, 0.4), (0.41, 0.4), (0.39, 0.4)]:
        assert strictly_greater(a, b) is bool(a - b > EPSILON)


# ---- regressions, soft drops, new and missing cases --------------------------------------------


def test_regressions_are_exactly_the_cases_that_passed_before_and_fail_now() -> None:
    before = run(("a", 1.0, True), ("b", 1.0, True), ("c", 0.0, False))
    after = run(("a", 1.0, True), ("b", 0.0, False), ("c", 0.0, False))
    assert [item.case_id for item in regressions(before, after)] == ["b"]


def test_a_regression_carries_both_scores() -> None:
    before = run(("b", 0.9, True))
    after = run(("b", 0.1, False))
    (item,) = regressions(before, after)
    assert (item.case_id, item.score_before, item.score_after) == ("b", 0.9, 0.1)


def test_a_case_that_failed_before_and_fails_now_is_not_a_regression() -> None:
    before = run(("c", 0.2, False))
    after = run(("c", 0.1, False))
    assert regressions(before, after) == ()


def test_a_score_drop_that_still_passes_is_a_soft_drop_not_a_regression() -> None:
    before = run(("a", 0.9, True))
    after = run(("a", 0.7, True))
    assert regressions(before, after) == ()
    (drop,) = compare(before, after).soft_drops
    assert (drop.case_id, drop.score_before, drop.score_after) == ("a", 0.9, 0.7)


def test_a_score_rise_is_not_a_soft_drop() -> None:
    before = run(("a", 0.7, True))
    after = run(("a", 0.9, True))
    assert compare(before, after).soft_drops == ()


def test_a_drop_inside_the_epsilon_band_is_not_a_soft_drop() -> None:
    before = run(("a", 0.5, True))
    after = run(("a", 0.5 - 1e-10, True))
    assert compare(before, after).soft_drops == ()


def test_a_case_absent_from_the_baseline_is_new_and_never_a_regression() -> None:
    before = run(("a", 1.0, True))
    after = run(("a", 1.0, True), ("z", 0.0, False))
    comparison = compare(before, after)
    assert comparison.new_cases == ("z",)
    assert comparison.regressions == ()
    assert regressions(before, after) == ()


def test_a_case_absent_from_the_candidate_run_is_missing() -> None:
    before = run(("a", 1.0, True), ("b", 1.0, True))
    after = run(("a", 1.0, True))
    comparison = compare(before, after)
    assert comparison.missing_cases == ("b",)
    assert comparison.regressions == ()


def test_a_missing_case_fails_the_decision_closed() -> None:
    before = run(("a", 0.5, True), ("b", 0.5, True))
    after = run(("a", 1.0, True))
    decision = promotes(before, after)
    assert bool(decision) is False
    assert decision.rule == "missing_case"
    assert decision.missing_cases == ("b",)


def test_the_four_categories_are_reported_in_declared_case_order() -> None:
    before = run(("a", 1.0, True), ("b", 1.0, True), ("c", 0.9, True))
    after = run(("a", 0.0, False), ("c", 0.5, True), ("d", 1.0, True))
    comparison = compare(before, after)
    assert [item.case_id for item in comparison.regressions] == ["a"]
    assert [item.case_id for item in comparison.soft_drops] == ["c"]
    assert comparison.new_cases == ("d",)
    assert comparison.missing_cases == ("b",)


# ---- promotes: the single entry point ----------------------------------------------------------


def test_a_strictly_higher_score_promotes_on_the_score_path() -> None:
    before = run(("a", 0.5, True))
    after = run(("a", 0.9, True))
    decision = promotes(before, after)
    assert bool(decision) is True
    assert decision.rule == "score"


def test_equal_scores_with_a_higher_pass_count_promote_and_name_the_tiebreak() -> None:
    before = run(("a", 0.4, False), ("b", 0.6, True))
    after = run(("a", 0.4, True), ("b", 0.6, True))
    decision = promotes(before, after)
    assert bool(decision) is True
    assert decision.rule == "tiebreak"
    assert decision.score_before == decision.score_after
    assert (decision.pass_count_before, decision.pass_count_after) == (1, 2)


def test_a_tiebreak_promotion_is_never_mistakable_for_a_score_win() -> None:
    before = run(("a", 0.4, False))
    after = run(("a", 0.4, True))
    assert promotes(before, after).rule != "score"


@pytest.mark.parametrize("passed", [False, True])
def test_equal_scores_with_an_equal_pass_count_do_not_promote(passed: bool) -> None:
    before = run(("a", 0.5, passed))
    after = run(("a", 0.5, passed))
    decision = promotes(before, after)
    assert bool(decision) is False
    assert decision.rule == "no_gain"


def test_equal_scores_with_a_lower_pass_count_do_not_promote() -> None:
    before = run(("a", 0.5, True), ("b", 0.5, True))
    after = run(("a", 0.5, True), ("b", 0.5, False))
    assert bool(promotes(before, after)) is False


def test_a_lower_score_never_promotes_however_the_pass_count_moves() -> None:
    """The tiebreak lives inside the equality band and cannot rescue a worse score."""
    before = run(("a", 1.0, True), ("b", 0.0, False), ("c", 0.0, False))
    after = run(("a", 0.0, False), ("b", 0.4, True), ("c", 0.4, True))
    decision = promotes(before, after)
    assert decision.score_after < decision.score_before
    assert decision.pass_count_after > decision.pass_count_before
    assert bool(decision) is False


def test_a_lower_score_with_a_higher_pass_count_and_no_regression_still_fails() -> None:
    before = run(("a", 0.9, False), ("b", 0.9, False))
    after = run(("a", 0.4, True), ("b", 0.4, True))
    decision = promotes(before, after)
    assert decision.regressions == ()
    assert decision.pass_count_after > decision.pass_count_before
    assert bool(decision) is False
    assert decision.rule == "no_gain"


def test_one_regression_rejects_a_candidate_whose_score_rose() -> None:
    before = run(("a", 1.0, True), ("b", 0.0, False))
    after = run(("a", 0.0, False), ("b", 1.0, True), ("c", 1.0, True))
    decision = promotes(before, after)
    assert strictly_greater(decision.score_after, decision.score_before) is True
    assert bool(decision) is False
    assert decision.rule == "regression"


def test_a_regression_is_not_tie_breakable() -> None:
    before = run(("a", 0.5, True), ("b", 0.5, False), ("c", 0.5, False))
    after = run(("a", 0.5, False), ("b", 0.5, True), ("c", 0.5, True))
    decision = promotes(before, after)
    assert decision.score_before == decision.score_after
    assert decision.pass_count_after > decision.pass_count_before
    assert bool(decision) is False
    assert decision.rule == "regression"


def test_the_decision_payload_carries_both_scores_both_counts_and_both_lists() -> None:
    before = run(("a", 1.0, True), ("b", 0.9, True))
    after = run(("a", 0.0, False), ("b", 0.5, True), ("z", 1.0, True))
    decision = promotes(before, after)
    assert isinstance(decision, Decision)
    assert decision.score_before == before.score
    assert decision.score_after == after.score
    assert decision.pass_count_before == before.pass_count
    assert decision.pass_count_after == after.pass_count
    assert [item.case_id for item in decision.regressions] == ["a"]
    assert [item.case_id for item in decision.soft_drops] == ["b"]
    assert decision.new_cases == ("z",)
    assert decision.missing_cases == ()


def test_the_deciding_rule_is_drawn_from_the_declared_vocabulary() -> None:
    """Pinned by name, so a rule cannot arrive without being noticed here.

    ``undersampled`` arrived with ticket 050: a promotion whose sides were drawn fewer times than
    the caller's minimum is refused, before any score is read, because a verdict on a coin is
    not a verdict. This test failed the moment the rule was added, which is the whole point of
    pinning the set rather than asserting it is a frozenset and nothing more.
    """
    assert set(scoring.RULES) == {
        "score",
        "tiebreak",
        "no_gain",
        "regression",
        "missing_case",
        "undersampled",
    }
    assert isinstance(scoring.RULES, frozenset)


def test_a_decision_is_truthy_exactly_when_it_promotes() -> None:
    promoted = promotes(run(("a", 0.5, True)), run(("a", 0.9, True)))
    rejected = promotes(run(("a", 0.9, True)), run(("a", 0.5, True)))
    assert promoted.promote is True
    assert bool(promoted) is True
    assert rejected.promote is False
    assert bool(rejected) is False


def test_promotes_is_deterministic_and_does_not_mutate_its_inputs() -> None:
    before = run(("a", 1.0, True), ("b", 0.5, True))
    after = run(("a", 1.0, True), ("b", 0.9, True))
    first = promotes(before, after)
    second = promotes(before, after)
    assert first == second
    assert before.cases == run(("a", 1.0, True), ("b", 0.5, True)).cases


def _matrix_cases(score: float, passes: int) -> list[tuple[str, float, bool]]:
    """Four cases carrying the given mean and the given pass count, with no regression possible."""
    return [(f"case-{index}", score, index < passes) for index in range(4)]


def test_promotes_agrees_with_the_rule_the_architecture_states() -> None:
    """The published formula, evaluated beside the implementation over a decision matrix."""
    matrix = [
        (0.5, 0.9, 1, 1),
        (0.9, 0.5, 1, 2),
        (0.5, 0.5, 1, 2),
        (0.5, 0.5, 2, 2),
        (0.5, 0.5, 2, 1),
        (0.5, 0.5 + 1e-10, 1, 1),
    ]
    for before_score, after_score, before_passes, after_passes in matrix:
        score_ok = (after_score - before_score) > EPSILON or (
            abs(after_score - before_score) <= EPSILON and after_passes > before_passes
        )
        before = run(*_matrix_cases(before_score, before_passes))
        after = run(*_matrix_cases(after_score, after_passes))
        decision = promotes(before, after)
        assert bool(decision) is (score_ok and not decision.regressions)


# ---- structure: the rules exist exactly once ---------------------------------------------------


def _module_sources() -> list[Path]:
    return sorted(path for path in SRC_ROOT.rglob("*.py") if path != SCORING_SOURCE)


def _epsilon_declarations(source: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and "EPSILON" in target.id.upper():
                names.append(target.id)
    return names


def _scoreish(name: str) -> bool:
    lowered = name.lower()
    return "score" in lowered or "pass_count" in lowered


def _is_score_like(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute):
        return _scoreish(node.attr)
    if isinstance(node, ast.Name):
        return _scoreish(node.id)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            return _scoreish(func.attr)
        if isinstance(func, ast.Name):
            return _scoreish(func.id)
    return False


def _inline_score_comparisons(source: str) -> list[int]:
    """Lines comparing two score-like operands with an ordering operator."""
    ordering = (ast.Gt, ast.GtE, ast.Lt, ast.LtE)
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ordering) for op in node.ops):
            continue
        operands = [node.left, *node.comparators]
        if sum(1 for operand in operands if _is_score_like(operand)) >= 2:
            lines.append(node.lineno)
    return lines


def _names_the_score_half(node: ast.expr) -> bool:
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"strictly_greater", "mean_score", "pass_count"}:
            return True
    return False


def _reassembled_promotion_rules(source: str) -> list[int]:
    """Lines where a caller combines the score half of the rule with a second condition."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.BoolOp) and any(
            _names_the_score_half(value) for value in node.values
        ):
            lines.append(node.lineno)
    return lines


def test_epsilon_is_declared_exactly_once_in_the_tree() -> None:
    assert _epsilon_declarations(SCORING_SOURCE.read_text(encoding="utf-8")) == ["EPSILON"]
    for path in _module_sources():
        assert _epsilon_declarations(path.read_text(encoding="utf-8")) == [], path


def test_no_module_outside_scoring_compares_two_scores_inline() -> None:
    for path in _module_sources():
        assert _inline_score_comparisons(path.read_text(encoding="utf-8")) == [], path


def test_no_module_outside_scoring_reassembles_the_promotion_rule() -> None:
    for path in _module_sources():
        assert _reassembled_promotion_rules(path.read_text(encoding="utf-8")) == [], path


def test_the_scoring_functions_are_defined_only_in_scoring() -> None:
    owned = {"promotes", "strictly_greater", "mean_score", "pass_count", "regressions", "compare"}
    for path in _module_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert node.name not in owned, f"{path} redefines {node.name}"


# ---- the scans are run against an offender too -------------------------------------------------

OFFENDING_INLINE = "def decide(before, after):\n    return after.score > before.score\n"
OFFENDING_REASSEMBLY = (
    "def decide(before, after, regs):\n"
    "    return strictly_greater(after.score, before.score) and not regs\n"
)
OFFENDING_EPSILON = "EPSILON = 1e-9\n"
INNOCENT = "def render(rows):\n    if len(rows) > 3:\n        return rows[:3]\n    return rows\n"


def test_the_inline_comparison_scan_catches_a_module_that_breaks_the_rule() -> None:
    assert _inline_score_comparisons(OFFENDING_INLINE) == [2]
    assert _inline_score_comparisons(INNOCENT) == []


def test_the_reassembly_scan_catches_a_caller_that_rebuilds_the_promotion_rule() -> None:
    assert _reassembled_promotion_rules(OFFENDING_REASSEMBLY) == [2]
    assert _reassembled_promotion_rules(INNOCENT) == []


def test_the_epsilon_scan_catches_a_second_declaration() -> None:
    assert _epsilon_declarations(OFFENDING_EPSILON) == ["EPSILON"]
    assert _epsilon_declarations(INNOCENT) == []


# ---- purity ------------------------------------------------------------------------------------


def test_the_scoring_module_imports_nothing_that_could_reach_a_provider() -> None:
    allowed = {"harnessed.errors", "harnessed.evals.graders"}
    tree = ast.parse(SCORING_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            if module.startswith("harnessed"):
                assert module in allowed, f"scoring.py imports {module!r}"
            else:
                assert module.split(".")[0] in STDLIB_ALLOWED, module


def test_the_scoring_module_performs_no_io() -> None:
    io_modules = {"os", "io", "pathlib", "socket", "subprocess", "urllib", "httpx", "sqlite3"}
    io_calls = {"open", "print", "input", "exec", "eval", "compile"}
    tree = ast.parse(SCORING_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in io_modules, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in io_modules, node.module
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in io_calls, node.func.id


def test_the_aggregate_takes_no_weight() -> None:
    """Weighted aggregates are rejected for v1: a weight is a knob tuned until the score rises."""
    tree = ast.parse(SCORING_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            assert "weight" not in node.arg.lower(), node.arg


def test_socket_guard_is_active_while_deciding() -> None:
    promotes(run(("a", 0.5, True)), run(("a", 0.9, True)))
    with pytest.raises(RuntimeError, match="blocked outbound connection"):
        socket.create_connection(("api.example.com", 9))
