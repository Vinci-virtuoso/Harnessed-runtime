"""FR-5: the legibility budget is a test, not a reviewer's discretion.

``loop/engine.py`` is the teaching artifact of the project. Its value is that a person can read it
top to bottom in one sitting and see the entire trick, so the properties that make that true are
measured here rather than trusted to review (`loop-readability.md`):

* ``run_loop`` fits in a budget of non-blank, non-comment lines.
* The ``loop/`` package imports no vendor SDK, and nothing under ``providers/`` except the
  normalized types.
* The narration - the module docstring and the reason/act/observe section comments - is present
  and in order.
* The loop decodes no wire shape, never names the argument-parse marker, and holds no tool-name
  conditional.
* ``run_loop`` has exactly two exits and each one is labelled with the guardrail it fires.

Every rule is a helper function exercised twice: once against the real source, and once against a
synthetic module that violates it. The second half is what keeps this file from being a row of
assertions that would pass against anything.

What cannot be measured - whether the narration actually reads well - is deliberately left as a
human review item at the gate rather than dressed up as a test.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from harnessed.loop import guardrails
from harnessed.tools import default_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
LOOP_ROOT = PACKAGE_ROOT / "loop"
ENGINE_SOURCE = LOOP_ROOT / "engine.py"

#: The ceiling from `loop-readability.md`. The honest response to breaching it is to move the new
#: concern out into a guardrail, a journal method or a translation - never to raise this number.
BUDGET = 80

#: The three section comments, in the order a reader meets them.
NARRATION = ("# ---- reason", "# ---- act", "# ---- observe")

#: The wire-shape decoder quarantined to ``providers/translate.py`` (architecture decision 8).
QUARANTINED_CALL = "json" + ".loads"

#: Both spellings of the argument-parse marker: the field, and its serialized name.
MARKER_NAMES = frozenset({"parse_error", "_arg_parse_error"})

#: Vendor SDKs. The loop knows a model is something with a ``complete`` method and nothing more.
VENDOR_SDKS = ("anthropic", "openai")

#: The one module under ``providers/`` the loop may reach.
ALLOWED_PROVIDER_MODULE = "harnessed.providers.types"

#: The constants an exit may report. Spelled here as identifiers because the scan reads the AST.
NAMED_GUARDRAILS = ("ITERATION_LIMIT", "NO_MORE_TOOL_CALLS")


# ---- reading the source ------------------------------------------------------------------------


def loop_sources() -> list[Path]:
    """Every module in the ``loop/`` package, bytecode caches excluded."""
    return [path for path in sorted(LOOP_ROOT.rglob("*.py")) if "__pycache__" not in path.parts]


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def function_named(tree: ast.Module, name: str) -> ast.FunctionDef:
    """The one top-level ``def`` called ``name``. An ``async def`` is not a match, on purpose."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no synchronous def named {name!r}")


def spanned_lines(source: str, node: ast.stmt) -> list[str]:
    """The physical lines ``node`` occupies, decorators included."""
    assert node.end_lineno is not None
    decorators = [decorator.lineno for decorator in getattr(node, "decorator_list", [])]
    start = min([node.lineno, *decorators])
    return source.splitlines()[start - 1 : node.end_lineno]


# ---- the measurements ---------------------------------------------------------------------------


def measure(source: str, name: str) -> int:
    """Count the non-blank, non-comment lines of the function ``name`` in ``source``."""
    node = function_named(ast.parse(source), name)
    return sum(
        1
        for line in spanned_lines(source, node)
        if line.strip() and not line.strip().startswith("#")
    )


def missing_narration(source: str) -> list[str]:
    """Which of the docstring and the three section comments are absent, or out of order."""
    missing = []
    if not ast.get_docstring(ast.parse(source)):
        missing.append("module docstring")
    comments = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("#") and not line.strip().startswith("#:")
    ]
    cursor = 0
    for marker in NARRATION:
        found = next(
            (index for index, text in enumerate(comments[cursor:], cursor) if marker in text), None
        )
        if found is None:
            missing.append(marker)
        else:
            cursor = found + 1
    return missing


def provider_leaks(tree: ast.Module) -> list[str]:
    """Imports a provider-ignorant module may not make, as ``lineno: dotted.name`` strings."""
    leaks = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [] if node.level else [node.module or ""]
        else:
            continue
        for name in names:
            under_providers = name.startswith("harnessed.providers")
            vendor = name.split(".")[0] in VENDOR_SDKS
            if vendor or (under_providers and name != ALLOWED_PROVIDER_MODULE):
                leaks.append(f"{node.lineno}: {name}")
    return sorted(leaks)


def _names_the_marker(node: ast.expr | ast.keyword) -> bool:
    """Whether ``node`` spells either marker name: a read, a write, a keyword or a string."""
    if isinstance(node, ast.Attribute):
        return node.attr in MARKER_NAMES
    if isinstance(node, ast.keyword):
        return node.arg in MARKER_NAMES
    if isinstance(node, ast.Name):
        return node.id in MARKER_NAMES
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in MARKER_NAMES
    return False


def marker_references(tree: ast.Module) -> list[int]:
    """Every line naming either spelling of the argument-parse marker, in any position."""
    positions = ast.Attribute | ast.Name | ast.Constant | ast.keyword
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, positions) and _names_the_marker(node)
        }
    )


def tool_name_conditionals(tree: ast.Module, tool_names: frozenset[str]) -> list[str]:
    """Branches that decide something from which tool was asked for. The registry owns dispatch."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare | ast.MatchValue):
            continue
        constants = {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        reads_a_name = any(
            isinstance(child, ast.Attribute) and child.attr == "name" for child in ast.walk(node)
        )
        named = constants & tool_names
        if named or reads_a_name:
            hits.append(f"{node.lineno}: {sorted(named)}")
    return hits


def exits(source: str, name: str) -> list[tuple[str, str]]:
    """Every exit of ``name``, as a ``(label, guardrail)`` pair.

    An exit is a ``return``, a ``break`` or a ``raise``: any way control leaves the function. The
    label is the comment on the line above it and the guardrail is the constant the exit reports,
    so an exit that ends a run without naming which guardrail fired reads as unlabelled here.
    """
    lines = source.splitlines()
    found = []
    for node in ast.walk(function_named(ast.parse(source), name)):
        if not isinstance(node, ast.Return | ast.Break | ast.Raise):
            continue
        above = lines[node.lineno - 2].strip() if node.lineno >= 2 else ""
        label = above.lstrip("#").strip() if above.startswith("#") else ""
        constants = {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name) and child.id in NAMED_GUARDRAILS
        }
        found.append((label, constants.pop() if len(constants) == 1 else ""))
    return found


@pytest.fixture(scope="module")
def engine_source() -> str:
    return ENGINE_SOURCE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tool_names() -> frozenset[str]:
    """The builtin tool vocabulary, derived from the registry so a new tool is covered for free."""
    return frozenset(default_registry(REPO_ROOT).names())


# ---- run_loop fits in one sitting ---------------------------------------------------------------


def test_run_loop_fits_the_legibility_budget(engine_source: str) -> None:
    measured = measure(engine_source, "run_loop")
    assert measured <= BUDGET, (
        f"run_loop is {measured} lines against a budget of {BUDGET}; move the new concern out "
        "into a guardrail, a journal method or a translation rather than widening the budget"
    )


def test_the_budget_counts_only_real_lines() -> None:
    """A blank line and a comment are free; the signature, the docstring and the body are not."""
    padded = 'def run_loop():\n    """Doc."""\n\n    # a comment\n    return 1\n'
    assert measure(padded, "run_loop") == 3


def test_an_oversized_run_loop_is_caught() -> None:
    body = "\n".join(f"    x{index} = {index}" for index in range(BUDGET + 1))
    assert measure(f"def run_loop():\n{body}\n", "run_loop") > BUDGET


def test_run_loop_is_synchronous(engine_source: str) -> None:
    """Async lives behind ``acp/runtime.py::submit()``; ``function_named`` rejects a coroutine."""
    node = function_named(ast.parse(engine_source), "run_loop")
    assert not any(isinstance(child, ast.Await) for child in ast.walk(node))


# ---- the loop is provider-ignorant ---------------------------------------------------------------


def test_the_loop_imports_no_vendor_sdk_and_only_the_normalized_types() -> None:
    leaks = {relative(path): provider_leaks(parse(path)) for path in loop_sources()}
    assert {path: hits for path, hits in leaks.items() if hits} == {}


def test_the_import_scan_catches_a_vendor_sdk_and_a_sibling_provider_module() -> None:
    offending = (
        "import anthropic\n"
        "from openai import OpenAI\n"
        "from harnessed.providers.base import Provider\n"
        "from harnessed.providers.types import ChatRequest\n"
    )
    assert provider_leaks(ast.parse(offending)) == [
        "1: anthropic",
        "2: openai",
        "3: harnessed.providers.base",
    ]


def test_importing_the_loop_pulls_in_no_vendor_sdk() -> None:
    """The static scan reads one package; this proves nothing drags an SDK in transitively."""
    script = (
        "import sys, harnessed.loop\n"
        f"vendors = {VENDOR_SDKS!r}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in vendors))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == "[]", completed.stdout


# ---- the narration is part of the deliverable ----------------------------------------------------


def test_the_engine_keeps_its_docstring_and_section_comments(engine_source: str) -> None:
    assert missing_narration(engine_source) == []


def test_the_narration_scan_notices_a_missing_and_a_reordered_section() -> None:
    stripped = "x = 1\n# ---- act\n# ---- reason\n"
    assert missing_narration(stripped) == ["module docstring", "# ---- act", "# ---- observe"]


# ---- no wire shapes, no marker, no tool-name ladder ----------------------------------------------


def test_the_loop_decodes_no_wire_shape() -> None:
    offenders = [
        relative(path)
        for path in loop_sources()
        if QUARANTINED_CALL in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_loop_never_names_the_argument_parse_marker() -> None:
    hits = {relative(path): marker_references(parse(path)) for path in loop_sources()}
    assert {path: lines for path, lines in hits.items() if lines} == {}


def test_the_marker_scan_catches_a_read_a_keyword_and_a_string() -> None:
    offending = (
        "def f(call):\n"
        "    if call.parse_error:\n"
        "        return ToolCall(parse_error='x')\n"
        "    return '_arg_parse_error'\n"
    )
    assert marker_references(ast.parse(offending)) == [2, 3, 4]


def test_the_loop_holds_no_tool_name_conditional(tool_names: frozenset[str]) -> None:
    hits = {
        relative(path): tool_name_conditionals(parse(path), tool_names) for path in loop_sources()
    }
    assert {path: found for path, found in hits.items() if found} == {}


def test_the_conditional_scan_catches_a_dispatch_ladder(tool_names: frozenset[str]) -> None:
    name = sorted(tool_names)[0]
    offending = f"def f(call):\n    if call.name == {name!r}:\n        return 1\n    return 0\n"
    assert tool_name_conditionals(ast.parse(offending), tool_names) == [f"2: [{name!r}]"]


def test_the_tool_vocabulary_is_not_empty(tool_names: frozenset[str]) -> None:
    """Against an empty vocabulary the conditional scan would pass over a dispatch ladder."""
    assert tool_names


# ---- two exits, both labelled --------------------------------------------------------------------


def test_run_loop_has_exactly_two_exits_and_each_is_labelled(engine_source: str) -> None:
    found = exits(engine_source, "run_loop")
    assert len(found) == 2, f"expected two exits, found {found}"
    assert all(label.startswith("guardrail") for label, _ in found), found
    assert {guardrail for _, guardrail in found} == set(NAMED_GUARDRAILS)


def test_both_exits_are_named_in_the_module_that_owns_them() -> None:
    assert guardrails.GUARDRAILS == (guardrails.NO_MORE_TOOL_CALLS, guardrails.ITERATION_LIMIT)


def test_the_exit_scan_catches_an_unlabelled_third_exit() -> None:
    offending = (
        "def run_loop():\n"
        "    while True:\n"
        "        # guardrail 2: the ceiling.\n"
        "        return ITERATION_LIMIT\n"
        "        if x:\n"
        "            raise RuntimeError('boom')\n"
    )
    assert exits(offending, "run_loop") == [
        ("guardrail 2: the ceiling.", "ITERATION_LIMIT"),
        ("", ""),
    ]
