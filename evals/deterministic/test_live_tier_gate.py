"""AC 2 - the live tier is **skipped with a reason**, never silently deselected.

The distinction is the whole criterion. ``pyproject.toml`` sets ``addopts = -m "not live"``, so a
test carrying the repository's ``live`` marker is *deselected*: pytest never imports its reason and
never prints one. An operator with a key sitting right there would see a green run and no hint that
the expensive half never happened.

So the live tier carries its own declared marker, ``live_eval``, plus a ``pytest.mark.skipif`` whose
reason names both gates it is waiting on - ``HARNESSED_RUN_LIVE_EVALS`` and the key variable of the
configured provider. This file spawns a child pytest over the live-tier module and reads the reason
back out of ``-rs``, because a reason that is never printed is a reason nobody can act on.

The child targets a **different** module, so there is no self-spawning recursion and no sentinel is
needed (learnings, ticket 033).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from evals.helpers import DOCUMENTED_ENV_VARS, RUN_LIVE_VAR, live_reason_for, load_dataset

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_MODULE = "evals/deterministic/test_tool_exchange.py"
PROBE_PROVIDER = "groq"
PROBE_KEY_VAR = "GROQ_API_KEY"

# ---- offline tier ------------------------------------------------------------------------------


def child_environment(home: Path) -> dict[str, str]:
    """This process's environment minus every documented variable, plus the probe's own.

    Subtracting rather than starting empty is deliberate: on Windows a minimal environment has no
    ``SystemRoot`` and no ``PATH``, and the child interpreter would fail to start for a reason with
    nothing to do with the assertion (learnings, ticket 030). ``HARNESSED_HOME`` points at a
    throwaway directory so the child's own results never touch the operator's.
    """
    env = {name: value for name, value in os.environ.items() if name not in DOCUMENTED_ENV_VARS}
    env["HARNESSED_PROVIDER"] = PROBE_PROVIDER
    env["HARNESSED_HOME"] = str(home)
    env.pop(RUN_LIVE_VAR, None)
    # Set, deliberately empty. Subtracting the name is not enough: importing ``deepeval`` puts
    # some provider key names back into ``os.environ`` for itself, and a name that is present for
    # another reason would decide this test's result. An empty value is how "the operator has no
    # key for this provider" is spelled in a way nothing downstream can quietly repair.
    env[PROBE_KEY_VAR] = ""
    return env


def run_child_pytest(home: Path) -> tuple[int, str]:
    """Run the live-tier module in a child with neither gate open, under the default selection.

    No ``-k`` and no ``-m``: the whole criterion is what the *default* selection does to a live
    test, and a filter of our own would answer a question nobody asked.
    """
    # The child runs from ``home``, not the repository root, and is handed the module as an
    # absolute path so it still resolves. Since ticket 040 the loader reads every documented
    # variable out of a ``.env`` in the working directory, so a child that sat in the repo root
    # would read the operator's own file - and on a machine whose file opens the live tier, the
    # "neither gate open" premise above was silently false. The env scrub below is correct and
    # was defeated by the file; the working directory is the other half of isolation.
    home.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", str(REPO_ROOT / LIVE_MODULE)],
        cwd=home,
        env=child_environment(home),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def test_the_live_tier_is_skipped_rather_than_deselected(tmp_path: Path) -> None:
    code, output = run_child_pytest(tmp_path / "home")
    assert code == 0, output
    assert f"{len(load_dataset())} skipped" in output, output
    assert "deselected" not in output, output


def test_the_skip_reason_names_both_gates(tmp_path: Path) -> None:
    _, output = run_child_pytest(tmp_path / "home")
    assert RUN_LIVE_VAR in output
    assert PROBE_KEY_VAR in output


def test_the_reason_string_is_reachable_without_spawning_anything() -> None:
    """The same sentence, built directly, so a change to its wording fails here first."""
    reason = live_reason_for(PROBE_PROVIDER, has_key=False, run_live=False)
    assert RUN_LIVE_VAR in reason
    assert PROBE_KEY_VAR in reason


def test_a_provider_that_is_not_configured_still_names_the_credentials_it_wants() -> None:
    reason = live_reason_for(None, has_key=False, run_live=False)
    assert RUN_LIVE_VAR in reason
    assert PROBE_KEY_VAR in reason


#: Repository-root entries that belong to whatever else is running, not to the child under test.
#: A bare before/after listing of the root fails whenever a coverage run, a type check or a lint
#: pass happens to be writing at the same moment, which says nothing about where results went.
#: Named explicitly rather than derived from ``git check-ignore``, so a leak into another ignored
#: path - ``evidence/``, ``.lavish/`` - still trips the assertion.
TOOLING_NOISE: tuple[str, ...] = (
    ".coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
)


def repository_entries() -> set[str]:
    return {
        path.name
        for path in REPO_ROOT.iterdir()
        if not any(path.name.startswith(prefix) for prefix in TOOLING_NOISE)
    }


def results_in(root: Path) -> set[str]:
    return {path.name for path in root.glob("*.jsonl")}


def test_the_child_wrote_its_results_into_its_own_home_and_not_the_repository(
    tmp_path: Path,
) -> None:
    """Both halves of the name, and the second one is the half that catches the real leak.

    A listing of the repository root alone cannot catch it: ``.harnessed/`` is already there, so
    a ``results.py`` that resolved a literal path instead of ``Settings.home`` would write into
    the repository's own results directory and leave the root's entry list identical. The set of
    result files in that directory is what actually moves when the leak happens.
    """
    home = tmp_path / "home"
    repository_results = REPO_ROOT / ".harnessed" / "evals"

    before_root = repository_entries()
    before_results = results_in(repository_results)

    run_child_pytest(home)

    # The child's results landed in the home it was given.
    assert results_in(home / "evals"), sorted(path.name for path in home.rglob("*"))
    # The repository's own results directory gained nothing.
    assert results_in(repository_results) == before_results
    # And the repository tree gained no entry of its own.
    assert repository_entries() == before_root
