"""FR-15: the grader set is deterministic, bounded, offline and total on bad input.

Graders are the arithmetic the whole thesis rests on. Every claim here is a claim about a pure
function: same input, same output, forever; a score inside ``[0.0, 1.0]``; a pass that derives
from a declared threshold and from nothing else; a named ``GraderError`` on malformed input; and
a stored score rounded to 6 decimal places.

The socket guard in ``conftest.py`` is autouse and this module is not marked ``live``, so every
test below runs with non-loopback connections blocked. ``test_socket_guard_is_active_while_grading``
makes that ambient fact an explicit assertion, because "no grader reaches a provider" is one of
the acceptance criteria rather than a happy accident of the fixture layout.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path
from typing import Any

import pytest

from harnessed.errors import EvalError, GraderError, HarnessError
from harnessed.evals import graders
from harnessed.evals.graders import GRADERS, Answer, Grade, grade, grade_all, round_score
from harnessed.evals.suite import GRADER_NAMES, GraderSpec, load_named_suite

GRADERS_SOURCE = Path(graders.__file__)

STDLIB_ALLOWED = {"__future__", "collections", "dataclasses", "json", "re", "typing"}


def spec(name: str, expect: dict[str, Any] | None = None, threshold: float = 1.0) -> GraderSpec:
    return GraderSpec(name=name, expect={} if expect is None else expect, threshold=threshold)


# ---- the vocabulary resolves -----------------------------------------------------------------


def test_every_name_the_suite_loader_allows_has_an_implementation() -> None:
    """A suite may only name these five; ticket 016 froze the list, 017 implements it."""
    assert tuple(sorted(GRADERS)) == tuple(sorted(GRADER_NAMES))


def test_the_committed_smoke_suite_resolves_every_grader_it_names() -> None:
    suite = load_named_suite("smoke")
    named = {grader.name for case in suite.cases for grader in case.graders}
    assert named
    assert named <= set(GRADERS)


# ---- determinism -----------------------------------------------------------------------------


CASES: tuple[tuple[GraderSpec, Answer], ...] = (
    (spec("exact_match", {"value": "Abuja"}), Answer(text="Abuja")),
    (spec("exact_match", {"value": "Abuja"}), Answer(text="the capital is Abuja")),
    (spec("contains_all", {"values": ["offline", "deterministic"]}), Answer(text="offline only")),
    (spec("json_object", {"required_keys": ["city"]}), Answer(text='{"city": "Abuja"}')),
    (spec("json_object", {"required_keys": ["city"]}), Answer(text="The city is Abuja.")),
    (spec("regex_match", {"pattern": r"^\d+\.\d+\.\d+$"}), Answer(text="0.1.0")),
    (spec("tool_called", {"name": "read_file"}), Answer(text="ok", tool_names=("read_file",))),
    (spec("tool_called", {"name": "read_file"}), Answer(text="ok")),
)


@pytest.mark.parametrize(("grader_spec", "answer"), CASES, ids=lambda value: str(value)[:40])
def test_repeated_calls_return_identical_output(grader_spec: GraderSpec, answer: Answer) -> None:
    first = grade(grader_spec, answer)
    for _ in range(10):
        assert grade(grader_spec, answer) == first


def test_grading_never_mutates_the_answer_or_the_spec() -> None:
    answer = Answer(text='{"city": "Abuja"}', tool_names=("read_file",))
    grader_spec = spec("json_object", {"required_keys": ["city"]})
    grade(grader_spec, answer)
    assert answer == Answer(text='{"city": "Abuja"}', tool_names=("read_file",))
    assert grader_spec == spec("json_object", {"required_keys": ["city"]})


# ---- the score is a bounded, rounded float ---------------------------------------------------


@pytest.mark.parametrize(("grader_spec", "answer"), CASES, ids=lambda value: str(value)[:40])
def test_every_score_is_a_float_inside_the_unit_interval(
    grader_spec: GraderSpec, answer: Answer
) -> None:
    result = grade(grader_spec, answer)
    assert isinstance(result.score, float)
    assert 0.0 <= result.score <= 1.0


@pytest.mark.parametrize(("grader_spec", "answer"), CASES, ids=lambda value: str(value)[:40])
def test_every_score_is_rounded_to_six_decimal_places(
    grader_spec: GraderSpec, answer: Answer
) -> None:
    score = grade(grader_spec, answer).score
    assert score == round(score, 6)


def test_a_partial_score_is_rounded_rather_than_carried_at_full_precision() -> None:
    """Two of three keywords present is 0.666666..., stored as 0.666667."""
    result = grade(
        spec("contains_all", {"values": ["alpha", "beta", "gamma"]}),
        Answer(text="alpha and beta"),
    )
    assert result.score == 0.666667


@pytest.mark.parametrize("value", [-0.000001, 1.000001, float("nan"), float("inf")])
def test_round_score_rejects_a_value_outside_the_unit_interval(value: float) -> None:
    with pytest.raises(GraderError):
        round_score(value)


# ---- pass or fail derives from the declared threshold ----------------------------------------


def test_pass_derives_from_the_declared_threshold_and_nothing_else() -> None:
    answer = Answer(text="alpha and beta")
    expect = {"values": ["alpha", "beta", "gamma"]}

    lenient = grade(spec("contains_all", expect, threshold=0.5), answer)
    strict = grade(spec("contains_all", expect, threshold=1.0), answer)

    assert lenient.score == strict.score
    assert lenient.passed is True
    assert strict.passed is False
    assert (lenient.threshold, strict.threshold) == (0.5, 1.0)


def test_a_score_exactly_on_the_threshold_passes() -> None:
    result = grade(
        spec("contains_all", {"values": ["alpha", "beta"]}, threshold=0.5),
        Answer(text="alpha only"),
    )
    assert (result.score, result.passed) == (0.5, True)


def test_a_grade_records_the_grader_that_produced_it() -> None:
    result = grade(spec("exact_match", {"value": "Abuja"}), Answer(text="Abuja"))
    assert isinstance(result, Grade)
    assert result.grader == "exact_match"


def test_grade_all_applies_every_spec_in_declared_order() -> None:
    suite = load_named_suite("smoke")
    case = suite.case("smoke-005-replay")
    grades = grade_all(case.graders, Answer(text="it runs offline", tool_names=("fs_read",)))
    assert tuple(result.grader for result in grades) == tuple(g.name for g in case.graders)
    assert all(result.passed for result in grades)


# ---- each grader's own arithmetic ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Abuja", 1.0), ("  Abuja  ", 1.0), ("abuja", 0.0), ("Abuja.", 0.0), ("", 0.0)],
)
def test_exact_match_scores_one_only_for_the_declared_value(text: str, expected: float) -> None:
    assert grade(spec("exact_match", {"value": "Abuja"}), Answer(text=text)).score == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("offline and deterministic", 1.0),
        ("OFFLINE AND DETERMINISTIC", 1.0),
        ("offline", 0.5),
        ("neither word", 0.0),
    ],
)
def test_contains_all_scores_the_fraction_of_values_present(text: str, expected: float) -> None:
    result = grade(spec("contains_all", {"values": ["offline", "deterministic"]}), Answer(text))
    assert result.score == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"city": "Abuja"}', 1.0),
        ('  {"city": "Abuja", "country": "Nigeria"}  ', 1.0),
        ('{"country": "Nigeria"}', 0.0),
        ('The city is {"city": "Abuja"}', 0.0),
        ('```json\n{"city": "Abuja"}\n```', 0.0),
        ('["Abuja"]', 0.0),
        ('"Abuja"', 0.0),
        ("not json at all", 0.0),
        ("", 0.0),
    ],
)
def test_json_object_scores_the_bare_object_only(text: str, expected: float) -> None:
    assert grade(spec("json_object", {"required_keys": ["city"]}), Answer(text)).score == expected


def test_json_object_scores_the_fraction_of_required_keys_present() -> None:
    result = grade(
        spec("json_object", {"required_keys": ["city", "country"]}), Answer('{"city": "Abuja"}')
    )
    assert result.score == 0.5


def test_json_object_with_no_required_keys_scores_the_shape_alone() -> None:
    assert grade(spec("json_object"), Answer("{}")).score == 1.0
    assert grade(spec("json_object"), Answer("[]")).score == 0.0


@pytest.mark.parametrize(
    ("text", "expected"), [("0.1.0", 1.0), ("10.20.30", 1.0), ("v0.1.0", 0.0), ("0.1", 0.0)]
)
def test_regex_match_scores_one_when_the_pattern_matches(text: str, expected: float) -> None:
    result = grade(spec("regex_match", {"pattern": r"^\d+\.\d+\.\d+$"}), Answer(text))
    assert result.score == expected


def test_regex_match_reads_the_whole_answer_not_only_its_first_line() -> None:
    """A multiline flag would let ``^\\d+$`` match line two of a two-line answer."""
    assert grade(spec("regex_match", {"pattern": r"^\d+$"}), Answer("one\n2")).score == 0.0


@pytest.mark.parametrize(
    ("names", "expected"),
    [(("read_file",), 1.0), (("list_dir", "read_file"), 1.0), (("list_dir",), 0.0), ((), 0.0)],
)
def test_tool_called_scores_one_when_the_named_tool_was_called(
    names: tuple[str, ...], expected: float
) -> None:
    result = grade(spec("tool_called", {"name": "read_file"}), Answer("ok", tool_names=names))
    assert result.score == expected


# ---- malformed input raises GraderError, never a bare exception ------------------------------


MALFORMED: tuple[tuple[str, dict[str, Any]], ...] = (
    ("exact_match", {}),
    ("exact_match", {"value": 42}),
    ("exact_match", {"value": "Abuja", "extra": "no"}),
    ("contains_all", {}),
    ("contains_all", {"values": []}),
    ("contains_all", {"values": "offline"}),
    ("contains_all", {"values": ["offline", 7]}),
    ("json_object", {"required_keys": "city"}),
    ("json_object", {"required_keys": [7]}),
    ("regex_match", {}),
    ("regex_match", {"pattern": "["}),
    ("regex_match", {"pattern": 7}),
    ("tool_called", {}),
    ("tool_called", {"name": ""}),
    ("tool_called", {"name": ["read_file"]}),
)


@pytest.mark.parametrize(("name", "expect"), MALFORMED, ids=lambda value: str(value)[:40])
def test_malformed_expectations_raise_grader_error(name: str, expect: dict[str, Any]) -> None:
    with pytest.raises(GraderError) as caught:
        grade(spec(name, expect), Answer(text="anything"))
    assert name in str(caught.value)


def test_an_unknown_grader_name_raises_grader_error_naming_the_known_set() -> None:
    with pytest.raises(GraderError) as caught:
        grade(spec("vibes"), Answer(text="anything"))
    message = str(caught.value)
    assert "vibes" in message
    assert all(name in message for name in GRADER_NAMES)


@pytest.mark.parametrize("threshold", [-0.1, 1.1, float("nan")])
def test_a_threshold_outside_the_unit_interval_raises_grader_error(threshold: float) -> None:
    with pytest.raises(GraderError):
        grade(
            GraderSpec(name="exact_match", expect={"value": "a"}, threshold=threshold),
            Answer(text="a"),
        )


def test_grader_error_is_a_harness_error_child_under_eval_error() -> None:
    """FR-2: every failure is a named child of the one tree, so the CLI can render it."""
    assert issubclass(GraderError, EvalError)
    assert issubclass(GraderError, HarnessError)


# ---- offline by construction ------------------------------------------------------------------


def test_socket_guard_is_active_while_grading() -> None:
    """AC 3: the guard is installed, and grading every smoke case opens nothing."""
    assert getattr(socket.socket.connect, "_harnessed_socket_guard", False) is True
    assert getattr(socket.socket.connect_ex, "_harnessed_socket_guard", False) is True
    assert getattr(socket.create_connection, "_harnessed_socket_guard", False) is True

    suite = load_named_suite("smoke")
    answer = Answer(text='{"city": "Abuja"}', tool_names=("read_file",))
    for case in suite.cases:
        grade_all(case.graders, answer)

    with pytest.raises(RuntimeError, match="blocked outbound connection"):
        socket.create_connection(("api.example.com", 9))


def test_the_grader_module_imports_nothing_that_could_reach_a_provider() -> None:
    """A pure function cannot call a model. Structure, not intention, is what proves it."""
    allowed = {"harnessed.errors", "harnessed.evals.suite"}
    tree = ast.parse(GRADERS_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            if module.startswith("harnessed"):
                assert module in allowed, f"graders.py imports {module!r}"
            else:
                assert module.split(".")[0] in STDLIB_ALLOWED, module


def test_the_grader_module_performs_no_io() -> None:
    io_modules = {"os", "io", "pathlib", "socket", "subprocess", "urllib", "httpx", "sqlite3"}
    io_calls = {"open", "print", "input", "exec", "eval", "compile"}
    tree = ast.parse(GRADERS_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in io_modules, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in io_modules, node.module
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in io_calls, node.func.id


def test_the_grader_module_stays_outside_the_tool_argument_quarantine() -> None:
    """Ticket 003 leaves exactly one offender for that text scan; this module is not a second.

    ``json_object`` reads the *answer* to score it and never produces a ``ToolCall`` or the
    argument-parse marker, so it is not the quarantined parse. Asserting the spelling here keeps
    the distinction deliberate rather than incidental.
    """
    source = GRADERS_SOURCE.read_text(encoding="utf-8")
    assert "json." + "loads" not in source
    assert "_arg" + "_parse_error" not in source


# ---- the verdict grader (ticket 236, decision 83) ---------------------------------------------


def _verdict_grade(verdict: str, answer: Answer, traits: tuple[str, ...] = ()) -> bool:
    spec = GraderSpec("verdict", {"verdict": verdict, "traits": list(traits)})
    return grade(spec, answer).passed


def test_verdict_accept_needs_a_reply_holding_every_trait() -> None:
    assert _verdict_grade("accept", Answer(text="A Launch email, sent."), ("launch email",))
    assert not _verdict_grade("accept", Answer(text="nothing"), ("launch email",))
    assert not _verdict_grade("accept", Answer(failed=True))
    assert _verdict_grade("accept", Answer(text="anything"))


def test_verdict_reject_passes_on_a_failed_hop_or_an_absent_trait() -> None:
    assert _verdict_grade("reject", Answer(failed=True))
    assert _verdict_grade("reject", Answer(text="nothing"), ("launch email",))
    assert not _verdict_grade("reject", Answer(text="the launch email"), ("launch email",))
