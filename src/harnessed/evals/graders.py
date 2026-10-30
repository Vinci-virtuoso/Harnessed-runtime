"""Deterministic graders: a score in ``[0.0, 1.0]`` plus a pass drawn from a declared threshold.

FR-15. A model may inform a grader's score; a model never decides a promotion. That sentence is
the whole reason this module is shaped the way it is:

- **Pure.** Every grader is a function of ``(Answer, expect)`` and nothing else. No clock, no id,
  no environment, no file, no socket - so "a grader cannot reach a provider" is a fact about the
  import list rather than a promise in a docstring. ``tests/test_graders.py`` asserts both.
- **Total.** Malformed input raises :class:`GraderError`, never a bare exception. A grader runs
  inside a scored run; an unnamed traceback there would end the run instead of failing the case.
- **Bounded and rounded.** A raw score is admitted through :func:`round_score`, which rejects
  anything outside the unit interval and rounds to 6 dp - the precision ``architecture.md`` fixes
  for a stored score. Rounding here, at the point the number is made, is what keeps the number
  the ledger holds identical to the number the grader computed.

**Comparison is not this module's business.** A :class:`Grade` reports one score against one
declared threshold, which is what "pass or fail derives from a declared threshold" means. Asking
whether one *run* beat another is a different question with an epsilon rule attached, and it
lives in ``evals/scoring.py`` and ``improve/promoter.py``, there and nowhere else (All AI agents
MUST rule 1).

The names live in ``suite.py::GRADER_NAMES``, which is the vocabulary a suite is validated
against; :data:`GRADERS` here is the matching set of implementations, and a test asserts the two
agree. A suite entry is never renamed to fit an implementation - the implementation is added.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from harnessed.errors import GraderError
from harnessed.evals.suite import GRADER_NAMES, GraderSpec

__all__ = [
    "GRADERS",
    "SCORE_PRECISION",
    "Answer",
    "Grade",
    "grade",
    "grade_all",
    "round_score",
]

#: Decimal places a score carries, in memory and in the ledger alike (``architecture.md``,
#: "Format"). Six is enough to separate any fraction a grader over a handful of items can make,
#: and few enough that two equal scores compare equal as floats.
SCORE_PRECISION: Final = 6

#: Answer text is parsed as JSON by ``json_object`` through this decoder. It is deliberately not
#: the string-form call that ``providers/translate.py`` quarantines: that quarantine covers the
#: parse of tool-call *arguments*, whose failure produces the typed marker on ``ToolCall``.
#: Nothing here produces a ``ToolCall``, a marker, or a message for the model - this reads a
#: finished answer to score it, and the score is the only thing that leaves.
_DECODER: Final = json.JSONDecoder()


# ---- what a grader is handed and what it returns ---------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """One case's finished output, reduced to exactly what a grader may see.

    ``text`` is the reply and ``tool_names`` is the work, in call order. The runner (ticket 016's
    sibling, FR-16) projects a ``LoopResult`` onto this; graders never see the loop, the
    messages, the provider or the request. Narrowing the input is what makes the pure-function
    claim checkable: there is nothing here to reach a model with.
    """

    text: str = ""
    tool_names: tuple[str, ...] = ()
    #: A work case's pinned hop ended in a failure, so no reply was produced (ticket 236).
    failed: bool = False


@dataclass(frozen=True, slots=True)
class Grade:
    """One grader's verdict on one answer: the score, the line it was held to, and the outcome."""

    grader: str
    score: float
    threshold: float
    passed: bool
    detail: str = ""


class _Grader(Protocol):
    """A grader: an answer and a validated expectation in, a raw score in ``[0, 1]`` out."""

    def __call__(self, answer: Answer, expect: Mapping[str, Any]) -> tuple[float, str]: ...


# ---- the numeric contract --------------------------------------------------------------------


def round_score(value: float) -> float:
    """Admit a raw score: finite, inside ``[0.0, 1.0]``, rounded to :data:`SCORE_PRECISION`.

    Every grader returns through here, so the bound and the precision are enforced once rather
    than restated per grader - and a grader whose arithmetic drifts out of range fails loudly at
    the moment it does, not silently in the mean three layers up.
    """
    score = float(value)
    if score != score or score in (float("inf"), float("-inf")):
        raise GraderError(f"a grader produced the non-finite score {value!r}; scores are floats")
    if not 0.0 <= score <= 1.0:
        raise GraderError(
            f"a grader produced the score {value!r}, which is outside the [0.0, 1.0] range every "
            f"grader scores on."
        )
    return round(score, SCORE_PRECISION)


def _fraction(matched: int, total: int) -> float:
    """Partial credit, expressed once. ``total`` is validated non-zero before this is reached."""
    return round_score(matched / total)


# ---- expectation reading (every rejection is a GraderError) ----------------------------------


def _check_fields(name: str, expect: Mapping[str, Any], known: frozenset[str]) -> None:
    unknown = sorted(set(expect) - known)
    if unknown:
        raise GraderError(
            f"grader {name!r}: unknown field(s) in 'expect': {unknown}. "
            f"Known fields: {sorted(known)}."
        )


def _required_text(name: str, expect: Mapping[str, Any], key: str) -> str:
    if key not in expect:
        raise GraderError(f"grader {name!r}: 'expect' is missing the required key {key!r}")
    value = expect[key]
    if not isinstance(value, str) or not value.strip():
        raise GraderError(
            f"grader {name!r}: 'expect.{key}' must be a non-empty string, got {value!r}"
        )
    return value


def _string_list(
    name: str, expect: Mapping[str, Any], key: str, *, allow_empty: bool
) -> tuple[str, ...]:
    if key not in expect:
        if allow_empty:
            return ()
        raise GraderError(f"grader {name!r}: 'expect' is missing the required key {key!r}")
    value = expect[key]
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise GraderError(
            f"grader {name!r}: 'expect.{key}' must be a list of strings, got {value!r}"
        )
    items = tuple(value)
    if not items and not allow_empty:
        raise GraderError(f"grader {name!r}: 'expect.{key}' must name at least one item")
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise GraderError(
                f"grader {name!r}: every entry of 'expect.{key}' must be a non-empty string, "
                f"got {item!r}"
            )
    return items


def _fold(text: str) -> str:
    """One normalization for every textual comparison that is deliberately case-blind."""
    return text.casefold()


# ---- the graders ------------------------------------------------------------------------------


def _exact_match(answer: Answer, expect: Mapping[str, Any]) -> tuple[float, str]:
    """1.0 when the answer *is* the declared value, ignoring surrounding whitespace only.

    Case and punctuation count: this is the grader a case reaches for when the instruction was
    "answer with the city name and nothing else", and a grader that forgave trailing prose would
    score obedience it did not observe.
    """
    _check_fields("exact_match", expect, frozenset({"value"}))
    value = _required_text("exact_match", expect, "value")
    hit = answer.text.strip() == value.strip()
    return (1.0 if hit else 0.0, "" if hit else f"expected {value.strip()!r}")


def _contains_all(answer: Answer, expect: Mapping[str, Any]) -> tuple[float, str]:
    """The fraction of the declared values present in the answer, case-insensitively.

    Partial credit is the point: a summary carrying one of two required ideas scores 0.5, which
    is a number the flywheel can move, where a bare 0 would flatten every degree of wrong.
    """
    _check_fields("contains_all", expect, frozenset({"values"}))
    values = _string_list("contains_all", expect, "values", allow_empty=False)
    haystack = _fold(answer.text)
    missing = tuple(value for value in values if _fold(value) not in haystack)
    return (
        _fraction(len(values) - len(missing), len(values)),
        "" if not missing else f"missing: {', '.join(missing)}",
    )


def _json_object(answer: Answer, expect: Mapping[str, Any]) -> tuple[float, str]:
    """The fraction of required keys present, and 0.0 unless the answer is a bare JSON object.

    Strict by design. ``smoke-003-json-only`` is the case the flywheel rides on, and it fails at
    baseline precisely because the model wraps prose around the object; a grader that dug the
    object out of a fenced block or a sentence would erase the failure the reflector is gated on.
    """
    _check_fields("json_object", expect, frozenset({"required_keys"}))
    required = _string_list("json_object", expect, "required_keys", allow_empty=True)
    try:
        parsed: object = _DECODER.decode(answer.text)
    except ValueError as exc:
        return (0.0, f"not JSON: {exc}")
    if not isinstance(parsed, dict):
        return (0.0, f"JSON {type(parsed).__name__}, not an object")
    if not required:
        return (1.0, "")
    missing = tuple(key for key in required if key not in parsed)
    return (
        _fraction(len(required) - len(missing), len(required)),
        "" if not missing else f"missing key(s): {', '.join(missing)}",
    )


def _regex_match(answer: Answer, expect: Mapping[str, Any]) -> tuple[float, str]:
    """1.0 when the declared pattern matches the answer. No flags, so a pattern means one thing.

    ``re.MULTILINE`` is deliberately absent: with it, ``^...$`` would match *some* line of a
    rambling answer, and a case asking for a bare version number would pass on a paragraph that
    happened to contain one.
    """
    _check_fields("regex_match", expect, frozenset({"pattern"}))
    pattern = _required_text("regex_match", expect, "pattern")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise GraderError(
            f"grader 'regex_match': 'expect.pattern' {pattern!r} is not a valid regular "
            f"expression: {exc}"
        ) from exc
    hit = compiled.search(answer.text.strip()) is not None
    return (1.0 if hit else 0.0, "" if hit else f"no match for {pattern!r}")


def _tool_called(answer: Answer, expect: Mapping[str, Any]) -> tuple[float, str]:
    """1.0 when the named tool was called at least once during the run.

    This is what keeps a lucky final sentence from passing a case about tool use: the answer text
    is not consulted at all.
    """
    _check_fields("tool_called", expect, frozenset({"name"}))
    name = _required_text("tool_called", expect, "name")
    hit = name in answer.tool_names
    return (
        1.0 if hit else 0.0,
        "" if hit else f"called: {', '.join(answer.tool_names) or 'no tools'}",
    )


def _verdict(answer: Answer, expect: Mapping[str, Any]) -> tuple[float, str]:
    """A work case against the operator's own verdict (ticket 236, decision 83).

    ``accept`` (and ``traits``, which is an accept naming what must show) passes when the pinned
    handler produced a reply and the reply text holds every trait; ``reject`` passes when the hop
    failed or a trait is absent. Traits match the served reply text only, folded the way
    ``contains_all`` folds, never an output file name.
    """
    _check_fields("verdict", expect, frozenset({"verdict", "traits"}))
    verdict = _required_text("verdict", expect, "verdict")
    traits = expect.get("traits", [])
    if not isinstance(traits, list) or not all(isinstance(t, str) for t in traits):
        raise GraderError("grader 'verdict': 'expect.traits' must be a list of strings")
    text = _fold(answer.text)
    absent = [trait for trait in traits if _fold(trait) not in text]
    if verdict == "reject":
        hit = answer.failed or bool(absent)
        return (1.0 if hit else 0.0, "" if hit else "the reply held every trait; it was rejected")
    if verdict not in ("accept", "traits"):
        raise GraderError(f"grader 'verdict': {verdict!r} is not a verdict a grader scores")
    if answer.failed:
        return 0.0, "the pinned hop failed; no reply"
    hit = not absent
    return (1.0 if hit else 0.0, "" if hit else f"absent from the reply: {', '.join(absent)}")


#: The implementations behind ``suite.py::GRADER_NAMES``. The two sets are asserted equal, so a
#: grader defined here without a name there (or the reverse) is a test failure rather than a
#: suite that loads and then cannot be scored.
GRADERS: Final[Mapping[str, _Grader]] = {
    "contains_all": _contains_all,
    "exact_match": _exact_match,
    "json_object": _json_object,
    "regex_match": _regex_match,
    "tool_called": _tool_called,
    "verdict": _verdict,
}


# ---- the one entry point ----------------------------------------------------------------------


def _threshold(spec: GraderSpec) -> float:
    """The declared pass line, re-validated here because :func:`grade` is public.

    ``suite.py`` already rejects an out-of-range threshold at load, but a grade may be taken over
    a spec built in code, and a pass line outside the scale would make ``passed`` meaningless.
    """
    value = float(spec.threshold)
    if value != value or not 0.0 <= value <= 1.0:
        raise GraderError(
            f"grader {spec.name!r}: threshold {spec.threshold!r} is outside the [0.0, 1.0] range "
            f"every grader scores on."
        )
    return round(value, SCORE_PRECISION)


def grade(spec: GraderSpec, answer: Answer) -> Grade:
    """Score one answer with one grader. Same spec, same answer, same :class:`Grade`, always."""
    grader = GRADERS.get(spec.name)
    if grader is None:
        raise GraderError(
            f"unknown grader {spec.name!r}. Known graders: {', '.join(GRADER_NAMES)}."
        )
    threshold = _threshold(spec)
    score, detail = grader(answer, spec.expect)
    return Grade(
        grader=spec.name,
        score=round_score(score),
        threshold=threshold,
        passed=round_score(score) >= threshold,
        detail=detail,
    )


def grade_all(specs: Iterable[GraderSpec], answer: Answer) -> tuple[Grade, ...]:
    """Score one answer with every grader a case declares, in declared order."""
    return tuple(grade(spec, answer) for spec in specs)
