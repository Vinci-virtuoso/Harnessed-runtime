"""FR-29: attaching a coding agent is a guided config step, and doctor reports the ladder.

Rung 2 of the onboarding ladder. Ticket 021 built the delegation machinery; this file asserts
that *reaching* it is ``harnessed agents add <name>`` rather than hand-authoring TOML from a
document, and that ``harnessed doctor`` says which rungs are live without punishing an operator
for a rung they have simply not climbed yet.

Three properties carry the requirement.

* **Templates are data.** Every agent-specific fact lives in ``acp/config.py``'s template table,
  the committed ``agents.toml.example`` is rendered from that same table, and a scan over
  ``src/`` proves no other module names an agent. Adding a fourth agent is a table row.
* **Both onboarding commands are idempotent.** A second ``agents add`` reports the entry that is
  already there and leaves the file byte-identical - no duplicate table, no reordering.
* **An unreached rung is not a failure.** ``doctor`` exits zero with rung 2 reported as not yet,
  and non-zero only when a rung the operator did configure is broken.

No agent is ever named literally in this file. Every parametrisation iterates the template
table, so ``tests/test_acp.py``'s scan of ``tests/`` stays green and a table edit cannot leave a
stale literal behind here. The one exception is ``PROVIDER_NAME_COLLISIONS``, which has to spell
the one template name that collides with a pre-existing word elsewhere in the package (ticket
159) in order to name the exemption at all.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessed.acp import PROTOCOL_VERSION
from harnessed.acp import config as acp_config
from harnessed.cli import doctor_cmd
from harnessed.cli.app import app, main
from harnessed.config import load
from harnessed.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_acp_agent.py"

#: The one module allowed to name a coding agent, as a path relative to the package root.
TEMPLATE_MODULE = "acp/config.py"

#: How many agents the shipped table declares. Pinned as a number rather than as names so this
#: file never spells one; a row added or removed has to be a deliberate edit here too.
SHIPPED_TEMPLATE_COUNT = 7

#: One template's name is also, and older than, an LLM provider's name (``config.py``'s provider
#: list, ``providers/quirks.py``'s per-provider tables) - ticket 159 is the first to reuse it for a
#: coding-agent template (decision 62). Same shape as ticket 133's "prices refresh" and ticket
#: 157's bare "oauth": a named, bounded exemption for a word that legitimately means something
#: else in a file that predates the collision, never a deleted protection - every other module,
#: present or future, still may not spell a shipped agent's name.
PROVIDER_NAME_COLLISIONS: dict[str, tuple[str, ...]] = {
    "deepseek": ("config.py", "providers/quirks.py"),
}

runner = CliRunner()
#: A second runner that keeps the two streams apart, for the ``--json`` assertions.
split = CliRunner(mix_stderr=False)

TEMPLATE_NAMES = sorted(acp_config.TEMPLATES)


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

    Every failure assertion goes through here rather than through ``CliRunner``: the runner
    invokes the Typer app directly, so an escaping ``HarnessError`` is recorded on the result
    object instead of being rendered, and the error contract is exactly what is under test.
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


@pytest.fixture
def empty_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty ``PATH``, so ``shutil.which`` deterministically finds nothing.

    One shipped template's executable is installed on the machine this was written on. Without
    this, the "not installed" assertions would pass here and fail on the next machine - the same
    accident as letting a stray file in the working directory decide a result.
    """
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return empty


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


# ---------------------------------------------------------------------------
# 1. The template table is data
# ---------------------------------------------------------------------------


def test_the_table_ships_every_confirmed_template() -> None:
    """Seven templates ship, four ACP and three ``cli``, and the table is where they live."""
    assert len(acp_config.TEMPLATES) == SHIPPED_TEMPLATE_COUNT
    for name, template in acp_config.TEMPLATES.items():
        assert template.name == name
        assert template.command
        assert template.install


def test_a_template_is_looked_up_by_name_and_an_unknown_one_lists_the_known() -> None:
    for name in TEMPLATE_NAMES:
        assert acp_config.template_named(name).name == name
    with pytest.raises(ConfigError) as caught:
        acp_config.template_named("no-such-agent")
    message = str(caught.value)
    assert "no-such-agent" in message
    for name in TEMPLATE_NAMES:
        assert name in message


def test_the_committed_example_is_rendered_from_the_same_table() -> None:
    """The two cannot drift, because the committed file *is* the renderer's output."""
    body = (REPO_ROOT / acp_config.EXAMPLE_PATH).read_text(encoding="utf-8")
    assert body == acp_config.render_example()


def test_the_committed_example_still_loads_through_the_real_loader() -> None:
    """FR-100 AC 1: every template round-trips with kind, billing and a non-empty use_for."""
    agents = acp_config.load_agents(REPO_ROOT / acp_config.EXAMPLE_PATH)
    assert sorted(agents) == TEMPLATE_NAMES
    for name, spec in agents.items():
        assert spec == acp_config.TEMPLATES[name].spec()
        assert spec.kind in acp_config.KINDS
        assert spec.billing is None or spec.billing in acp_config.BILLINGS
        assert spec.use_for, f"{name} declares no use_for"


def test_the_oauth_templates_are_acp_and_the_openrouter_ones_declare_base_url_var() -> None:
    """Decision 62's groupings, by shape rather than by name (this file names no agent).

    Three ``acp`` agents billed ``oauth``, three agents billed ``openrouter`` each declaring
    ``base_url_var``/``key_var`` and decision 70's "unverified until installed" caveat, and one
    template left with no billing at all - the natural fixture for ``doctor``'s own warning.
    """
    specs = [template.spec() for template in acp_config.TEMPLATES.values()]
    oauth = [spec for spec in specs if spec.billing == "oauth"]
    openrouter = [spec for spec in specs if spec.billing == "openrouter"]
    unbilled = [spec for spec in specs if spec.billing is None]
    assert len(oauth) == 3
    assert all(spec.kind == "acp" for spec in oauth)
    assert len(openrouter) == 3
    for spec in openrouter:
        assert spec.base_url_var
        assert spec.key_var
        assert "unverified until installed" in spec.use_for
    assert len(unbilled) == 1


# ---------------------------------------------------------------------------
# 2. agents add
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_agents_add_writes_an_entry_that_round_trips_through_the_loader(
    name: str, tmp_path: Path
) -> None:
    """The first acceptance criterion: what is written is what the loader reads back."""
    target = tmp_path / "agents.toml"
    result = runner.invoke(app, ["agents", "add", name, "--agents", str(target)])
    assert result.exit_code == 0, result.output
    assert name in result.output

    agents = acp_config.load_agents(target)
    assert list(agents) == [name]
    assert agents[name] == acp_config.TEMPLATES[name].spec()


def test_agents_add_appends_rather_than_replacing_what_is_already_declared(
    tmp_path: Path,
) -> None:
    target = tmp_path / "agents.toml"
    for name in TEMPLATE_NAMES:
        result = runner.invoke(app, ["agents", "add", name, "--agents", str(target)])
        assert result.exit_code == 0, result.output
    assert sorted(acp_config.load_agents(target)) == TEMPLATE_NAMES


def test_adding_the_same_agent_twice_reports_it_and_changes_not_one_byte(tmp_path: Path) -> None:
    """The second acceptance criterion. Byte equality, so a reorder counts as a change."""
    target = tmp_path / "agents.toml"
    name = TEMPLATE_NAMES[0]
    runner.invoke(app, ["agents", "add", name, "--agents", str(target)])
    before = target.read_bytes()

    result = runner.invoke(app, ["agents", "add", name, "--agents", str(target)])
    assert result.exit_code == 0, result.output
    assert "already" in result.output
    assert target.read_bytes() == before


def test_adding_a_second_agent_leaves_the_first_entry_untouched(tmp_path: Path) -> None:
    """Idempotence is about the whole file, not only the table being added."""
    target = tmp_path / "agents.toml"
    first, second = TEMPLATE_NAMES[0], TEMPLATE_NAMES[1]
    runner.invoke(app, ["agents", "add", first, "--agents", str(target)])
    before = target.read_text(encoding="utf-8")
    runner.invoke(app, ["agents", "add", second, "--agents", str(target)])
    assert target.read_text(encoding="utf-8").startswith(before)


def test_agents_add_with_no_name_lists_what_can_be_added(tmp_path: Path) -> None:
    """``harnessed init`` names this command with no argument, so it must answer helpfully."""
    target = tmp_path / "agents.toml"
    result = runner.invoke(app, ["agents", "add", "--agents", str(target)])
    assert result.exit_code == 0, result.output
    for name in TEMPLATE_NAMES:
        assert name in result.output
    assert not target.exists()


def test_agents_add_names_the_template_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = shell(
        ["agents", "add", "no-such-agent", "--agents", str(tmp_path / "agents.toml")],
        monkeypatch,
        capsys,
    )
    assert seen.code != 0
    assert "no-such-agent" in seen.err
    assert "Traceback" not in seen.err


def test_agents_add_json_writes_exactly_one_object(tmp_path: Path) -> None:
    target = tmp_path / "agents.toml"
    name = TEMPLATE_NAMES[0]
    payload = one_object(
        split.invoke(app, ["agents", "add", name, "--agents", str(target), "--json"]).stdout
    )
    assert payload["agent"] == name
    assert payload["added"] is True
    again = one_object(
        split.invoke(app, ["agents", "add", name, "--agents", str(target), "--json"]).stdout
    )
    assert again["added"] is False


# ---------------------------------------------------------------------------
# 3. agents test
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_agents_test_against_the_stub_reports_the_negotiated_protocol_version(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    """The third acceptance criterion, against a real handshake on a real transport."""
    target = stub_agents(tmp_path)
    payload = one_object(
        split.invoke(app, ["agents", "test", "probe", "--agents", str(target), "--json"]).stdout
    )
    assert payload["agent"] == "probe"
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["status"] == "succeeded"


@pytest.mark.slow
def test_agents_test_prints_the_negotiated_version_for_a_human_too(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    target = stub_agents(tmp_path)
    result = runner.invoke(app, ["agents", "test", "probe", "--agents", str(target)])
    assert result.exit_code == 0, result.output
    assert str(PROTOCOL_VERSION) in result.output


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_agents_test_on_an_uninstalled_template_prints_the_install_command(
    name: str,
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    empty_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fourth acceptance criterion: the exact install command, and no spawn traceback."""
    target = tmp_path / "agents.toml"
    runner.invoke(app, ["agents", "add", name, "--agents", str(target)])

    seen = shell(["agents", "test", name, "--agents", str(target)], monkeypatch, capsys)
    assert seen.code != 0
    assert acp_config.TEMPLATES[name].install in seen.err
    assert "Traceback" not in seen.err


# ---------------------------------------------------------------------------
# 4. doctor reports the ladder
# ---------------------------------------------------------------------------


def test_doctor_reports_every_rung_with_the_command_that_advances_it(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rungs = doctor_cmd.run_rungs(load())
    assert [rung.number for rung in rungs] == [1, 2, 3, 4, 5]
    assert rungs[0].state == "live"
    assert rungs[1].state == "not yet"
    assert "agents add" in rungs[1].detail
    assert rungs[3].state in {"live", "not yet", "broken"}, "rung 4 is a check (ticket 125)"


def test_doctor_exits_zero_when_the_operator_has_not_reached_rung_two(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreached rung is not a failure. A doctor that fails on one trains people to ignore it."""
    monkeypatch.chdir(tmp_path)
    result = split.invoke(app, ["doctor", "--offline"])
    assert result.exit_code == 0, result.stderr
    assert "rung 2" in result.stdout
    assert "not yet" in result.stdout
    assert "agents add" in result.stdout


def test_doctor_exits_non_zero_when_a_configured_agent_is_not_installed(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    empty_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rung the operator did configure is broken, which is the one case that fails."""
    target = tmp_path / "agents.toml"
    runner.invoke(app, ["agents", "add", TEMPLATE_NAMES[0], "--agents", str(target)])
    monkeypatch.chdir(tmp_path)

    rungs = {rung.number: rung for rung in doctor_cmd.run_rungs(load())}
    assert rungs[2].state == "broken"
    assert split.invoke(app, ["doctor", "--offline"]).exit_code != 0


def test_doctor_reports_rung_two_declared_when_the_declared_agent_is_installed(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A found executable proves a file exists, not that it speaks ACP (ticket 088)."""
    stub_agents(tmp_path)
    monkeypatch.chdir(tmp_path)
    rungs = {rung.number: rung for rung in doctor_cmd.run_rungs(load())}
    assert rungs[2].state == "declared"
    assert "harnessed agents test probe" in rungs[2].detail
    assert split.invoke(app, ["doctor", "--offline"]).exit_code == 0


def test_agents_test_on_an_agent_that_dies_quotes_what_it_said(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``Connection closed`` alone was the whole symptom once. The child's own words come back."""
    script = "import sys\nprint('the adapter could not start', file=sys.stderr)\nsys.exit(1)\n"
    target = tmp_path / "agents.toml"
    target.write_text(
        "[agents.probe]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps(['-c', script])}\n"
        "timeout_s = 20.0\n",
        encoding="utf-8",
    )
    seen = shell(["agents", "test", "probe", "--agents", str(target)], monkeypatch, capsys)
    assert seen.code != 0
    assert "the adapter could not start" in seen.err
    assert "Traceback" not in seen.err


def test_agents_test_prints_the_agents_own_error_and_doctor_shows_it(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ticket 229: the refusal's code, message and data, on the command and on rung 2."""
    monkeypatch.setenv("HARNESSED_STUB_MODE", "quota_error")
    target = stub_agents(tmp_path)
    seen = shell(["agents", "test", "probe", "--agents", str(target)], monkeypatch, capsys)
    detail = '-32000 Usage limit reached ({"retry_after":"2026-10-04T00:00:00Z"})'
    assert seen.code != 0
    assert f"the agent 'probe' failed: {detail}" in seen.err
    rung = doctor_cmd.run_rungs(load(), agents_path=target)[1]
    assert detail in rung.detail


def test_agents_test_names_a_quota_refusal_for_the_recorded_template(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ticket 194: the recorded refusal, replayed, reads as a refusal for quota."""
    name = next(t.name for t in acp_config.TEMPLATES.values() if t.quota_markers)
    monkeypatch.setenv("HARNESSED_STUB_MODE", "grok_quota")
    target = stub_agents(tmp_path)
    target.write_text(
        target.read_text("utf-8").replace("[agents.probe]", f"[agents.{name}]"), encoding="utf-8"
    )
    seen = shell(["agents", "test", name, "--agents", str(target)], monkeypatch, capsys)
    assert seen.code != 0
    assert f"the agent '{name}' refused for quota" in seen.err
    rung = doctor_cmd.run_rungs(load(), agents_path=target)[1]
    assert "refused for quota" in rung.detail


def test_doctor_reports_a_malformed_registry_as_broken_rather_than_dying(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rung configured wrongly is broken. It must not take the whole report down with it."""
    (tmp_path / "agents.toml").write_text("[agents.broken]\nargs = []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rungs = {rung.number: rung for rung in doctor_cmd.run_rungs(load())}
    assert rungs[2].state == "broken"
    assert "command" in rungs[2].detail


def test_doctor_reports_rung_one_as_not_yet_when_no_provider_is_configured(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unconfigured is not the same as broken: init is the command that advances it."""
    monkeypatch.delenv("HARNESSED_PROVIDER", raising=False)
    monkeypatch.chdir(tmp_path)
    rungs = {rung.number: rung for rung in doctor_cmd.run_rungs(load())}
    assert rungs[1].state == "not yet"
    assert "init" in rungs[1].detail


def test_doctor_json_carries_the_ladder_beside_the_checks(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    payload = one_object(split.invoke(app, ["doctor", "--offline", "--json"]).stdout)
    ladder = payload["rungs"]
    assert isinstance(ladder, list)
    assert [rung["number"] for rung in ladder] == [1, 2, 3, 4, 5]
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# 5. No agent is named outside the template table
# ---------------------------------------------------------------------------


def vocabulary() -> tuple[str, ...]:
    """Every token that can only mean one shipped agent, taken from the table itself.

    Derived rather than spelled, so a table edit moves the scan with it and this file never
    becomes an offender in ``tests/test_acp.py``'s own scan.

    A template's name, plus every command or argument that carries that name - the published
    package, the adapter binary. The rest of the argv is deliberately ignored: a runner and a
    protocol acronym are shared vocabulary, and scanning for them would report the package that
    speaks the protocol as a package that hardcodes an agent.
    """
    tokens: set[str] = set()
    for template in acp_config.TEMPLATES.values():
        tokens.add(template.name)
        tokens.update(
            part
            for part in (template.command, *template.args)
            if template.name.casefold() in part.casefold()
        )
    return tuple(sorted(tokens))


def name_offenders(paths: Iterable[Path], root: Path) -> list[str]:
    """Every scanned file naming a shipped agent, relative to ``root``.

    Matching is on word boundaries rather than on containment: one shipped name is two letters
    long and a substring of half the standard library, so ``in`` would report every module that
    mentions a mapping or a pipeline. ``PROVIDER_NAME_COLLISIONS`` excuses the handful of files
    that already spelled a token for an unrelated reason before this table claimed it.
    """
    patterns = [
        (token, re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])")) for token in vocabulary()
    ]
    found = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        body = path.read_text(encoding="utf-8")
        for token, pattern in patterns:
            if rel in PROVIDER_NAME_COLLISIONS.get(token, ()):
                continue
            if pattern.search(body):
                found.append(rel)
                break
    return sorted(found)


def test_no_module_outside_the_template_table_names_a_shipped_agent() -> None:
    """The rule the architecture states as a MUST: adding an agent is data, not a code change."""
    scanned = (
        path
        for path in PACKAGE_ROOT.rglob("*.py")
        if path.relative_to(PACKAGE_ROOT).as_posix() != TEMPLATE_MODULE
    )
    assert name_offenders(scanned, PACKAGE_ROOT) == []


def test_the_scan_finds_the_table_itself() -> None:
    """A guard on the guard: a scan that found nothing anywhere would prove nothing."""
    module = PACKAGE_ROOT / TEMPLATE_MODULE
    assert name_offenders([module], PACKAGE_ROOT) == [TEMPLATE_MODULE]


def test_the_scan_catches_a_synthetic_offender(tmp_path: Path) -> None:
    innocent = tmp_path / "swappable.py"
    innocent.write_text("COMMAND = spec.command\n", encoding="utf-8")
    offender = tmp_path / "hardcoded.py"
    offender.write_text(f'AGENT = "{vocabulary()[0]}"\n', encoding="utf-8")
    assert name_offenders([innocent, offender], tmp_path) == ["hardcoded.py"]


def test_the_scan_does_not_report_a_word_that_merely_contains_a_name(tmp_path: Path) -> None:
    """One shipped name is two letters long, so containment would report half the package."""
    innocent = tmp_path / "typing_heavy.py"
    innocent.write_text(
        "from typing import Mapping\n\n\n"
        "def pipeline(mapping: Mapping[str, str]) -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )
    assert name_offenders([innocent], tmp_path) == []


def test_agents_add_writes_the_launch_settings_for_the_template_that_has_them(
    tmp_path: Path,
) -> None:
    """Ticket 213: ``session_meta`` and ``tool_names`` reach the registry, not just the table."""
    name = next(n for n, t in acp_config.TEMPLATES.items() if t.session_meta)
    target = tmp_path / "agents.toml"
    result = CliRunner().invoke(app, ["agents", "add", name, "--agents", str(target)])
    assert result.exit_code == 0, result.output
    written = acp_config.load_agents(target)[name]
    template = acp_config.TEMPLATES[name].spec()
    assert written.session_meta == template.session_meta
    assert written.tool_names == template.tool_names


# ---------------------------------------------------------------------------
# Ticket 215: agents add --refresh
# ---------------------------------------------------------------------------

_META = next(n for n, t in acp_config.TEMPLATES.items() if t.session_meta)
_PLAIN = next(n for n, t in acp_config.TEMPLATES.items() if not t.session_meta and t.kind == "acp")
_OWNED = acp_config.render_template_fields(acp_config.TEMPLATES[_META])


def _thin_file(tmp_path: Path) -> Path:
    target = tmp_path / "agents.toml"
    target.write_text(
        '# mine\n[agents.other]\ncommand = "x"\n\n'
        f'[agents.{_META}]\ncommand = "npx"\nuse_for = "mine"\n\n'
        '[agents.last]\ncommand = "y"\n',
        encoding="utf-8",
    )
    return target


def test_refresh_adds_the_template_lines_and_changes_no_other_byte(tmp_path: Path) -> None:
    target = _thin_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    result = runner.invoke(app, ["agents", "add", _META, "--refresh", "--agents", str(target)])
    assert result.exit_code == 0, result.output
    assert f"{target}: refreshed {_META} (session_meta, tool_names)" in result.output
    after = target.read_text(encoding="utf-8")
    assert "\n".join(line for line in after.split("\n") if line not in _OWNED) == original
    assert all(line in after.split("\n") for line in _OWNED)
    spec = acp_config.load_agents(target)[_META]
    template = acp_config.TEMPLATES[_META].spec()
    assert spec.session_meta == template.session_meta
    assert spec.tool_names == template.tool_names
    assert spec.command == "npx"


def test_refresh_is_a_no_op_when_the_values_already_match(tmp_path: Path) -> None:
    target = _thin_file(tmp_path)
    runner.invoke(app, ["agents", "add", _META, "--refresh", "--agents", str(target)])
    before = target.read_bytes()
    result = runner.invoke(app, ["agents", "add", _META, "--refresh", "--agents", str(target)])
    assert result.exit_code == 0, result.output
    assert "nothing written" in result.output
    assert target.read_bytes() == before


def test_refresh_replaces_different_values_in_place(tmp_path: Path) -> None:
    target = tmp_path / "agents.toml"
    target.write_text(
        f'[agents.{_META}]\ncommand = "npx"\nsession_meta = {{ old = 1 }}\n'
        'tool_names = { execute = ["Old"] }\nuse_for = "mine"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["agents", "add", _META, "--refresh", "--agents", str(target)])
    assert result.exit_code == 0, result.output
    text = target.read_text(encoding="utf-8")
    assert "old = 1" not in text and "Old" not in text
    assert text.index(_OWNED[0]) < text.index('use_for = "mine"')
    assert acp_config.load_agents(target)[_META].session_meta == (
        acp_config.TEMPLATES[_META].spec().session_meta
    )


def test_refresh_with_no_template_fields_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "agents.toml"
    runner.invoke(app, ["agents", "add", _PLAIN, "--agents", str(target)])
    before = target.read_bytes()
    result = runner.invoke(app, ["agents", "add", _PLAIN, "--refresh", "--agents", str(target)])
    assert result.exit_code == 0, result.output
    assert "nothing to refresh" in result.output
    assert target.read_bytes() == before


def test_refresh_for_an_undeclared_name_appends_like_add(tmp_path: Path) -> None:
    target = _thin_file(tmp_path)
    result = runner.invoke(app, ["agents", "add", _PLAIN, "--refresh", "--agents", str(target)])
    assert result.exit_code == 0, result.output
    assert f"added {_PLAIN}" in result.output
    assert _PLAIN in acp_config.load_agents(target)


def test_refresh_json_carries_agent_refreshed_and_path(tmp_path: Path) -> None:
    target = _thin_file(tmp_path)
    result = runner.invoke(
        app, ["agents", "add", _META, "--refresh", "--json", "--agents", str(target)]
    )
    assert json.loads(result.output) == {
        "agent": _META,
        "refreshed": ["session_meta", "tool_names"],
        "path": str(target),
    }
    again = runner.invoke(
        app, ["agents", "add", _META, "--refresh", "--json", "--agents", str(target)]
    )
    assert json.loads(again.output)["refreshed"] == []


def test_refresh_without_a_name_is_an_error_naming_the_argument(tmp_path: Path) -> None:
    result = runner.invoke(app, ["agents", "add", "--refresh", "--agents", str(tmp_path / "a")])
    assert result.exit_code != 0
    assert "NAME" in str(result.exception)
