"""AC 11 and AC 12 - the judge tier is optional, lazy, and structurally unable to gate a promotion.

Three separate claims, and none of them can be proved by running the judge tier - the point of the
first two is that it *does not run*. So they are proved by reading the source: an AST scan over
``evals/judge/`` for a module-scope ``deepeval`` import, and a scan over ``src/harnessed/`` for any
reach into this suite at all.

The third claim is the one that matters most. A judge score is a 0-1 percentage against a
threshold, not a 0/1 truth. ``improve/promoter.py`` decides promotions from deterministic results
and makes zero provider calls; if anything under ``src/harnessed/`` could import a judge metric,
that would stop being true. Nothing does, and this file is what keeps it that way.

There is no live tier in this file. It reads source text, which is the same on every machine.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JUDGE_PACKAGE = REPO_ROOT / "evals" / "judge"
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
PYPROJECT = REPO_ROOT / "pyproject.toml"
OPTIONAL_DEPENDENCY = "deepeval"
EXTRA = "eval"

# ---- offline tier ------------------------------------------------------------------------------


def module_scope_imports(path: Path) -> set[str]:
    """Every module a file imports at module scope. Imports inside a function do not count.

    That distinction is the criterion: a function-local ``import deepeval`` runs only when the
    judge tier actually runs, which is what lets the whole suite collect on a base install.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module.split(".")[0])
    return found


def judge_modules() -> list[Path]:
    return sorted(JUDGE_PACKAGE.rglob("*.py"))


def test_the_judge_package_exists_and_has_modules_to_scan() -> None:
    assert [path.name for path in judge_modules()]
    assert (JUDGE_PACKAGE / "judge_model.py").is_file()
    assert (JUDGE_PACKAGE / "test_response_quality.py").is_file()


@pytest.mark.parametrize("path", judge_modules(), ids=lambda path: path.name)
def test_deepeval_is_never_imported_at_module_scope(path: Path) -> None:
    assert OPTIONAL_DEPENDENCY not in module_scope_imports(path)


def test_the_judge_tier_names_deepeval_somewhere_or_the_scan_proves_nothing() -> None:
    """A scan for an import that no file was ever going to make is not a test."""
    text = "".join(path.read_text(encoding="utf-8") for path in judge_modules())
    assert OPTIONAL_DEPENDENCY in text


def test_the_optional_dependency_is_absent_from_the_base_install() -> None:
    manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    base = manifest["project"]["dependencies"]
    assert not [pin for pin in base if OPTIONAL_DEPENDENCY in pin]


def test_the_optional_dependency_is_present_only_under_the_eval_extra() -> None:
    manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    extras = manifest["project"]["optional-dependencies"]
    assert [pin for pin in extras[EXTRA] if OPTIONAL_DEPENDENCY in pin]
    others = {name: pins for name, pins in extras.items() if name != EXTRA}
    assert not [pin for pins in others.values() for pin in pins if OPTIONAL_DEPENDENCY in pin]


def test_nothing_in_the_package_reaches_the_judge_or_the_judge_library() -> None:
    """A percentage may not gate a promotion, and the cheapest guarantee is that it cannot."""
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if {OPTIONAL_DEPENDENCY, "evals"} & module_scope_imports(path)
        and path.parent != PACKAGE_ROOT / "evals"
    ]
    assert offenders == []


def test_the_promoter_still_makes_no_provider_call(tmp_path: Path) -> None:
    """The scan ``tests/test_promoter.py`` owns, restated here as the boundary the judge respects.

    ``improve/promoter.py`` is a pure function over three result sets. It has nothing to reach a
    provider with, and adding a judge metric to it would be the one change that made a 0-1
    percentage into a promotion decision.
    """
    promoter = PACKAGE_ROOT / "improve" / "promoter.py"
    imports = module_scope_imports(promoter)
    assert OPTIONAL_DEPENDENCY not in imports
    assert "harnessed" in imports
    assert "providers" not in promoter.read_text(encoding="utf-8")
