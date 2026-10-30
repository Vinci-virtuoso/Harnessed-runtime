"""FR-26: ``harnessed init`` takes a fresh clone to a command that runs.

Two claims are load-bearing here and neither is about the happy path.

* **The merge.** ``.env`` is an operator's file, not the harness's. Init owns a named few
  variables in it and must leave every other line - value, comment and position - byte-identical.
  A rewrite that normalised the file would silently destroy unrelated settings, which is the kind
  of bug that loses trust permanently, so the merge is asserted on the bytes rather than on a
  parsed view of them.
* **The credential never surfaces.** Not in stdout, not in stderr, not in a failure message. The
  scripted-stdin tests below assert that over the whole captured output of every path, including
  the one where validation fails.

Every test drives the command offline: the socket guard in ``conftest.py`` is autouse, so a run
that reached an endpoint would fail here as a guard violation rather than as a wrong assertion.

The two credential variable names are held in :data:`KEY_VAR` and :data:`OTHER_VAR` and
interpolated rather than spelled inside the fixture strings, so no line of this file looks like
an assignment of a credential to a literal.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessed import config
from harnessed.cli import init_cmd
from harnessed.cli.app import app, main
from harnessed.errors import ProviderError
from harnessed.providers import registry as provider_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / ".env.example"

#: The variable the chosen provider's credential lands in, and one belonging to a provider the
#: operator did not choose. Quoted from ``config.PROVIDER_KEY_VARS`` rather than retyped.
KEY_VAR = config.PROVIDER_KEY_VARS["groq"]
OTHER_VAR = config.PROVIDER_KEY_VARS["openai"]

#: The made-up value every test types at the prompt. Distinctive enough that a leak into any
#: stream is unmissable, and obviously not a real one.
TYPED = "not-a-real-value-31415926535897932384"

#: A short stand-in for the same thing in the merge unit tests, where the value is beside the
#: point and a long one would push the expected string past the line length.
SHORT = "v1"

#: The line init prints once the command it names will actually run - which, since ticket 039
#: made ``.env`` a place keys are read from, is every successful run. Quoted from the module
#: under test rather than retyped, so a re-wording moves it here and nowhere else.
TRY_IT_NOW = init_cmd.TRY_IT_NOW

runner = CliRunner()

#: One ``NAME=`` assignment at the start of a line. Init's own claim - "no other key is present" -
#: is a claim about assignments, so the test spells out what an assignment is rather than
#: borrowing the parser under test.
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)


def assigned(text: str) -> set[str]:
    """Every variable the file actually sets. A commented line sets nothing."""
    return set(ASSIGNMENT.findall(text))


@dataclass(frozen=True)
class Shell:
    """What one ``main()`` run wrote and how it exited (the ticket 028 pattern)."""

    code: int
    out: str
    err: str

    @property
    def output(self) -> str:
        return self.out + self.err


@pytest.fixture
def fresh_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> Iterator[Path]:
    """A checkout with ``.env.example`` and no ``.env``, and the process sitting in it."""
    (tmp_path / ".env.example").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    yield tmp_path


class _Stdin:
    """The smallest stdin a scripted run needs: line reads and nothing else."""

    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""

    def read(self) -> str:
        text = "".join(self._lines)
        self._lines = []
        return text

    def isatty(self) -> bool:
        return False


def shell(
    argv: list[str],
    stdin: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Shell:
    """Run ``harnessed <argv>`` through ``main`` with a scripted stdin.

    ``CliRunner`` records an escaping ``HarnessError`` on its result object instead of rendering
    it, so every assertion about stderr has to go through the entry point an operator runs.
    """
    monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
    monkeypatch.setattr(sys, "stdin", _Stdin(stdin))
    code = 0
    try:
        main()
    except SystemExit as exit_info:
        code = 0 if exit_info.code is None else int(exit_info.code)
    captured = capsys.readouterr()
    return Shell(code=code, out=captured.out, err=captured.err)


class Boom(ProviderError):
    """A refusal in the shape the ``Provider`` protocol promises: a ``ProviderError`` child.

    Not a bare ``Exception`` and not a vendor SDK error, for two reasons that pull the same way.
    ``providers/`` catches the two vendor trees plus ``ProviderError`` and nothing wider - the
    broad catch belongs to the two seams ``tests/test_events.py`` names - so a stub raising
    ``Exception`` would assert against a catch the harness deliberately does not have. And
    ``tests/test_provider_quirks.py`` keeps the vendor SDKs out of every module outside the
    quarantine, tests included, so the vendor tree cannot be named here either.
    """

    status_code = 401


def rejecting(_settings: config.Settings) -> object:
    """A provider whose one call fails, so validation can be exercised with no network."""

    class Rejects:
        def complete(self, request: object) -> object:
            raise Boom("invalid credential")

    return Rejects()


def accepting(_settings: config.Settings) -> object:
    """A provider whose one call succeeds. It records nothing: validation asserts only the
    absence of an exception."""

    class Accepts:
        def complete(self, request: object) -> object:
            return object()

    return Accepts()


def answering(settings: config.Settings) -> object:
    """A keyed provider's endpoint, stood in for so a run of it stays offline.

    Unlike :func:`accepting` this returns a real ``ChatResponse``, because the loop reads it -
    validation only cares that nothing was raised. The credential is asked for so a run that
    resolved no key fails here rather than answering anyway.
    """
    from harnessed.providers.types import ChatResponse

    settings.require_api_key("groq")

    class Answers:
        def complete(self, request: object) -> ChatResponse:
            return ChatResponse(response_id="resp_stub", model="stub-model", content="pong")

    return Answers()


# ---------------------------------------------------------------------------
# 1. The dotenv merge, on its own
# ---------------------------------------------------------------------------


def test_merge_sets_an_existing_assignment_in_place() -> None:
    text = "# a comment\nHARNESSED_PROVIDER=fake\n\nUNRELATED=keep me\n"
    merged = config.merge_env_text(text, {"HARNESSED_PROVIDER": "groq"})
    assert merged == "# a comment\nHARNESSED_PROVIDER=groq\n\nUNRELATED=keep me\n"


def test_merge_appends_a_variable_the_file_does_not_set() -> None:
    merged = config.merge_env_text("UNRELATED=1\n", {KEY_VAR: SHORT})
    assert merged == f"UNRELATED=1\n{KEY_VAR}={SHORT}\n"


def test_merge_leaves_a_commented_placeholder_commented_and_appends_the_real_line() -> None:
    """A commented line is a comment. Rewriting it in place would change a line init does not
    own and would delete the documentation ``.env.example`` carries."""
    merged = config.merge_env_text(f"# {KEY_VAR}=\n", {KEY_VAR: SHORT})
    assert merged == f"# {KEY_VAR}=\n{KEY_VAR}={SHORT}\n"


def test_merge_changes_nothing_when_every_owned_variable_already_holds_its_value() -> None:
    text = f"HARNESSED_PROVIDER=groq\n{KEY_VAR}={SHORT}\n"
    updates = {"HARNESSED_PROVIDER": "groq", KEY_VAR: SHORT}
    assert config.merge_env_text(text, updates) == text


def test_merge_preserves_a_file_that_does_not_end_in_a_newline() -> None:
    merged = config.merge_env_text("UNRELATED=1", {KEY_VAR: SHORT})
    assert merged == f"UNRELATED=1\n{KEY_VAR}={SHORT}\n"


def test_parse_reads_only_uncommented_assignments() -> None:
    parsed = config.parse_env_text(f"# {KEY_VAR}=nope\nHARNESSED_PROVIDER=groq\n\nA=b=c\n")
    assert parsed == {"HARNESSED_PROVIDER": "groq", "A": "b=c"}


def test_parse_reads_spaced_exported_and_quoted_assignments() -> None:
    """``KEY = value``, ``export KEY=value`` and quoted values are all real assignments, and the
    loader must read them the way python-dotenv does rather than silently dropping them."""
    parsed = config.parse_env_text("A=1\nB = 2\nexport C=3\nD=\"quoted\"\nE='one'\n")
    assert parsed == {"A": "1", "B": "2", "C": "3", "D": "quoted", "E": "one"}


# ---------------------------------------------------------------------------
# 2. The fresh clone
# ---------------------------------------------------------------------------


def test_a_fresh_clone_gets_the_provider_and_exactly_one_credential(fresh_clone: Path) -> None:
    """The first acceptance criterion: scripted stdin picks a provider and supplies a key."""
    result = runner.invoke(app, ["init", "--no-validate"], input=f"groq\n{TYPED}\n")
    assert result.exit_code == 0, result.output

    written = (fresh_clone / ".env").read_text(encoding="utf-8")
    assert assigned(written) == {"HARNESSED_PROVIDER", KEY_VAR}
    assert "HARNESSED_PROVIDER=groq" in written
    assert f"{KEY_VAR}={TYPED}" in written


def test_a_fresh_clone_keeps_the_documentation_the_example_file_carries(fresh_clone: Path) -> None:
    """``.env`` is written from ``.env.example``, so the comments an operator reads survive."""
    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output

    written = (fresh_clone / ".env").read_text(encoding="utf-8")
    assert "# ---- Provider keys (set only the one you use) " in written


def test_what_was_typed_never_appears_in_the_output_of_a_successful_run(fresh_clone: Path) -> None:
    result = runner.invoke(app, ["init", "--no-validate"], input=f"groq\n{TYPED}\n")
    assert result.exit_code == 0, result.output
    assert TYPED not in result.output


# ---------------------------------------------------------------------------
# 3. The merge, through the command
# ---------------------------------------------------------------------------


def test_an_unrelated_variable_survives_verbatim_in_its_own_position(fresh_clone: Path) -> None:
    """The criterion the whole design is built around: value, comment and position all hold."""
    before = (
        "# the operator's own note\n"
        "UNRELATED=some value  # trailing\n"
        "HARNESSED_HOME=/tmp/elsewhere\n"
    )
    (fresh_clone / ".env").write_text(before, encoding="utf-8")

    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output

    after = (fresh_clone / ".env").read_text(encoding="utf-8")
    assert after.startswith(before)
    assert after.splitlines()[:3] == before.splitlines()
    assert assigned(after) == {"UNRELATED", "HARNESSED_HOME", "HARNESSED_PROVIDER", KEY_VAR}


def test_only_the_variables_init_owns_change(fresh_clone: Path) -> None:
    before = f"HARNESSED_PROVIDER=fake\n{OTHER_VAR}=someone elses value\n"
    (fresh_clone / ".env").write_text(before, encoding="utf-8")

    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output

    after = config.parse_env_text((fresh_clone / ".env").read_text(encoding="utf-8"))
    assert after["HARNESSED_PROVIDER"] == "groq"
    assert after[KEY_VAR] == TYPED
    assert after[OTHER_VAR] == "someone elses value"


# ---------------------------------------------------------------------------
# 4. Validation
# ---------------------------------------------------------------------------


def test_a_refusal_names_the_provider_the_endpoint_and_the_status(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(provider_registry, "create", rejecting)
    result = shell(["init", "--provider", "groq"], f"{TYPED}\n", monkeypatch, capsys)

    assert result.code != 0
    assert "groq" in result.err
    assert "https://api.groq.com/openai/v1" in result.err
    assert "401" in result.err


def test_a_refusal_leaves_the_env_file_untouched(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = "UNRELATED=keep me\n"
    (fresh_clone / ".env").write_text(before, encoding="utf-8")
    monkeypatch.setattr(provider_registry, "create", rejecting)

    result = shell(["init", "--provider", "groq"], f"{TYPED}\n", monkeypatch, capsys)

    assert result.code != 0
    assert (fresh_clone / ".env").read_text(encoding="utf-8") == before


def test_a_refusal_never_repeats_what_was_typed(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(provider_registry, "create", rejecting)
    result = shell(["init", "--provider", "groq"], f"{TYPED}\n", monkeypatch, capsys)
    assert TYPED not in result.output


def test_validation_writes_the_env_file_when_the_endpoint_accepts(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard on the guard: the failing path above must fail for the credential, not for the
    stub."""
    monkeypatch.setattr(provider_registry, "create", accepting)
    result = shell(["init", "--provider", "groq"], f"{TYPED}\n", monkeypatch, capsys)

    assert result.code == 0, result.output
    parsed = config.parse_env_text((fresh_clone / ".env").read_text(encoding="utf-8"))
    assert parsed[KEY_VAR] == TYPED


def test_no_validate_asks_nothing_and_calls_no_provider(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--provider`` plus ``--no-validate`` is the fully non-interactive path.

    The socket guard would already fail a run that reached an endpoint; this asserts the
    stronger claim that the provider is never even constructed, and that nothing is asked.
    """

    def refuse(_settings: config.Settings) -> object:
        raise AssertionError("--no-validate constructed a provider")

    monkeypatch.setattr(provider_registry, "create", refuse)
    argv = ["init", "--provider", "groq", "--no-validate"]
    result = shell(argv, f"{TYPED}\n", monkeypatch, capsys)

    assert result.code == 0, result.output
    assert "?" not in result.out
    assert (fresh_clone / ".env").exists()


# ---------------------------------------------------------------------------
# 5. What it leaves the operator with
# ---------------------------------------------------------------------------


def test_a_successful_run_prints_the_three_rungs_and_marks_the_first_one_live(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential is already exported, so ``live`` is a true claim and rung 1 makes it.

    The environment variable is what this branch turns on. Before ticket 038 this test set
    nothing and still asserted ``live``, which is exactly the false claim FR-26 was failing on:
    the assertion held of the wording and not of the harness.
    """
    monkeypatch.setenv(KEY_VAR, TYPED)
    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output

    for rung in ("rung 1", "rung 2", "rung 3"):
        assert rung in result.output
    assert "live" in result.output
    assert "harnessed agents add" in result.output


def test_a_successful_run_names_a_command_the_operator_can_now_type(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_VAR, TYPED)
    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert TRY_IT_NOW in result.output
    assert "harnessed run" in result.output


# ---------------------------------------------------------------------------
# 5b. The claim and the behaviour, compared (ticket 038)
# ---------------------------------------------------------------------------


def rung_one(output: str) -> str:
    """The rung 1 row of a report, so an assertion about it cannot read rung 2's words."""
    rows = [row for row in output.splitlines() if row.strip().startswith("rung 1")]
    assert len(rows) == 1, output
    return rows[0]


def test_rung_one_is_live_when_the_credential_is_only_in_the_file(fresh_clone: Path) -> None:
    """Ticket 038's honest not-yet branch, retired by ticket 039 making the claim true.

    Nothing is exported here: the credential exists only in the ``.env`` init has just written,
    which is now a place ``config.py`` reads a key back out of. The branch that told the operator
    to export it is deleted rather than left dormant - an unreachable branch is a lie waiting to
    be re-enabled.
    """
    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output

    row = rung_one(result.output)
    assert "live" in row
    assert TRY_IT_NOW in result.output


def test_no_export_instruction_survives_anywhere_in_the_report(fresh_clone: Path) -> None:
    """The instruction is gone from the output *and* from the module, so it cannot come back as
    a dormant branch nobody reaches. ``export_lines`` and its placeholder are deleted with it."""
    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")

    assert f"$env:{KEY_VAR} = " not in result.output
    assert f"export {KEY_VAR}=" not in result.output
    assert not hasattr(init_cmd, "export_lines")


def test_the_credential_still_never_appears_in_the_output(fresh_clone: Path) -> None:
    """Unchanged by the reversal and asserted again beside it: what the operator typed is never
    printed, in any branch, in any form."""
    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert TYPED not in result.output


def test_a_keyless_provider_is_live_and_gets_the_command_to_try(fresh_clone: Path) -> None:
    """``fake`` needs nothing exported, so nothing about this branch changes."""
    result = runner.invoke(app, ["init", "--provider", "fake", "--no-validate"], input="")
    assert result.exit_code == 0, result.output
    assert "live" in rung_one(result.output)
    assert TRY_IT_NOW in result.output


@pytest.mark.parametrize("provider", ["groq", "fake"])
def test_the_reported_rung_agrees_with_what_the_next_command_actually_does(
    provider: str, fresh_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion that would have caught the defect: a claim compared with a behaviour.

    ``HARNESSED_PROVIDER`` is deleted between the two invocations so the second one resolves the
    provider the way a fresh operator's shell does - out of the ``.env`` init has just written,
    not out of the offline variable ``clean_env`` sets for the suite. Nothing about the
    *configuration* is stubbed, which is the part under test; the keyed provider's endpoint is
    stood in for so the second command stays offline, because since ticket 039 rung 1 claims a
    working command rather than a pending export and asserting that against a real network call
    is what the socket guard exists to prevent.

    Rung 1 claims only that the *chat* provider works, and the router's own Decider always needs
    ``OPENROUTER_API_KEY`` (decision 57) whatever the chat provider is - a second, unrelated
    credential this test does not configure and the claim under test never promised (ticket 160).
    Ticket 179 deleted the flag that used to turn routing off for exactly this; ``build_decider``
    is stubbed instead, to a Decider whose own call always falls back (decision 59) rather than
    ever reaching the network.
    """
    from harnessed.cli import run_cmd
    from harnessed.errors import DecisionUnreachable

    class _AlwaysFallbackDecider:
        def decide(self, state: str, questions: object) -> object:
            raise DecisionUnreachable("stubbed: no live router in this test")

    monkeypatch.setattr(run_cmd, "build_decider", lambda settings: _AlwaysFallbackDecider())
    monkeypatch.setitem(provider_registry.OFFLINE_FACTORIES, "groq", answering)
    init_result = runner.invoke(
        app, ["init", "--provider", provider, "--no-validate"], input=f"{TYPED}\n"
    )
    assert init_result.exit_code == 0, init_result.output
    claimed_live = "live" in rung_one(init_result.output)

    monkeypatch.delenv("HARNESSED_PROVIDER", raising=False)
    run_result = runner.invoke(app, ["run", "ping"])

    assert claimed_live == (run_result.exit_code == 0), init_result.output + run_result.output


# ---------------------------------------------------------------------------
# 5c. The same fact, for a machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exported", [False, True], ids=["file-only", "exported-too"])
def test_json_reports_the_credential_live_however_it_was_supplied(
    exported: bool, fresh_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both arms of the order end in the same true claim since ticket 039.

    The field is still *computed* rather than asserted as a constant: it is read back out of the
    resolution the next command will perform, so a loader that stopped reading the file would
    turn this green pair red instead of leaving a hardcoded ``true`` behind.
    """
    if exported:
        monkeypatch.setenv(str(KEY_VAR), TYPED)
    argv = ["init", "--provider", "groq", "--no-validate", "--json"]
    result = runner.invoke(app, argv, input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["credential_live"] is True
    assert payload["credential_var"] == KEY_VAR
    assert payload["provider"] == "groq"


def test_json_writes_exactly_one_object_and_no_prose(fresh_clone: Path) -> None:
    argv = ["init", "--provider", "groq", "--no-validate", "--json"]
    result = runner.invoke(app, argv, input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output

    assert len([row for row in result.stdout.splitlines() if row.strip()]) == 1
    payload = json.loads(result.stdout)
    assert TYPED not in result.stdout
    assert payload["rungs"][0]["number"] == 1


def test_json_and_the_human_report_agree_on_the_same_run(fresh_clone: Path) -> None:
    """One fact, two renderings. A dashboard must not read a different state from the operator."""
    argv = ["init", "--provider", "groq", "--no-validate"]
    human = runner.invoke(app, argv, input=f"{TYPED}\n")
    machine = runner.invoke(app, [*argv, "--json"], input=f"{TYPED}\n")

    payload = json.loads(machine.stdout)
    assert payload["credential_live"] is ("live" in rung_one(human.output))


def test_a_keyless_provider_is_live_in_the_json_too(fresh_clone: Path) -> None:
    result = runner.invoke(app, ["init", "--provider", "fake", "--no-validate", "--json"], input="")
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["credential_live"] is True
    assert payload["credential_var"] is None


# ---------------------------------------------------------------------------
# 5d. Evidence (ticket 038)
# ---------------------------------------------------------------------------


def test_evidence_captures_the_report_a_fresh_clone_now_gets(fresh_clone: Path) -> None:
    """The manifest ticket 039 names, regenerated on every verify run.

    Ticket 038's pair of files documented a two-branch report; this ticket deleted one of those
    branches, so what is captured here is the single report that survives. The path is absolute:
    every test here runs with the process sitting in ``tmp_path``, so a relative one would write
    the evidence into a directory pytest deletes.
    """
    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output

    directory = REPO_ROOT / "evidence" / "039"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "init-live-from-the-file.txt").write_text(result.output, encoding="utf-8")

    assert "live" in rung_one(result.output)
    assert TYPED not in result.output


# ---------------------------------------------------------------------------
# 6. Idempotence
# ---------------------------------------------------------------------------


def test_a_second_run_reports_the_existing_configuration_and_changes_nothing(
    fresh_clone: Path,
) -> None:
    argv = ["init", "--provider", "groq", "--no-validate"]
    first = runner.invoke(app, argv, input=f"{TYPED}\n")
    assert first.exit_code == 0, first.output
    after_first = (fresh_clone / ".env").read_text(encoding="utf-8")

    second = runner.invoke(app, argv, input=f"{TYPED}\n")
    assert second.exit_code == 0, second.output

    assert (fresh_clone / ".env").read_text(encoding="utf-8") == after_first
    assert "unchanged" in second.output
    assert "groq" in second.output


def test_a_second_run_duplicates_no_line(fresh_clone: Path) -> None:
    argv = ["init", "--provider", "groq", "--no-validate"]
    runner.invoke(app, argv, input=f"{TYPED}\n")
    runner.invoke(app, argv, input=f"{TYPED}\n")

    written = (fresh_clone / ".env").read_text(encoding="utf-8")
    # The example file's commented placeholder, plus exactly one real assignment.
    assert written.count(f"{KEY_VAR}=") == 2
    assert written.count(f"\n{KEY_VAR}=") == 1


# ---------------------------------------------------------------------------
# 7. The surface itself
# ---------------------------------------------------------------------------


def test_init_is_a_top_level_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "init" in result.output


def test_an_unknown_provider_is_named_with_the_known_ones(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["init", "--provider", "grok", "--no-validate"]
    result = shell(argv, f"{TYPED}\n", monkeypatch, capsys)

    assert result.code != 0
    assert "grok" in result.err
    assert "groq" in result.err
    assert not (fresh_clone / ".env").exists()


def test_a_keyless_provider_asks_for_nothing(fresh_clone: Path) -> None:
    """``ollama`` needs no credential, so init must not invent a variable to hold one."""
    result = runner.invoke(app, ["init", "--provider", "ollama", "--no-validate"], input="")
    assert result.exit_code == 0, result.output
    assert assigned((fresh_clone / ".env").read_text(encoding="utf-8")) == {"HARNESSED_PROVIDER"}


def test_the_variables_init_owns_are_declared_rather_than_described() -> None:
    """The blast radius is data: the provider, the optional model, and the chosen key var."""
    assert init_cmd.OWNED_KEYS == ("HARNESSED_PROVIDER", "HARNESSED_MODEL")


def test_init_says_routing_also_needs_the_openrouter_key(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 232: a chat key alone leaves routing unconfigured, and init says so."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output
    assert "routing also needs OPENROUTER_API_KEY" in result.output


def test_init_with_only_a_groq_key_says_routing_is_off_and_agents_need_it_on(
    fresh_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 316 (FR-175, operator ruling Q12): a newcomer's first key is Groq alone."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = runner.invoke(app, ["init", "--provider", "groq", "--no-validate"], input=f"{TYPED}\n")
    assert result.exit_code == 0, result.output
    assert "routing is off: set OPENROUTER_API_KEY to turn it on" in result.output
    assert "agents run only with routing on" in result.output
