"""FR-24: the ``harnessed`` command surface, and the proof that it stays a render layer.

The CLI is the composition root and nothing else. Every command here parses its arguments,
builds the objects the subsystem needs, calls one callable in that subsystem and hands what
comes back to :mod:`harnessed.cli.render`. That thinness is not a style preference: ticket 030
asserts that the four-command proof run in-process and the same four commands run in a shell
produce identical numbers, and business logic inside a handler is the one thing that could make
those two disagree. So the assertions here come in two kinds.

* **Behaviour.** Every group exists, every command exits 0 on success and non-zero with a reason
  on stderr on failure, ``--json`` writes exactly one object, an unknown subcommand is a usage
  message rather than a traceback, and ``python -m harnessed`` is the console script.
* **Structure.** An AST scan asserts that ``cli/render.py`` is the only module in the package
  that prints, and a second one asserts that no ``doctor`` check reports a hard-coded status.
  The second exists because between tickets 001 and this one three of doctor's six checks were
  static strings claiming that migrations, the database and the fixture corpus had not landed -
  long after they had. A check that cannot fail is not a check, and a check that cannot notice
  its subsystem arriving is worse: it is stage 6 of the verify harness reporting nothing.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from harnessed import config
from harnessed.cli import app as app_module
from harnessed.cli import doctor_cmd, improve_cmd, memory_cmd
from harnessed.cli.app import app, main
from harnessed.cli.run_cmd import Session, open_session
from harnessed.evals.scoring import MINIMUM_LIVE_SAMPLES, strictly_greater
from harnessed.events.bus import EventBus
from harnessed.improve import promoter, steps
from harnessed.memory import db, episodic, procedural, semantic
from harnessed.providers import openai_compat
from harnessed.providers.fake import FakeProvider

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "028"
STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_acp_agent.py"

#: Every v1 command group FR-24 names, spelled as it is typed.
V1_GROUPS = ("run", "eval", "improve", "memory", "agents", "gate")

#: The candidate the committed ``_reflect`` corpus writes, and the two traits
#: ``smoke-003-json-only``'s ``with_procedure`` variant keys on. Quoted from the corpus rather
#: than paraphrased: if a fixture edit moves either trait, this file must break loudly.
AUTHORED = (
    "When the task asks for structured data, respond with a single JSON object and no prose "
    "before or after it."
)

runner = CliRunner()
#: A second runner that keeps the two streams apart, for the assertions about what lands on
#: stdout and what lands on stderr. The default one mixes them, which is what most tests want.
split = CliRunner(mix_stderr=False)


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against the throwaway home, opened the way the CLI opens it."""
    connection = db.connect()
    yield connection
    connection.close()


def stub_agents(path: Path) -> Path:
    """An ``agents.toml`` whose one agent is the ACP stub, spawned through this interpreter."""
    target = path / "agents.toml"
    target.write_text(
        "[agents.probe]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = [{json.dumps(str(STUB_PATH))}]\n"
        "timeout_s = 20.0\n",
        encoding="utf-8",
    )
    return target


@dataclass(frozen=True)
class Shell:
    """What an operator saw: the exit code, stdout and stderr, kept apart."""

    code: int
    out: str
    err: str


def shell(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Shell:
    """Run one command through ``main`` - argv and all - and report what it wrote.

    Every failure assertion goes through here rather than through ``CliRunner``. The runner
    invokes the Typer app directly, so an escaping ``HarnessError`` is recorded on the result
    object and never rendered; ``main`` is the module that owns the error contract, and it is
    the thing an operator actually runs.
    """
    monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_info:
        code = 0 if exit_info.code is None else int(exit_info.code)
    captured = capsys.readouterr()
    return Shell(code=code, out=captured.out, err=captured.err)


def one_object(stdout: str) -> dict[str, object]:
    """Parse ``--json`` output, asserting it is exactly one object and nothing else."""
    text = stdout.strip()
    assert text.count("\n") == 0, f"--json wrote more than one line:\n{stdout}"
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


# ---------------------------------------------------------------------------
# 1. The command surface
# ---------------------------------------------------------------------------


def test_help_exits_zero_and_lists_every_v1_command_group() -> None:
    """The first acceptance criterion, and the one an operator meets first."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for group in V1_GROUPS:
        assert group in result.output, f"{group} is missing from the top-level help"
    capture("cli-help.txt", result.output)


@pytest.mark.parametrize(
    ("group", "commands"),
    [("eval", ("run",)), ("improve", ("run", "promote")), ("agents", ("test",))],
)
def test_each_group_keeps_its_subcommands(group: str, commands: tuple[str, ...]) -> None:
    """A Typer group with one command collapses into a bare command unless a callback pins it."""
    result = runner.invoke(app, [group, "--help"])
    assert result.exit_code == 0, result.output
    for command in commands:
        assert command in result.output


def test_an_unknown_subcommand_exits_non_zero_with_usage_and_no_traceback() -> None:
    result = split.invoke(app, ["improvise"])
    assert result.exit_code != 0
    assert "Usage" in result.stderr
    assert "Traceback" not in result.stderr
    capture(
        "cli-unknown-subcommand.txt",
        f"$ harnessed improvise\nexit code: {result.exit_code}\n\n{result.stderr}",
    )


def test_an_unknown_subcommand_of_a_group_is_a_usage_message_too() -> None:
    result = split.invoke(app, ["eval", "improvise"])
    assert result.exit_code != 0
    assert "Usage" in result.stderr


def test_python_m_harnessed_resolves_to_the_console_script_entry_point() -> None:
    """Same function object, and the same string in ``pyproject.toml``'s script table."""
    import harnessed.__main__ as module_entry

    assert module_entry.main is main
    assert 'harnessed = "harnessed.cli.app:main"' in (REPO_ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )


@pytest.mark.slow
def test_python_m_harnessed_runs_as_a_process(tmp_path: Path) -> None:
    # Its own cwd and home: the operator's `.env` and `.harnessed/` must not decide `--help`.
    home = tmp_path / ".harnessed"
    home.mkdir()
    completed = subprocess.run(
        [sys.executable, "-m", "harnessed", "--help"],
        cwd=tmp_path,
        env={**os.environ, "HARNESSED_HOME": str(home)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stderr
    for group in V1_GROUPS:
        assert group in completed.stdout


# ---------------------------------------------------------------------------
# 2. The error contract
# ---------------------------------------------------------------------------


def test_a_harness_error_is_rendered_to_stderr_with_exit_code_one(
    clean_env: None,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main`` is the one place that catches broadly, and it catches ``HarnessError`` only."""
    monkeypatch.setattr(sys, "argv", ["harnessed", "eval", "run", "improvise"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    captured = capsys.readouterr()
    assert exit_info.value.code == 1
    assert "no suite named 'improvise'" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_any_other_exception_propagates_as_a_traceback(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception is a bug report and is never dressed up as a friendly message."""

    def boom(settings: object) -> tuple[object, ...]:
        raise RuntimeError("the disk caught fire")

    monkeypatch.setattr(doctor_cmd, "run_checks", boom)
    monkeypatch.setattr(sys, "argv", ["harnessed", "doctor"])
    with pytest.raises(RuntimeError, match="the disk caught fire"):
        main()


def test_the_app_never_replaces_a_traceback_with_a_panel() -> None:
    """Typer's pretty exception hook would swallow the traceback the contract above promises."""
    assert app_module.app.pretty_exceptions_enable is False


# ---------------------------------------------------------------------------
# 3. eval run
# ---------------------------------------------------------------------------


def test_eval_run_scores_a_suite_and_exits_zero(clean_env: None, tmp_harnessed_home: Path) -> None:
    result = runner.invoke(app, ["eval", "run", "smoke", "--fresh"])
    assert result.exit_code == 0, result.output
    assert "smoke" in result.output


def test_eval_run_json_writes_exactly_one_object(clean_env: None, tmp_harnessed_home: Path) -> None:
    result = split.invoke(app, ["eval", "run", "smoke", "--fresh", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = one_object(result.stdout)
    assert payload["suite"] == "smoke"
    assert payload["case_count"] == len(list(payload["cases"]))
    assert isinstance(payload["score"], float)
    assert payload["fresh"] is True


def test_eval_run_scores_under_the_active_procedures(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """The load-bearing wiring: without it the second command of the FR-25 proof cannot move.

    The score is compared through ``scoring.strictly_greater`` rather than with ``>``, because
    the epsilon rule lives in one place and a test is not an exception to that.
    """
    before = one_object(split.invoke(app, ["eval", "run", "smoke", "--fresh", "--json"]).stdout)

    procedure = procedural.put(conn, AUTHORED, source="test")
    procedural.activate(conn, procedure.proc_id, cand_id=None)

    result = split.invoke(app, ["eval", "run", "smoke", "--fresh", "--json"])
    assert result.exit_code == 0, result.stderr
    after = one_object(result.stdout)
    assert strictly_greater(float(after["score"]), float(before["score"]))


def test_eval_run_names_the_suite_that_does_not_exist(
    clean_env: None,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = shell(["eval", "run", "improvise"], monkeypatch, capsys)
    assert seen.code != 0
    assert "improvise" in seen.err


# ---------------------------------------------------------------------------
# 4. gate
# ---------------------------------------------------------------------------


def test_gate_exits_non_zero_when_the_deterministic_evals_are_not_at_one_hundred_percent(
    clean_env: None,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = shell(["gate"], monkeypatch, capsys)
    assert seen.code != 0
    assert "not at 100%" in seen.err


def test_gate_exits_zero_when_every_case_passes(clean_env: None, conn: sqlite3.Connection) -> None:
    procedure = procedural.put(conn, AUTHORED, source="test")
    procedural.activate(conn, procedure.proc_id, cand_id=None)
    result = runner.invoke(app, ["gate"])
    assert result.exit_code == 0, result.output


def test_gate_never_resumes_the_run_eval_run_would_resume(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    """Its own label, always fresh: otherwise it replays a score taken under an older prompt."""
    runner.invoke(app, ["gate"])
    payload = one_object(split.invoke(app, ["eval", "run", "smoke", "--json"]).stdout)
    assert payload["label"] != "gate"


# ---------------------------------------------------------------------------
# 5. improve run / improve promote
# ---------------------------------------------------------------------------


def dry_run(home: Path) -> dict[str, object]:
    """The second command of the FR-25 proof, as ticket 030 will issue it."""
    result = split.invoke(app, ["improve", "run", "--dry-run", "--json"])
    assert result.exit_code == 0, result.stderr
    return one_object(result.stdout)


def test_improve_run_dry_writes_a_candidate_and_promotes_nothing(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    payload = dry_run(tmp_harnessed_home)
    assert payload["candidate"] is True
    assert payload["dry_run"] is True
    assert payload["promoted"] is False
    assert str(payload["cand_id"]).startswith("cand_")
    assert procedural.active(conn) == ()


def test_improve_promote_accepts_the_recorded_candidate(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    cand_id = str(dry_run(tmp_harnessed_home)["cand_id"])
    result = split.invoke(app, ["improve", "promote", cand_id, "--json"])
    assert result.exit_code == 0, result.stderr
    payload = one_object(result.stdout)
    assert payload["cand_id"] == cand_id
    assert payload["promoted"] is True
    assert payload["regressions"] == 0
    assert [procedure.text for procedure in procedural.active(conn)] == [AUTHORED]


def test_improve_promote_prints_the_caveat_a_human_reads(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    """Nobody can screenshot this proof and oversell it (architecture, THE CRUX)."""
    cand_id = str(dry_run(tmp_harnessed_home)["cand_id"])
    result = runner.invoke(app, ["improve", "promote", cand_id])
    assert result.exit_code == 0, result.output
    assert "provider=fake" in result.output
    assert "not model quality" in result.output


def test_improve_promote_refuses_an_id_that_is_not_the_recorded_candidate(
    clean_env: None,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dry_run(tmp_harnessed_home)
    seen = shell(["improve", "promote", "cand_deadbeef"], monkeypatch, capsys)
    assert seen.code == 1
    assert "cand_deadbeef" in seen.err


def test_improve_promote_refuses_when_no_cycle_has_run(
    clean_env: None,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = shell(["improve", "promote", "cand_deadbeef"], monkeypatch, capsys)
    assert seen.code == 1
    assert "improve run --dry-run" in seen.err


# ---------------------------------------------------------------------------
# 6. run
# ---------------------------------------------------------------------------


def test_run_answers_an_ad_hoc_prompt_from_the_committed_corpus(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    """No ``metadata["scope"]``, so FakeProvider falls back to the hash of the first message."""
    result = runner.invoke(app, ["run", "ping"])
    assert result.exit_code == 0, result.output
    assert "pong" in result.output


def test_run_binds_the_token_budget_flag(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag ticket 014 declared and left for the composition root to bind to argv."""
    calls: list[int | None] = []
    real = config.load

    def spy(*, provider: str | None = None, token_budget: int | None = None) -> config.Settings:
        calls.append(token_budget)
        return real(provider=provider, token_budget=token_budget)

    monkeypatch.setattr(config, "load", spy)
    result = runner.invoke(app, ["run", "ping", config.TOKEN_BUDGET_FLAG, "321"])
    assert result.exit_code == 0, result.output
    assert calls[0] == 321


def test_run_reports_a_prompt_the_corpus_cannot_answer(
    clean_env: None,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = shell(["run", "a prompt no fixture was ever written for"], monkeypatch, capsys)
    assert seen.code == 1
    assert seen.err.startswith("error: ")


# ---------------------------------------------------------------------------
# 7. memory
# ---------------------------------------------------------------------------


def test_memory_search_finds_a_stored_fact(clean_env: None, conn: sqlite3.Connection) -> None:
    semantic.put(conn, "the harness stays offline by default", source="test")
    result = runner.invoke(app, ["memory", "search", "offline"])
    assert result.exit_code == 0, result.output
    assert "offline by default" in result.output


def test_memory_procedures_lists_the_active_set(clean_env: None, conn: sqlite3.Connection) -> None:
    procedure = procedural.put(conn, AUTHORED, source="test")
    procedural.activate(conn, procedure.proc_id, cand_id=None)
    result = runner.invoke(app, ["memory", "procedures"])
    assert result.exit_code == 0, result.output
    assert procedure.proc_id in result.output


def test_memory_search_says_so_when_nothing_matches(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    result = runner.invoke(app, ["memory", "search", "nothing was ever written about this"])
    assert result.exit_code == 0, result.output
    assert result.output.strip()


# Ticket 042. ``semantic.put`` had no caller anywhere in ``src/``: the store, the FTS5 index and
# the search command all existed, and nothing shipped could write a fact into them. Every test
# that exercised semantic memory called the store API directly, which is exactly why the gap
# survived a suite at 97 percent - the module was covered and unreachable. These go through the
# CLI for that reason, never through ``semantic.put``.

# Split the way ``test_redaction.py`` splits its samples, so no tracked file holds a string that
# reads as a key.
SECRET_SAMPLE = "gsk" + "_" + "AAAABBBBCCCCDDDDEEEEFFFF"

# 042's artefacts go under 042, not under this file's own 028 directory. A manifest naming paths
# that nothing writes to is the defect this repo keeps rediscovering: it reads as confirmation of
# files that are not there.
EVIDENCE_042 = REPO_ROOT / "evidence" / "042"


def capture_042(name: str, text: str) -> Path:
    """Write one of ticket 042's evidence artefacts."""
    EVIDENCE_042.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_042 / name
    path.write_text(text, encoding="utf-8")
    return path


def test_memory_add_writes_a_fact_the_search_command_then_finds(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """The write and the read proved together, both through the shipped surface."""
    written = runner.invoke(app, ["memory", "add", "the promoter never asks a model anything"])
    assert written.exit_code == 0, written.output
    assert "mem_" in written.output

    found = runner.invoke(app, ["memory", "search", "promoter"])
    assert found.exit_code == 0, found.output
    assert "never asks a model anything" in found.output
    capture_042(
        "add-then-search.txt",
        f"$ harnessed memory add ...\n{written.output}\n$ harnessed memory search promoter\n"
        f"{found.output}",
    )


def test_memory_add_records_the_source_it_was_given(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    result = runner.invoke(
        app, ["memory", "add", "deep work runs four hours", "--source", "onboarding"]
    )
    assert result.exit_code == 0, result.output
    assert semantic.search(conn, "deep", limit=5)[0].source == "onboarding"


def test_memory_add_defaults_its_source_to_the_operator(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """One documented literal, so an automatic writer later is distinguishable in the store."""
    result = runner.invoke(app, ["memory", "add", "the loop has two guardrails"])
    assert result.exit_code == 0, result.output
    assert semantic.search(conn, "guardrails", limit=5)[0].source == memory_cmd.OPERATOR_SOURCE


def test_memory_add_redacts_before_it_stores(clean_env: None, conn: sqlite3.Connection) -> None:
    """FR-13. The criterion is that the CLI routes through ``put`` rather than inserting itself."""
    result = runner.invoke(app, ["memory", "add", f"the staging key is {SECRET_SAMPLE}"])
    assert result.exit_code == 0, result.output

    stored = conn.execute("SELECT text FROM mem_semantic").fetchall()
    assert len(stored) == 1
    assert SECRET_SAMPLE not in stored[0][0]
    capture_042("redacted-fact.txt", f"stored text: {stored[0][0]}\n")


def test_memory_add_refuses_an_empty_fact(
    clean_env: None,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty fact is retrievable noise that costs budget in every future assembly."""
    seen = shell(["memory", "add", "   "], monkeypatch, capsys)
    assert seen.code == 1
    assert seen.err.strip()
    assert conn.execute("SELECT COUNT(*) FROM mem_semantic").fetchone()[0] == 0


def test_memory_add_needs_no_provider(
    clean_env: None, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout configured for a live model with no key can still write down a fact."""
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    result = runner.invoke(app, ["memory", "add", "a fact needs no endpoint"])
    assert result.exit_code == 0, result.output
    capture_042("no-provider-needed.txt", f"HARNESSED_PROVIDER=groq, no key\n{result.output}")


# ---------------------------------------------------------------------------
# 8. agents test
# ---------------------------------------------------------------------------


def test_agents_test_names_the_agent_the_registry_does_not_declare(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["agents", "test", "absent", "--agents", str(stub_agents(tmp_path))]
    seen = shell(argv, monkeypatch, capsys)
    assert seen.code == 1
    assert "absent" in seen.err


def test_agents_test_reports_a_missing_registry(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["agents", "test", "probe", "--agents", str(tmp_path / "nothing.toml")]
    seen = shell(argv, monkeypatch, capsys)
    assert seen.code == 1
    assert "nothing.toml" in seen.err


@pytest.mark.slow
def test_agents_test_completes_the_handshake_against_the_stub(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    """A real child process over a real transport, the way every ACP test spawns one."""
    registry = stub_agents(tmp_path)
    result = split.invoke(app, ["agents", "test", "probe", "--agents", str(registry), "--json"])
    assert result.exit_code == 0, result.stderr
    payload = one_object(result.stdout)
    assert payload["agent"] == "probe"
    assert payload["status"] == "succeeded"

    human = runner.invoke(app, ["agents", "test", "probe", "--agents", str(registry)])
    assert human.exit_code == 0, human.output
    assert "probe" in human.output
    assert "succeeded" in human.output


# ---------------------------------------------------------------------------
# 9. The structural claims
# ---------------------------------------------------------------------------


def modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def calls_to(tree: ast.AST, name: str) -> list[ast.Call]:
    """Every call of a bare name, which is how both ``print`` and ``Check`` are spelled here."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def test_render_is_the_only_module_in_the_package_that_prints() -> None:
    """Observable moments publish events and the CLI renders them; a print elsewhere is a bug."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{call.lineno}"
        for path in modules()
        if path.name != "render.py"
        for call in calls_to(ast.parse(path.read_text(encoding="utf-8")), "print")
    ]
    assert offenders == []


def test_the_print_scan_would_notice_a_print(tmp_path: Path) -> None:
    """A guard on the guard: the scan above must be able to speak."""
    offender = tmp_path / "loud.py"
    offender.write_text("def shout() -> None:\n    print('hello')\n", encoding="utf-8")
    assert calls_to(ast.parse(offender.read_text(encoding="utf-8")), "print")


def check_calls() -> list[tuple[str, ast.Call]]:
    """Every ``Check(...)`` in ``doctor_cmd.py``, paired with the function that builds it."""
    tree = ast.parse((PACKAGE_ROOT / "cli" / "doctor_cmd.py").read_text(encoding="utf-8"))
    found: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            found.extend((node.name, call) for call in calls_to(node, "Check"))
    return found


def status_of(call: ast.Call) -> str | None:
    """The literal status a ``Check(...)`` call reports, or ``None`` when it is computed."""
    argument: ast.expr | None = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "status":
            argument = keyword.value
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return None


def test_no_doctor_check_reports_a_subsystem_as_deferred() -> None:
    """The three static skips are gone. A status of ``skip`` is not reachable any more.

    This is the assertion that stops doctor lying: a hard-coded ``skip`` reads the same on the
    day its subsystem lands as on the day it did not exist, which is exactly what happened to
    ``database.wal``, ``migrations.current`` and ``fixtures.corpus`` for twenty-odd tickets.
    """
    deferred = [name for name, call in check_calls() if status_of(call) == "skip"]
    assert deferred == []


def test_every_doctor_check_can_report_both_outcomes() -> None:
    """A check that cannot fail is not a check, and one that cannot pass is not one either."""
    builders: dict[str, set[str]] = {}
    for name, call in check_calls():
        status = status_of(call)
        if status is not None:
            builders.setdefault(name, set()).add(status)
    assert builders, "no Check(...) call was found; the scan is looking at the wrong file"
    for name, statuses in builders.items():
        assert {"ok", "fail"} <= statuses, f"{name} cannot report both outcomes: {statuses}"


def test_run_checks_delegates_every_check(clean_env: None) -> None:
    """``run_checks`` is a report order, not a place a status can be written by hand."""
    assert [name for name, _ in check_calls() if name == "run_checks"] == []


# ---------------------------------------------------------------------------
# 10. doctor, computed from disk
# ---------------------------------------------------------------------------


def test_doctor_reports_the_database_migrations_and_corpus_as_ok(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    results = {check.name: check for check in doctor_cmd.run_checks(config.load())}
    for name in ("database.wal", "migrations.current", "fixtures.corpus"):
        assert results[name].status == "ok", results[name].detail
        assert results[name].status != "skip"
    assert str(db.schema_version(db.connect())) in results["migrations.current"].detail
    assert "fake" in results["fixtures.corpus"].detail


def test_doctor_fails_when_the_fixture_corpus_is_gone(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    results = {
        check.name: check
        for check in doctor_cmd.run_checks(config.load(), corpus_root=tmp_path / "renamed-away")
    }
    assert results["fixtures.corpus"].status == "fail"
    assert "renamed-away" in results["fixtures.corpus"].detail


def test_doctor_exits_non_zero_when_the_fixture_corpus_is_gone(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_cmd, "CORPUS_ROOT", tmp_path / "renamed-away")
    result = split.invoke(app, ["doctor", "--offline"])
    assert result.exit_code != 0


def test_doctor_fails_on_a_migration_directory_that_is_not_there(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    results = {
        check.name: check
        for check in doctor_cmd.run_checks(config.load(), migrations_dir=tmp_path / "no-such-dir")
    }
    assert results["migrations.current"].status == "fail"


def test_doctor_json_still_writes_exactly_one_object(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    result = split.invoke(app, ["doctor", "--offline", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = one_object(result.stdout)
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# 11. The refusals a half-finished cycle owes an operator
# ---------------------------------------------------------------------------


def test_improve_promote_refuses_a_cycle_that_never_reflected(
    clean_env: None,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run interrupted before its reflection has no candidate, and says which run to resume."""
    improvement_run_id, _ = steps.start(conn, "smoke")
    seen = shell(["improve", "promote", "cand_deadbeef"], monkeypatch, capsys)
    assert seen.code == 1
    assert improvement_run_id in seen.err
    assert "reflection step" in seen.err


def test_improve_promote_refuses_when_the_cycle_wrote_no_candidate(
    clean_env: None,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The flywheel's success condition, one cycle later: nothing failed, so nothing was written."""
    improvement_run_id, _ = steps.start(conn, "smoke")
    steps.put(conn, improvement_run_id, steps.REFLECT, "smoke", {"cand_id": steps.NO_CANDIDATE})
    seen = shell(["improve", "promote", "cand_deadbeef"], monkeypatch, capsys)
    assert seen.code == 1
    assert "wrote no candidate" in seen.err


def test_memory_episodes_lists_what_one_run_recorded(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    # A held session: ticket 079 refuses an id no event was published under.
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    episodic.put(conn, bus.run_id, "delegation", "handed the refactor to the probe agent")
    result = runner.invoke(app, ["memory", "episodes", bus.run_id])
    assert result.exit_code == 0, result.output
    assert "handed the refactor" in result.output


def test_doctor_fails_on_a_corpus_that_will_not_load(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    """Present but malformed is its own failure, and it must not read like absent."""
    scope = tmp_path / "wire" / "fake" / "badscope"
    scope.mkdir(parents=True)
    (scope / "_index.yaml").write_text(
        "scope: badscope\nvariants:\n  - name: only\n    when: always\n", encoding="utf-8"
    )
    results = {
        check.name: check
        for check in doctor_cmd.run_checks(config.load(), corpus_root=scope.parent)
    }
    assert results["fixtures.corpus"].status == "fail"
    assert "malformed" in results["fixtures.corpus"].detail


# ---------------------------------------------------------------------------
# 10. a provider failure reads like one (ticket 040)
# ---------------------------------------------------------------------------

#: The SDK exception is reached through the adapter module rather than imported: a scan in
#: `tests/test_provider_quirks.py` keeps a vendor SDK import inside `providers/`, and it covers
#: `tests/` too.
#:
#: Obviously not a credential, so the repository write guard accepts the literal. It
#: authenticates a client whose one call is replaced before it is ever made.
NOT_A_KEY = "not-a-real-key-for-a-stubbed-endpoint-1618033988"


class RaisingCompletions:
    """The one SDK call ``OpenAICompatAdapter`` makes, replaced by the exception it would get."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def create(self, **kwargs: object) -> object:
        raise self.exc


def stub_openai_client(exc: BaseException) -> Callable[..., object]:
    """A stand-in for ``openai.OpenAI`` that builds a client refusing every request.

    The endpoint is stubbed and the configuration is left real: what this test asks is whether a
    vendor refusal reaches the operator as one rendered line, and the configuration is the half
    that must not be faked to answer it.
    """

    def build(**kwargs: object) -> object:
        return SimpleNamespace(chat=SimpleNamespace(completions=RaisingCompletions(exc)))

    return build


def test_a_vendor_status_error_is_one_line_on_stderr_with_exit_code_one(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 400 from a real endpoint is the single most likely thing to happen to a new operator.
    Before ticket 040 it arrived as a thirty-line traceback through click and typer, which the
    error contract reserves for a bug report.

    This test's stub only answers the chat vendor's client, and the router's own Decider always
    needs ``OPENROUTER_API_KEY`` (decision 57) whatever the chat provider is, which would raise
    before ever reaching the vendor error this test is about (ticket 160). Ticket 179 deleted the
    flag that used to turn routing off for exactly this; ``build_decider`` is stubbed instead, to
    a Decider whose own call always falls back (decision 59) without ever reaching the network or
    needing a credential this test does not configure.
    """
    from harnessed.cli import run_cmd
    from harnessed.errors import DecisionUnreachable

    class _AlwaysFallbackDecider:
        def decide(self, state: str, questions: object) -> object:
            raise DecisionUnreachable("stubbed: no live router in this test")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings: _AlwaysFallbackDecider())
    monkeypatch.setenv(config.PROVIDER_VAR, "groq")
    monkeypatch.setenv("GROQ_API_KEY", NOT_A_KEY)
    monkeypatch.setenv(config.MODEL_VAR, "a-model-that-was-retired")
    monkeypatch.setattr(
        openai_compat.openai,
        "OpenAI",
        stub_openai_client(
            openai_compat.openai.APIStatusError(
                "The model 'a-model-that-was-retired' does not exist",
                response=SimpleNamespace(status_code=400, request=None, headers={}),
                body=None,
            )
        ),
    )
    seen = shell(["run", "ping"], monkeypatch, capsys)
    assert seen.code == 1
    assert seen.err.startswith("error: ")
    assert "400" in seen.err
    assert "a-model-that-was-retired" in seen.err
    assert "stale" in seen.err
    assert "Traceback" not in seen.err
    assert "Traceback" not in seen.out

    directory = REPO_ROOT / "evidence" / "040"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "vendor-400-renders.txt").write_text(
        f"exit={seen.code}\nstdout:\n{seen.out}\nstderr:\n{seen.err}", encoding="utf-8"
    )


# Ticket 047. Tags, links and archiving, all through the shipped surface.


def test_memory_add_accepts_tags_and_links_that_round_trip(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    target = runner.invoke(app, ["memory", "add", "the deployment manifest lives in platform"])
    assert target.exit_code == 0, target.output
    target_id = next(word for word in target.output.split() if word.startswith("mem_"))

    written = runner.invoke(
        app,
        [
            "memory",
            "add",
            "the ingest service listens on port 8471",
            "--tag",
            "infra",
            "--tag",
            "ports",
            "--link",
            target_id,
        ],
    )
    assert written.exit_code == 0, written.output

    found = runner.invoke(app, ["memory", "search", "ingest"])
    assert found.exit_code == 0, found.output
    assert "infra" in found.output
    assert target_id in found.output


def test_memory_add_refuses_a_link_to_a_fact_that_does_not_exist(
    clean_env: None,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = shell(["memory", "add", "a fact", "--link", "mem_never_written"], monkeypatch, capsys)
    assert seen.code == 1
    assert "mem_never_written" in seen.err
    assert conn.execute("SELECT COUNT(*) FROM mem_semantic").fetchone()[0] == 0


def test_memory_archive_retires_a_fact_from_search(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    written = runner.invoke(app, ["memory", "add", "the ingest service listens on port 8471"])
    mem_id = next(word for word in written.output.split() if word.startswith("mem_"))
    assert runner.invoke(app, ["memory", "search", "ingest"]).output.count("mem_") == 1

    archived = runner.invoke(app, ["memory", "archive", mem_id])
    assert archived.exit_code == 0, archived.output
    assert mem_id in archived.output

    assert runner.invoke(app, ["memory", "search", "ingest"]).output.count("mem_") == 0
    assert semantic.get(conn, mem_id) is not None


def test_memory_archive_is_idempotent(clean_env: None, conn: sqlite3.Connection) -> None:
    written = runner.invoke(app, ["memory", "add", "a fact worth retiring"])
    mem_id = next(word for word in written.output.split() if word.startswith("mem_"))
    first = runner.invoke(app, ["memory", "archive", mem_id])
    second = runner.invoke(app, ["memory", "archive", mem_id])
    assert first.exit_code == second.exit_code == 0
    assert mem_id in second.output


def test_memory_archive_names_a_fact_that_does_not_exist(
    clean_env: None,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = shell(["memory", "archive", "mem_never_written"], monkeypatch, capsys)
    assert seen.code == 1
    assert "mem_never_written" in seen.err


# Ticket 045. An eval run that has been scored must be readable without opening SQLite.


def test_eval_list_names_the_runs_show_can_be_given(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """`show` needs a way to learn its own argument, or it is a command nobody can call."""
    scored = runner.invoke(app, ["eval", "run", "smoke", "--fresh"])
    assert scored.exit_code == 0, scored.output

    listed = runner.invoke(app, ["eval", "list"])
    assert listed.exit_code == 0, listed.output
    assert "eval_" in listed.output
    assert "smoke" in listed.output


def test_eval_show_prints_a_scored_run_and_every_case(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """The run scored an hour ago exists in the ledger; until now nothing could read it back."""
    scored = runner.invoke(app, ["eval", "run", "smoke", "--fresh", "--json"])
    eval_run_id = json.loads(scored.stdout.strip())["eval_run_id"]

    shown = runner.invoke(app, ["eval", "show", eval_run_id])
    assert shown.exit_code == 0, shown.output
    assert eval_run_id in shown.output
    assert "smoke-001-capital" in shown.output


def test_eval_show_names_a_run_that_does_not_exist(
    clean_env: None,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = shell(["eval", "show", "eval_never_scored"], monkeypatch, capsys)
    assert seen.code == 1
    assert "eval_never_scored" in seen.err


def test_eval_show_and_list_need_no_provider(
    clean_env: None, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading a score back has no business constructing a provider, like the memory group."""
    scored = runner.invoke(app, ["eval", "run", "smoke", "--fresh", "--json"])
    eval_run_id = json.loads(scored.stdout.strip())["eval_run_id"]

    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert runner.invoke(app, ["eval", "list"]).exit_code == 0
    assert runner.invoke(app, ["eval", "show", eval_run_id]).exit_code == 0


def test_eval_show_json_is_one_object(clean_env: None, conn: sqlite3.Connection) -> None:
    scored = runner.invoke(app, ["eval", "run", "smoke", "--fresh", "--json"])
    eval_run_id = json.loads(scored.stdout.strip())["eval_run_id"]
    payload = one_object(split.invoke(app, ["eval", "show", eval_run_id, "--json"]).stdout)
    assert payload["eval_run_id"] == eval_run_id
    assert payload["cases"]


# Ticket 049. `eval list` invented a pass ratio and called it the score.


def test_eval_list_reports_the_mean_the_ledger_holds_not_a_pass_ratio(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """Found by a human running the CLI: the same run read 0.3 in --json and 0.2 in `list`.

    A case can score between 0 and 1 - two graders, one passed - and the promoter counts that
    half point. ``RunSummary.score`` was ``passes / case_count``, which rounds it to nothing. The
    row is planted rather than produced, so the assertion is about what ``list`` *reads*, and a
    pass ratio cannot pass it: 1 pass in 2 cases is 0.5 either way, so the half-scoring case is
    what separates the two formulas.
    """
    from harnessed import clock

    at = clock.isoformat(clock.now())
    db.write(
        conn,
        (
            (
                "INSERT INTO eval_runs (eval_run_id, suite, label, started_at) VALUES (?, ?, ?, ?)",
                ("eval_planted", "smoke", "baseline", at),
            ),
            (
                "INSERT INTO eval_results (eval_run_id, case_id, attempt, score, passed, detail, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("eval_planted", "case-a", 1, 1.0, 1, "[]", at),
            ),
            (
                "INSERT INTO eval_results (eval_run_id, case_id, attempt, score, passed, detail, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("eval_planted", "case-b", 1, 0.5, 0, "[]", at),
            ),
        ),
    )
    # mean of (1.0, 0.5) is 0.75; the pass ratio is 1/2 = 0.5. Only one of these is the score.
    listed = one_object(split.invoke(app, ["eval", "list", "--json"]).stdout)
    planted = next(r for r in listed["runs"] if r["eval_run_id"] == "eval_planted")
    shown = one_object(split.invoke(app, ["eval", "show", "eval_planted", "--json"]).stdout)

    assert planted["score"] == 0.75, f"list says {planted['score']}; the ledger's mean is 0.75"
    assert planted["score"] == shown["score"], "list and show disagree about one run"
    assert planted["passes"] == 1 and planted["case_count"] == 2


# Ticket 050. The surface says how many draws a number is.


def test_eval_run_json_carries_the_sample_count(clean_env: None, conn: sqlite3.Connection) -> None:
    payload = one_object(split.invoke(app, ["eval", "run", "smoke", "--fresh", "--json"]).stdout)
    assert payload["samples"] == 1
    payload = one_object(
        split.invoke(app, ["eval", "run", "smoke", "--fresh", "--json", "--samples", "2"]).stdout
    )
    assert payload["samples"] == 2


def test_eval_list_and_show_carry_the_sample_count(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    run = one_object(
        split.invoke(app, ["eval", "run", "smoke", "--fresh", "--json", "--samples", "2"]).stdout
    )
    listed = next(
        r
        for r in one_object(split.invoke(app, ["eval", "list", "--json"]).stdout)["runs"]
        if r["eval_run_id"] == run["eval_run_id"]
    )
    shown = one_object(split.invoke(app, ["eval", "show", run["eval_run_id"], "--json"]).stdout)
    assert listed["samples"] == shown["samples"] == 2


# Ticket 053. The caveat is derived from the configured provider, not from a literal.


#: The provider the live arm names. Keyed, so ``_minimum_for`` demands the live minimum and both
#: arms have to draw the same number of times for their two transcripts to be comparable at all.
LIVE_PROVIDER = "deepseek"

EVIDENCE_053 = REPO_ROOT / "evidence" / "053"


def offline_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the settings the environment resolved and replace only the wire.

    The point of the pairing test is which provider name ``improve_cmd`` reads off its session,
    so the settings must be the real resolution of ``HARNESSED_PROVIDER`` - naming the provider
    from the test would assert the test's own literal. Only the built provider object is swapped,
    because a real DeepSeek client in a test is what the socket guard exists to stop.
    """

    def opened(**kwargs: object) -> Session:
        session = open_session(**kwargs)
        return replace(session, provider=FakeProvider(session.settings))

    monkeypatch.setattr(improve_cmd, "open_session", opened)


def cycle_under(provider: str, home: Path, monkeypatch: pytest.MonkeyPatch, *, samples: int) -> str:
    """One full ``improve run`` under ``provider``, in a database of its own.

    Its own home because a promotion is content-addressed over the candidate: two cycles in one
    database decide once and the second replays the first's outcome, which would print one arm's
    transcript twice and call it a comparison (ticket 052).
    """
    home.mkdir(parents=True)
    monkeypatch.setenv("HARNESSED_HOME", str(home))
    monkeypatch.setenv("HARNESSED_PROVIDER", provider)
    if config.PROVIDER_KEY_VARS[provider] is not None:
        monkeypatch.setenv(str(config.PROVIDER_KEY_VARS[provider]), "sk-not-a-real-key")
    result = runner.invoke(app, ["improve", "run", "--samples", str(samples)])
    assert result.exit_code == 0, result.output
    return result.output


def without_run_id(output: str) -> list[str]:
    """The transcript with the one line that cannot match across two runs folded away."""
    return [re.sub(r"imp_[0-9A-Z]+", "<run>", line) for line in output.splitlines() if line]


def test_the_caveat_follows_the_configured_provider(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 and AC2 in one assertion, because either alone is vacuous.

    A test that only checks "the fake run prints the caveat" passed before this ticket, when the
    argument was the constant ``promoter.FAKE``. A test that only checks "the live run does not"
    passes if ``describe`` is deleted. The pairing is the assertion: the same cycle, drawn the
    same number of times, differs by exactly the caveat line and nothing else.
    """
    offline_wire(monkeypatch)

    fake = cycle_under("fake", tmp_path / "fake", monkeypatch, samples=MINIMUM_LIVE_SAMPLES)
    live = cycle_under(LIVE_PROVIDER, tmp_path / "live", monkeypatch, samples=MINIMUM_LIVE_SAMPLES)

    assert promoter.CAVEAT in without_run_id(fake)
    assert promoter.CAVEAT not in without_run_id(live)
    assert without_run_id(live) == [
        line for line in without_run_id(fake) if line != promoter.CAVEAT
    ]
    EVIDENCE_053.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_053 / "caveat-pairing.txt").write_text(
        f"HARNESSED_PROVIDER=fake:\n{fake}\nHARNESSED_PROVIDER={LIVE_PROVIDER}:\n{live}",
        encoding="utf-8",
    )


def test_the_decision_a_live_run_prints_is_otherwise_whole(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2's second half: the caveat goes, the decision it hangs off stays."""
    offline_wire(monkeypatch)

    live = cycle_under(LIVE_PROVIDER, tmp_path / "live", monkeypatch, samples=MINIMUM_LIVE_SAMPLES)

    assert "rule: score" in live
    assert "passes: 4 -> 5" in live
    assert "soft_drops: []" in live


# Ticket 054. `eval list` counted ledger rows, `eval show` folded them: one run, two sizes.


EVIDENCE_054 = REPO_ROOT / "evidence" / "054"

#: A two-case run drawn three times, planted so that folding changes the answer. ``case-b``
#: passes two draws of three, so counting rows says 6 cases and 5 passes while the run is 2
#: cases and 1 pass. A sampled run whose draws all agree cannot tell those two readings apart.
FLAKY_RUN = "eval_flaky_054"


def plant_flaky_run(conn: sqlite3.Connection) -> None:
    from harnessed import clock

    at = clock.isoformat(clock.now())
    rows: list[tuple[str, tuple[object, ...]]] = [
        (
            "INSERT INTO eval_runs (eval_run_id, suite, label, started_at) VALUES (?, ?, ?, ?)",
            (FLAKY_RUN, "smoke", "baseline", at),
        )
    ]
    for attempt in (1, 2, 3):
        for case_id, passed in (("case-a", True), ("case-b", attempt < 3)):
            rows.append(
                (
                    "INSERT INTO eval_results (eval_run_id, case_id, attempt, score, passed, "
                    "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (FLAKY_RUN, case_id, attempt, 1.0 if passed else 0.0, int(passed), "[]", at),
                )
            )
    db.write(conn, tuple(rows))


def test_eval_list_and_show_describe_the_same_sampled_run(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """AC1 to AC3: the two surfaces of one tool, pinned to each other and to the fold.

    The assertion that matters is the equality between the surfaces, not either surface's
    literal - a test pinning only ``list`` would have to be edited by whoever next changes what
    a fold means, and would then agree with nothing. The absolute numbers are here as well
    because equality alone is satisfied by both surfaces being wrong the same way.
    """
    plant_flaky_run(conn)
    listed = one_object(split.invoke(app, ["eval", "list", "--json"]).stdout)
    runs = cast("list[dict[str, object]]", listed["runs"])
    row = next(r for r in runs if r["eval_run_id"] == FLAKY_RUN)
    shown = one_object(split.invoke(app, ["eval", "show", FLAKY_RUN, "--json"]).stdout)

    assert row["case_count"] == shown["case_count"] == 2, "6 rows, 2 cases"
    assert row["passes"] == shown["pass_count"] == 1, "5 rows passed, 1 case did"
    assert row["score"] == shown["score"]
    assert row["samples"] == shown["samples"] == 3

    text = split.invoke(app, ["eval", "list"]).stdout
    line = next(ln for ln in text.splitlines() if FLAKY_RUN in ln)
    EVIDENCE_054.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_054 / "list-vs-show.txt").write_text(
        "A 2-case run drawn 3 times; case-b passes 2 draws of 3.\n"
        "Before this ticket `eval list` counted rows: 6 cases, 5 passed.\n\n"
        f"eval list:\n{line.strip()}\n\n"
        f"eval list --json:\n{json.dumps(row, indent=2)}\n\n"
        f"eval show --json:\n{json.dumps(shown, indent=2)}\n",
        encoding="utf-8",
    )
