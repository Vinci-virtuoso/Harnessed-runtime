"""FR-3 - the verify harness and the package skeleton it runs against.

The acceptance criteria of ticket 001 in executable form: the six stages and their order, the
fail-fast contract, the coverage ratchet and its override, shell parity, and the checks
``harnessed doctor --offline`` performs.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from conftest import OFFLINE_PROVIDER
from harnessed import clock, ids
from harnessed.cli import doctor_cmd, render
from harnessed.cli.app import app
from harnessed.config import SECRET_VARS, Settings, load
from harnessed.errors import (
    ConfigError,
    DelegationError,
    EvalError,
    EventError,
    HarnessError,
    ImproveError,
    MissingCredentialError,
    ProviderError,
    StorageError,
    ToolError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_PS1 = REPO_ROOT / "scripts" / "verify.ps1"
VERIFY_SH = REPO_ROOT / "scripts" / "verify.sh"
VERIFY_DASHBOARD_PS1 = REPO_ROOT / "scripts" / "verify-dashboard.ps1"
VERIFY_DASHBOARD_SH = REPO_ROOT / "scripts" / "verify-dashboard.sh"
RATCHET = REPO_ROOT / ".spec" / "coverage-ratchet"

#: The phase-0 floor. The ratchet rises per phase and may never fall below where it started.
PHASE_0_RATCHET = 55
#: The current value, read once. Every assertion below derives from it rather than pinning a
#: phase, so a phase-end bump changes one file and not the test suite.
RATCHET_VALUE = int(RATCHET.read_text(encoding="utf-8").strip())

# A stand-in credential value. Deliberately not shaped like any real vendor key.
PLACEHOLDER_VALUE = "not-a-real-credential"

# The six stages, in the one order that is load-bearing: cheapest failure first.
EXPECTED_STAGES = (
    ("sync", ("uv", "sync", "--all-extras")),
    ("lint", ("uv", "run", "ruff", "check", ".")),
    ("format", ("uv", "run", "ruff", "format", "--check", ".")),
    ("types", ("uv", "run", "mypy", "src/harnessed")),
    (
        "tests",
        (
            "uv",
            "run",
            "pytest",
            "-q",
            "-m",
            "not live",
            "--cov=src/harnessed",
            "--cov-fail-under",
        ),
    ),
    ("doctor", ("uv", "run", "harnessed", "doctor", "--offline")),
)

runner = CliRunner()


# --------------------------------------------------------------------------------------------
# Stage order and shell parity
# --------------------------------------------------------------------------------------------


def _ps1_stages() -> list[tuple[str, tuple[str, ...]]]:
    text = VERIFY_PS1.read_text(encoding="utf-8")
    stages: list[tuple[str, tuple[str, ...]]] = []
    for line in text.splitlines():
        match = re.match(r"^Invoke-Stage\s+'(\w+)'\s+@\((.*)\)\s*$", line.strip())
        if match is None:
            continue
        parts = tuple(p.strip().strip("'\"") for p in match.group(2).split(","))
        stages.append((match.group(1), parts))
    return stages


def _sh_stages() -> list[tuple[str, tuple[str, ...]]]:
    text = VERIFY_SH.read_text(encoding="utf-8")
    stages: list[tuple[str, tuple[str, ...]]] = []
    for line in text.splitlines():
        match = re.match(r"^stage\s+(\w+)\s+(.*)$", line.strip())
        if match is None:
            continue
        # shlex, not split(): `-m "not live"` is one argument, and the parity assertion is
        # about the arguments the two shells pass, not about their quoting.
        parts = tuple(shlex.split(match.group(2)))
        stages.append((match.group(1), parts))
    return stages


def _normalize(stages: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, tuple[str, ...]]]:
    """Drop the interpolated ratchet value so the two shells' quoting can be compared."""
    normalized = []
    for name, parts in stages:
        trimmed = tuple(p.split("=")[0] if p.startswith("--cov-fail-under") else p for p in parts)
        normalized.append((name, trimmed))
    return normalized


def test_ps1_runs_the_six_stages_in_order() -> None:
    assert _normalize(_ps1_stages()) == list(EXPECTED_STAGES)


def test_sh_runs_the_identical_six_stages_in_the_identical_order() -> None:
    assert _normalize(_sh_stages()) == _normalize(_ps1_stages())


def test_both_scripts_default_to_the_ratchet_file() -> None:
    # Read rather than hardcode: the last ticket of each phase bumps this file, and an assertion
    # that has to be edited every phase is an assertion somebody eventually edits downward.
    assert RATCHET_VALUE >= PHASE_0_RATCHET, "the ratchet may rise, never fall (MUST rule 5)"
    assert ".spec/coverage-ratchet" in VERIFY_PS1.read_text(encoding="utf-8")
    assert ".spec/coverage-ratchet" in VERIFY_SH.read_text(encoding="utf-8")


def _testpaths() -> list[str]:
    """The default pytest selection, read out of the project configuration."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r"^testpaths\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert declared is not None, "pyproject.toml declares no testpaths"
    return [part.strip().strip("\"'") for part in declared.group(1).split(",") if part.strip()]


def test_the_default_selection_covers_every_suite_the_repository_ships() -> None:
    """Both test trees, or the gate is reporting on one of them and silent about the other.

    Regression, 2026-08-26 (ticket 046). ``testpaths`` named ``tests`` alone, so the composite
    gate never ran the eval suite ticket 035 built - 75 cases over tool dispatch, the iteration
    limit, the socket guard and the results writer. Ticket 040 then widened the environment file
    to supply every documented variable, and two absence-asserting cases in that suite began
    reading the developer's own file out of the repository root. They stayed red through six green
    gate runs and were found only because ticket 043 happened to run the suite by hand.

    Consistency rule 11 - sweep the absence-asserting tests whenever a configuration source
    widens - could not have caught it: the suite was outside 040's writable scope, so the sweep had
    nowhere to reach. The rule was followed and the defect shipped, because the guard that would
    have caught it was not run by anything.

    Fixed in the configuration rather than in the gate command on purpose. The person most likely
    not to run a suite is a developer typing ``pytest``, not the gate; fixing only the gate would
    leave the local command still quietly covering half the repository.

    By rule 12 this assertion cannot prove the suite *runs* - only that nobody dropped it from the
    selection. What proves it runs is the probe in ``evidence/046/``: an eval assertion broken on
    purpose, and a red gate.
    """
    declared = _testpaths()
    assert "tests" in declared
    assert "evals/deterministic" in declared, (
        "the eval suite is not in the default pytest selection, so neither the gate nor a "
        f"developer typing `pytest` runs it. Declared: {declared}"
    )


def test_the_judge_tier_is_left_out_of_the_default_selection() -> None:
    """A gate whose result is a percentage is not a gate.

    The judge tier scores 0-1 against a threshold and needs both the optional extra and a live
    key. Its exclusion is a decision, so it is named here rather than left to fall out of the
    ``live_eval`` marker: a marker keeps it from *executing*, but only this keeps it from being
    collected into the thing that decides whether the build is good.
    """
    declared = _testpaths()
    assert "evals" not in declared, (
        "`evals` selects the judge tier as well as the deterministic one. Name "
        "`evals/deterministic` so the exclusion is visible."
    )
    assert not any(path.startswith("evals/judge") for path in declared)


def test_both_scripts_declare_their_own_offline_provider() -> None:
    """The gate names ``fake`` for itself, in both scripts, or it is red on a clean clone.

    Regression, 2026-08-25 (ticket 036). Ticket 032 removed the silent ``fake`` default (FR-27),
    which is correct - an unconfigured harness must fail loudly. But the verify scripts had been
    leaning on that default, so from the moment 032 landed, stage 6 (``doctor --offline``) exited 1
    on any checkout without ``HARNESSED_PROVIDER`` in the environment.

    Nothing caught it for five tickets. The last ticket to gate on the full harness was 030, which
    ran before the default was removed; 032, 034 and 035 each gate only on their own test file, so
    every one was honestly green while the composite gate was broken. This assertion is what was
    missing. It is checked per script, so the two cannot drift apart the way the AFK launchers did.
    """
    for script in (VERIFY_PS1, VERIFY_SH):
        text = script.read_text(encoding="utf-8")
        declared = (
            f"HARNESSED_PROVIDER={OFFLINE_PROVIDER}" in text
            or f"$env:HARNESSED_PROVIDER = '{OFFLINE_PROVIDER}'" in text
        )
        assert declared, (
            f"{script.name} does not select the offline provider. Stage 6 shells out to "
            f"`harnessed doctor --offline`, which reads the real environment and fails when no "
            f"provider is configured, so the gate must name {OFFLINE_PROVIDER!r} itself."
        )


def test_neither_script_needs_an_undocumented_variable() -> None:
    """No key, and no env var beyond the documented set, appears in either script."""
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", env_example, re.MULTILINE))
    # The scripts' own shell locals, which are not process configuration.
    # Shell locals and PowerShell automatic variables. Neither is process configuration, so
    # neither belongs in .env.example.
    locals_ = {
        "COV_FAIL_UNDER",
        "REPO_ROOT",
        "STAGE",
        "NAME",
        "STATUS",
        "CDPATH",
        "LASTEXITCODE",
        "HEAD_BEFORE",
        "HEAD_NOW",
        "TREE_BEFORE",
        "TREE_NOW",
        "VOID_EXIT",
    }
    for script in (VERIFY_PS1, VERIFY_SH):
        text = script.read_text(encoding="utf-8")
        referenced = set(re.findall(r"\$env:([A-Z][A-Z0-9_]+)", text))
        referenced |= set(re.findall(r"\$\{?([A-Z][A-Z0-9_]{3,})\}?", text))
        assert (referenced - locals_) <= documented


# --------------------------------------------------------------------------------------------
# Fail-fast and the -CovFailUnder override, exercised against a shimmed `uv`
# --------------------------------------------------------------------------------------------

# Raw string: the newline escape below must reach the shim as an escape, not as a real newline
# inside a string literal, which would make the shim a syntax error on every call.
_SHIM_PY = textwrap.dedent(
    r"""
    import os, sys
    log = os.environ["HARNESSED_SHIM_LOG"]
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(" ".join(sys.argv[1:]) + "\n")
    with open(log, encoding="utf-8") as handle:
        calls = len([line for line in handle if line.strip()])
    sys.exit(1 if calls == int(os.environ.get("HARNESSED_SHIM_FAIL_AT", "0")) else 0)
    """
).strip()


def _shim_dir(tmp_path: Path) -> Path:
    """A fake `uv` on PATH that logs each invocation and can fail at a chosen stage."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "shim.py").write_text(_SHIM_PY, encoding="utf-8")
    # A .ps1, not a .bat: PowerShell resolves .ps1 from PATH the same way it resolves .bat, and
    # a .bat would drag in cmd.exe, which is not always usable in a sandboxed shell.
    (shim / "uv.ps1").write_text(
        f'& "{sys.executable}" "{shim / "shim.py"}" @args\nexit $LASTEXITCODE\n', encoding="utf-8"
    )
    uv_sh = shim / "uv"
    uv_sh.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{shim / "shim.py"}" "$@"\n', encoding="utf-8"
    )
    uv_sh.chmod(0o755)
    return shim


def _run_shimmed(script_argv: list[str], tmp_path: Path, fail_at: int) -> tuple[int, list[str]]:
    shim = _shim_dir(tmp_path)
    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    env["HARNESSED_SHIM_LOG"] = str(log)
    env["HARNESSED_SHIM_FAIL_AT"] = str(fail_at)
    completed = subprocess.run(
        script_argv,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return completed.returncode, [line for line in calls if line.strip()]


pwsh = shutil.which("pwsh")
posix_sh = shutil.which("sh")


@pytest.mark.slow
@pytest.mark.skipif(pwsh is None, reason="pwsh is not on PATH")
def test_ps1_stops_at_the_first_failing_stage(tmp_path: Path) -> None:
    argv = [str(pwsh), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(VERIFY_PS1)]
    code, calls = _run_shimmed(argv, tmp_path, fail_at=3)
    assert code != 0
    assert len(calls) == 3, f"later stages ran after the failure: {calls}"
    assert "format --check" in calls[2]


@pytest.mark.slow
@pytest.mark.skipif(pwsh is None, reason="pwsh is not on PATH")
def test_ps1_cov_fail_under_overrides_the_ratchet(tmp_path: Path) -> None:
    argv = [
        str(pwsh),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(VERIFY_PS1),
        "-CovFailUnder",
        "90",
    ]
    code, calls = _run_shimmed(argv, tmp_path, fail_at=5)
    assert code != 0
    assert "--cov-fail-under=90" in calls[4]
    assert f"--cov-fail-under={RATCHET_VALUE}" not in calls[4]


@pytest.mark.slow
@pytest.mark.skipif(pwsh is None, reason="pwsh is not on PATH")
def test_ps1_defaults_to_the_ratchet_value(tmp_path: Path) -> None:
    argv = [str(pwsh), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(VERIFY_PS1)]
    code, calls = _run_shimmed(argv, tmp_path, fail_at=5)
    assert code != 0
    assert f"--cov-fail-under={RATCHET_VALUE}" in calls[4]


@pytest.mark.slow
@pytest.mark.skipif(posix_sh is None, reason="no POSIX shell on PATH")
def test_sh_stops_at_the_first_failing_stage(tmp_path: Path) -> None:
    code, calls = _run_shimmed([str(posix_sh), str(VERIFY_SH)], tmp_path, fail_at=2)
    assert code != 0
    assert len(calls) == 2, f"later stages ran after the failure: {calls}"


# --------------------------------------------------------------------------------------------
# The quiet tree: a run whose tree moved is void (FR-159, decision 71)
# --------------------------------------------------------------------------------------------

VOID_LINE = "void: the tree changed under the run"
VOID_EXIT_CODE = 3

# A fake `git` that answers the two questions the harness asks. In "moving" mode the porcelain
# status differs on every call, which is a tree that changed under the run.
_GIT_SHIM_PY = textwrap.dedent(
    r"""
    import os, sys
    counter = os.environ["HARNESSED_GIT_COUNTER"]
    if sys.argv[1:2] == ["rev-parse"]:
        print("0123456789abcdef0123456789abcdef01234567")
    elif sys.argv[1:2] == ["status"]:
        calls = 0
        if os.path.exists(counter):
            with open(counter, encoding="utf-8") as handle:
                calls = int(handle.read() or 0)
        with open(counter, "w", encoding="utf-8") as handle:
            handle.write(str(calls + 1))
        if os.environ["HARNESSED_GIT_MODE"] == "moving":
            print(" M moved-%d.py" % calls)
        else:
            print(" M steady.py")
    sys.exit(0)
    """
).strip()


def _void_shims(tmp_path: Path) -> Path:
    """Fake ``git``, ``uv`` and ``npm`` on a directory of their own, for both shells."""
    shim = _shim_dir(tmp_path)
    (shim / "git_shim.py").write_text(_GIT_SHIM_PY, encoding="utf-8")
    for tool in ("git", "npm"):
        target = "git_shim.py" if tool == "git" else "shim.py"
        (shim / f"{tool}.ps1").write_text(
            f'& "{sys.executable}" "{shim / target}" @args\nexit $LASTEXITCODE\n', encoding="utf-8"
        )
        script = shim / tool
        script.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{shim / target}" "$@"\n', encoding="utf-8"
        )
        script.chmod(0o755)
    return shim


def _run_with_fake_git(
    script_argv: list[str], tmp_path: Path, *, mode: str, fail_at: int = 0
) -> subprocess.CompletedProcess[str]:
    shim = _void_shims(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    env["HARNESSED_SHIM_LOG"] = str(tmp_path / "calls.log")
    env["HARNESSED_SHIM_FAIL_AT"] = str(fail_at)
    env["HARNESSED_GIT_MODE"] = mode
    env["HARNESSED_GIT_COUNTER"] = str(tmp_path / "git.count")
    return subprocess.run(
        script_argv,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )


def _quiet_tree_scripts() -> list[tuple[str, list[str]]]:
    scripts: list[tuple[str, list[str]]] = []
    ps = [str(pwsh), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    for name, path in (("verify", VERIFY_SH), ("verify-dashboard", VERIFY_DASHBOARD_SH)):
        if posix_sh is not None:
            scripts.append((f"{name}.sh", [str(posix_sh), str(path)]))
    if pwsh is not None:
        scripts.append(("verify.ps1", [*ps, str(VERIFY_PS1)]))
        scripts.append(("verify-dashboard.ps1", [*ps, str(VERIFY_DASHBOARD_PS1)]))
    return scripts


QUIET_TREE_SCRIPTS = _quiet_tree_scripts()

#: Why a ``.sh`` case is skipped on Windows. The operator's PC ran these two through Git Bash and
#: the script printed the real ``HEAD``: the fake ``git`` on ``PATH`` was never the one it ran, so
#: the tree never moved and the exit was 0 or 1 where 3 is claimed (ticket 263). The ``.ps1``
#: twin of the same case runs the same shim under PowerShell and passes there: the claim on Windows.
_SH_ON_WINDOWS = (
    "Git Bash does not resolve the fake git from PATH on Windows; the {name}.ps1 case of this "
    "test covers the same assertion there; ci: windows job"
)


def _void_scripts() -> list[Any]:
    """``QUIET_TREE_SCRIPTS`` for the two void tests: each ``.sh`` case skips on win32, with its
    twin named; the ``.ps1`` cases never skip."""
    params: list[Any] = []
    for name, argv in QUIET_TREE_SCRIPTS:
        marks = []
        if name.endswith(".sh"):
            twin = name.removesuffix(".sh")
            marks.append(
                pytest.mark.skipif(sys.platform == "win32", reason=_SH_ON_WINDOWS.format(name=twin))
            )
        params.append(pytest.param(name, argv, id=name, marks=marks))
    return params


VOID_TREE_SCRIPTS = _void_scripts()


def test_each_windows_skip_of_a_void_case_has_a_ps1_twin() -> None:
    """A skip with no twin is not allowed: every ``.sh`` id has its ``.ps1`` id beside it, and
    the skip's reason names that twin."""
    ids = {param.id for param in VOID_TREE_SCRIPTS}
    for param in VOID_TREE_SCRIPTS:
        skips = [mark for mark in param.marks if mark.name == "skipif"]
        if not str(param.id).endswith(".sh"):
            assert skips == []
            continue
        twin = str(param.id).removesuffix(".sh")
        assert f"{twin}.ps1" in ids or pwsh is None, f"{param.id} is skipped with no twin"
        (skip,) = skips
        assert f"{twin}.ps1" in skip.kwargs["reason"]


@pytest.mark.slow
@pytest.mark.parametrize(("name", "argv"), VOID_TREE_SCRIPTS)
def test_a_tree_that_moved_under_the_run_is_void_and_exits_3(
    name: str, argv: list[str], tmp_path: Path
) -> None:
    done = _run_with_fake_git(argv, tmp_path, mode="moving")
    output = done.stdout + done.stderr
    assert done.returncode == VOID_EXIT_CODE, output
    assert VOID_LINE in output
    assert "0123456789abcdef0123456789abcdef01234567" in output, "HEAD is printed"
    assert output.count("status-sha256 ") >= 4, "both values are printed, before and after"
    # The stages themselves were green: the void is the tree's verdict, not theirs.
    assert "FAILED at stage" not in output


@pytest.mark.slow
@pytest.mark.parametrize(("name", "argv"), VOID_TREE_SCRIPTS)
def test_a_void_run_stays_void_when_a_stage_failed(
    name: str, argv: list[str], tmp_path: Path
) -> None:
    done = _run_with_fake_git(argv, tmp_path, mode="moving", fail_at=2)
    assert done.returncode == VOID_EXIT_CODE, done.stdout + done.stderr
    assert VOID_LINE in done.stdout + done.stderr


@pytest.mark.slow
@pytest.mark.parametrize(
    ("name", "argv"), QUIET_TREE_SCRIPTS, ids=[n for n, _ in QUIET_TREE_SCRIPTS]
)
def test_a_quiet_tree_prints_both_lines_and_is_not_void(
    name: str, argv: list[str], tmp_path: Path
) -> None:
    done = _run_with_fake_git(argv, tmp_path, mode="steady")
    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    assert VOID_LINE not in output
    assert "tree before:" in output and "tree after:" in output
    # The lines are not stages: the stage counts the scripts pin are unchanged.
    expected = "6/6" if name.startswith("verify.") else "7/7"
    assert f"[{expected[0]}/{expected[2]}]" in output


# --------------------------------------------------------------------------------------------
# The leaf modules: errors, config, ids, clock
# --------------------------------------------------------------------------------------------


def test_error_tree_is_complete_and_rooted() -> None:
    import harnessed.errors as errors_module

    tree = {
        ConfigError: ("MissingCredentialError",),
        ProviderError: ("FixtureMissError", "FixtureCorpusError", "TranslationError"),
        StorageError: ("MigrationError", "MigrationChecksumError"),
        ToolError: ("ToolNotFoundError", "ToolArgumentError"),
        EvalError: ("SuiteLoadError", "GraderError"),
        ImproveError: ("CandidateRejectedError", "InvalidCandidateError"),
        DelegationError: ("AgentNotFoundError", "AcpVersionMismatch", "DelegationTimeout"),
        EventError: ("UnknownEventError",),
    }
    for parent, children in tree.items():
        assert issubclass(parent, HarnessError)
        for child in children:
            assert issubclass(getattr(errors_module, child), parent), child


def test_missing_credential_names_every_place_the_key_may_go() -> None:
    """The fix it names must be one that works - both of them, since ticket 039.

    The wording is deliberately not asserted whole: ``render.py`` formats guidance off the fields,
    so the prose has to stay editable. What is pinned is that the message names *both* arms of the
    key order. It said "Add a line to .env" while no key was read from that file (ticket 037), and
    then said the file is not read for keys, which ticket 039 made false by making it read. Naming
    only one arm is how this message goes stale a third time.
    """
    error = MissingCredentialError("groq", "GROQ_API_KEY")
    text = str(error)
    assert error.provider == "groq"
    assert error.env_var == "GROQ_API_KEY"
    assert "GROQ_API_KEY" in text
    assert ".env" in text, "the file is a place the key may go, and the message must say so"
    assert "environment" in text, "so is the environment, and it is the one that wins"
    assert "not read for keys" not in text, "that stopped being true in ticket 039"
    assert "HARNESSED_PROVIDER=fake" in text, "the offline escape stays on the message"


def test_no_source_file_tells_an_operator_the_dotenv_file_is_ignored_for_keys() -> None:
    """The inverse of ticket 037's scan, because ticket 039 inverted the fact under it.

    037 scanned for "Add a line to .env" while no key was read from that file. 039 made the file
    the third arm of the key order, so the false instruction is now the *other* one: any string
    telling an operator their `.env` key does nothing sends them to export a variable they do not
    need to export. The scan survives with its subject swapped rather than being deleted, because
    what it guards is unchanged - one wrong sentence in one f-string fragment costs a debugging
    session, and the message is assembled from several.

    **Docstrings are exempt, deliberately.** ``config.py`` and ``errors.py`` quote the old wording
    to record why it changed, and a scan that could not tell an emitted instruction from prose
    *about* that instruction would force the explanation to be deleted - which is how the reason
    for a fix gets lost. The exemption is narrow: only the docstring of a module, class or
    function, never any other literal.
    """
    phrase = "not read for keys"
    offenders: list[str] = []
    for path in (REPO_ROOT / "src" / "harnessed").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and phrase in node.value
                and id(node) not in docstrings
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"{offenders} still tell an operator that a key in .env is ignored. Since ticket 039 "
        f"config.py reads the key vars back out of that file; the environment only wins over it."
    )


def test_settings_default_to_the_offline_provider(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chdir keeps a developer's own `.env` out of a claim about defaults. Since ticket 040
    that file supplies every documented variable, not the provider and the keys alone."""
    monkeypatch.chdir(tmp_path)
    settings = load()
    assert settings.provider == "fake"
    assert settings.api_key_for("fake") is None
    assert settings.retrieval_token_budget == 4000
    assert settings.home.name == ".harnessed"


def test_settings_read_only_documented_variables(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HARNESSED_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.setenv("HARNESSED_TOKEN_BUDGET", "1234")
    monkeypatch.setenv("GROQ_API_KEY", PLACEHOLDER_VALUE)
    settings = load()
    assert settings.provider == "groq"
    assert settings.retrieval_token_budget == 1234
    assert settings.api_key_for("groq") == PLACEHOLDER_VALUE
    assert settings.home == (tmp_path / "home")


def test_settings_reject_a_non_integer_budget(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_TOKEN_BUDGET", "lots")
    with pytest.raises(ConfigError, match="HARNESSED_TOKEN_BUDGET"):
        load()


def test_settings_are_frozen(clean_env: None) -> None:
    settings = load()
    with pytest.raises(AttributeError):
        settings.provider = "openai"  # type: ignore[misc]


def test_require_api_key_raises_missing_credential(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chdir is what makes "no key is set" true rather than machine-dependent (ticket 039).

    ``load()`` now reads the key vars out of a ``.env`` in the working directory, and a developer
    running the suite from a checkout they have configured has one. Without the chdir this
    assertion would pass in CI and fail on their machine, for a reason that is not a defect.
    """
    monkeypatch.chdir(tmp_path)
    settings = load()
    with pytest.raises(MissingCredentialError, match="GROQ_API_KEY"):
        settings.require_api_key("groq")


def test_secret_vars_cover_every_provider_key() -> None:
    assert "GROQ_API_KEY" in SECRET_VARS
    assert "ANTHROPIC_API_KEY" in SECRET_VARS
    assert all(name.endswith("_API_KEY") for name in SECRET_VARS)


def test_config_is_the_only_reader_of_environ() -> None:
    """All AI agents MUST rule 3: config.py is the only module touching os.environ."""
    offenders = []
    for path in (REPO_ROOT / "src" / "harnessed").rglob("*.py"):
        if path.name == "config.py":
            continue
        if re.search(r"os\.environ|os\.getenv", path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_run_ids_are_prefixed_monotonic_and_unique() -> None:
    first = ids.new_run_id()
    second = ids.new_run_id()
    assert first.startswith("run_")
    assert len(first) == len("run_") + 26
    assert second > first


def test_seeded_run_ids_are_deterministic() -> None:
    ids.seed(7)
    seeded = [ids.new_run_id() for _ in range(3)]
    ids.seed(7)
    assert [ids.new_run_id() for _ in range(3)] == seeded
    ids.seed(None)


def test_content_hash_is_stable_and_prefixed() -> None:
    payload = {"text": "hello", "cases": ["a", "b"]}
    first = ids.content_hash("cand", payload)
    assert first == ids.content_hash("cand", {"cases": ["a", "b"], "text": "hello"})
    assert first.startswith("cand_")
    assert len(first) == len("cand_") + 16


def test_clock_is_utc_and_iso_z() -> None:
    now = clock.now()
    offset = now.utcoffset()
    assert offset is not None and offset.total_seconds() == 0
    assert clock.isoformat(now).endswith("Z")


def test_clock_can_be_frozen() -> None:
    frozen = clock.parse("2026-08-22T00:00:00Z")
    clock.freeze(frozen)
    assert clock.now() == frozen
    assert clock.now() == frozen
    clock.unfreeze()
    assert clock.now() != frozen


def test_nothing_calls_datetime_now_outside_clock() -> None:
    offenders = []
    for path in (REPO_ROOT / "src" / "harnessed").rglob("*.py"):
        if path.name == "clock.py":
            continue
        if re.search(r"datetime\.now\(|datetime\.utcnow\(", path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


# --------------------------------------------------------------------------------------------
# harnessed doctor --offline
# --------------------------------------------------------------------------------------------


def test_doctor_offline_exits_zero(clean_env: None, tmp_harnessed_home: Path) -> None:
    result = runner.invoke(app, ["doctor", "--offline"])
    assert result.exit_code == 0, result.output


ALWAYS_CHECKED = {
    "python.version",
    "database.wal",
    "migrations.current",
    "fixtures.corpus",
    "sqlite.fts5",
    "provider.configured",
}


def test_doctor_asserts_every_declared_check(clean_env: None, tmp_harnessed_home: Path) -> None:
    """The check set, pinned - and since ticket 051 it depends on what was asked.

    ``provider.no_key_required`` answered the offline question to every caller, so a keyed live
    machine was told ``fail``. It is two checks now: ``provider.configured`` always, and
    ``provider.offline`` only under ``--offline``. A check that appears when it was not asked for
    is the old defect back.
    """
    plain = {check.name: check for check in doctor_cmd.run_checks(load())}
    assert set(plain) == ALWAYS_CHECKED
    assert plain["python.version"].status == "ok"
    assert plain["sqlite.fts5"].status == "ok"
    assert plain["provider.configured"].status == "ok"
    assert all(check.status != "fail" for check in plain.values())

    offline = {check.name: check for check in doctor_cmd.run_checks(load(), offline=True)}
    assert set(offline) == ALWAYS_CHECKED | {"provider.offline"}
    assert offline["provider.offline"].status == "ok"


def test_doctor_skips_are_never_silent(clean_env: None, tmp_harnessed_home: Path) -> None:
    for check in doctor_cmd.run_checks(load()):
        if check.status == "skip":
            assert check.detail.strip(), f"{check.name} skipped without a reason"


def test_doctor_json_is_a_single_machine_readable_object(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    result = runner.invoke(app, ["doctor", "--offline", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["offline"] is True
    assert {c["name"] for c in payload["checks"]} >= {"python.version", "sqlite.fts5"}


def test_doctor_offline_makes_zero_outbound_connections(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    with doctor_cmd.no_outbound() as attempts:
        doctor_cmd.run_checks(load())
    assert attempts == []


def test_no_outbound_records_and_refuses_a_connection() -> None:
    import socket

    # Calls the entry point `no_outbound` itself patches. Since FR-1 landed, the session-wide
    # socket guard owns `socket.create_connection`, so going through that helper would prove
    # the conftest guard works rather than the doctor's own offline contract.
    sock = socket.socket()
    with doctor_cmd.no_outbound() as attempts, pytest.raises(HarnessError):
        sock.connect(("127.0.0.1", 9))
    sock.close()
    assert attempts == [("127.0.0.1", 9)]


def test_doctor_reports_a_failed_check_with_a_non_zero_exit(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = (doctor_cmd.Check("python.version", "fail", "needs 3.12, found 3.9"),)
    monkeypatch.setattr(doctor_cmd, "run_checks", lambda settings: broken)
    result = runner.invoke(app, ["doctor", "--offline"])
    assert result.exit_code == 1


def test_doctor_reports_a_provider_that_needs_a_key_and_has_none_as_broken(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyed provider with no key is broken in every mode, and the message names the key."""
    monkeypatch.chdir(tmp_harnessed_home)
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    checks = {check.name: check for check in doctor_cmd.run_checks(load())}
    assert checks["provider.configured"].status == "fail"
    assert "GROQ_API_KEY" in checks["provider.configured"].detail


# Ticket 051. `doctor` failed a correctly configured live machine for not being offline.


def test_doctor_does_not_fail_a_keyed_provider_whose_key_is_present(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by the operator's QA pass: `fail` in red, two lines above `rung 1 live`.

    The provider check had no idea whether ``--offline`` was asked for. It failed a keyed provider
    unconditionally and told the operator to switch to ``fake`` - a fix that would make a working
    machine less configured. FR-29: doctor exits non-zero only when a configured rung is broken.
    """
    monkeypatch.chdir(tmp_harnessed_home)
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk" + "_" + "AAAABBBBCCCCDDDDEEEEFFFF")
    checks = {check.name: check for check in doctor_cmd.run_checks(load())}
    assert checks["provider.configured"].status == "ok", checks["provider.configured"].detail
    assert "groq" in checks["provider.configured"].detail
    assert "provider.offline" not in checks, "the offline question was not asked"


def test_doctor_offline_still_fails_a_keyed_provider(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --offline the question really is 'can this run without a network', and it cannot.

    This is stage 6 of the verify harness, and it must stay red for a keyed provider or the gate
    would pass a configuration that reaches for the network.
    """
    monkeypatch.chdir(tmp_harnessed_home)
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk" + "_" + "AAAABBBBCCCCDDDDEEEEFFFF")
    checks = {check.name: check for check in doctor_cmd.run_checks(load(), offline=True)}
    assert checks["provider.configured"].status == "ok"
    assert checks["provider.offline"].status == "fail"
    assert "not offline" in checks["provider.offline"].detail


def test_doctor_offline_passes_the_offline_provider(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    checks = {check.name: check for check in doctor_cmd.run_checks(load(), offline=True)}
    assert checks["provider.configured"].status == "ok"
    assert checks["provider.offline"].status == "ok"


# --------------------------------------------------------------------------------------------
# The CLI shell: app, __main__, render
# --------------------------------------------------------------------------------------------


def test_cli_exposes_doctor() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output


def test_module_entry_point_matches_the_console_script(tmp_path: Path) -> None:
    # The offline provider is named in the environment because FR-27 left no default to fall
    # into: an unconfigured subprocess now reports that, correctly, instead of running.
    # A home and a cwd of its own: the operator's `.harnessed/` ledger and `agents.toml` (a failed
    # probe, an unproven agent) must not decide whether the product's entry point works.
    home = tmp_path / ".harnessed"
    home.mkdir()
    env = {**os.environ, "HARNESSED_PROVIDER": OFFLINE_PROVIDER, "HARNESSED_HOME": str(home)}
    completed = subprocess.run(
        [sys.executable, "-m", "harnessed", "doctor", "--offline", "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ok"] is True


def test_render_is_the_only_module_that_prints() -> None:
    offenders = []
    for path in (REPO_ROOT / "src" / "harnessed").rglob("*.py"):
        if path.name == "render.py":
            continue
        if re.search(r"^\s*print\(", path.read_text(encoding="utf-8"), re.MULTILINE):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_render_error_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    render.error(ConfigError("bad setting"))
    captured = capsys.readouterr()
    assert "bad setting" in captured.err
    assert captured.out == ""


def test_render_checks_names_every_check(capsys: pytest.CaptureFixture[str]) -> None:
    render.checks(
        (
            doctor_cmd.Check("python.version", "ok", "3.12.6"),
            doctor_cmd.Check("database.wal", "skip", "memory/db.py lands with FR-10"),
        )
    )
    out = capsys.readouterr().out
    assert "python.version" in out
    assert "database.wal" in out


def test_settings_repr_never_leaks_a_key(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", PLACEHOLDER_VALUE)
    settings = load()
    assert PLACEHOLDER_VALUE not in repr(settings)


def test_settings_is_a_slots_dataclass() -> None:
    assert Settings.__slots__
