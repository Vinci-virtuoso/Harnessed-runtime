"""FR-27: `fake` is reachable only by naming it, and an unconfigured harness says so.

The resolution order the architecture pins is asserted directly here, one arm at a time:

    --provider flag  ->  HARNESSED_PROVIDER env  ->  .env file  ->  ERROR naming `harnessed init`

There is no fallback arm, so the last row of that table is an error rather than a default. The
danger this file guards is narrow and specific: a fixture replay that looks like a working agent
to an operator who has configured nothing. A first run that appears to succeed while doing
nothing real is worse than a first run that fails.

Every test that exercises the unconfigured state runs from a chdir'd temporary directory. The
`.env` arm reads the file in the working directory, so a developer with a real `.env` in the repo
root would otherwise flip "unconfigured" into "configured" and this whole file would pass for the
wrong reason on their machine and fail in CI.
"""

from __future__ import annotations

import ast
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import conftest
from conftest import DOCUMENTED_ENV_VARS
from harnessed import config
from harnessed.cli.app import main
from harnessed.cli.run_cmd import open_session
from harnessed.errors import ConfigError, HarnessError, ProviderNotConfiguredError
from harnessed.providers import build_provider, quirks, registry

# The prompt the committed corpus can answer. Quoted from `tests/test_cli.py`'s own ad-hoc run
# rather than invented: MUST rule 8 makes adding a fixture a deliberate act with a reason, and
# "the success path needs a prompt" is not one.
ANSWERABLE = "ping"

SOURCE_ROOT = Path(config.__file__).resolve().parent
REPO_ROOT = SOURCE_ROOT.parents[1]


def _module_source(relative: str) -> ast.Module:
    return ast.parse((SOURCE_ROOT / relative).read_text(encoding="utf-8"))


@pytest.fixture
def unconfigured(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> pytest.MonkeyPatch:
    """The state a fresh clone is in: no flag, no variable, and no `.env` within reach.

    `clean_env` selects `fake` for the rest of the suite, which is exactly the deliberate
    pytest-fixture selection FR-27 sanctions, so this fixture has to undo it. The chdir is what
    makes the `.env` arm honest on a machine that has one.
    """
    monkeypatch.delenv("HARNESSED_PROVIDER", raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


# ---------------------------------------------------------------------------
# 1. the error itself
# ---------------------------------------------------------------------------


def test_the_error_is_a_harness_error_so_the_cli_renders_it_rather_than_a_traceback() -> None:
    """`main` catches `HarnessError` only. Anything outside the tree reaches the operator as a
    traceback, which is the one thing FR-27 says this failure must never be."""
    assert issubclass(ProviderNotConfiguredError, HarnessError)
    assert issubclass(ProviderNotConfiguredError, ConfigError)


def test_the_error_carries_the_variable_and_the_remedy_as_fields() -> None:
    """`render.py` formats guidance off these fields. A renderer that string-matched the message
    would break the moment the wording changed, and the wording is the part that gets edited."""
    exc = ProviderNotConfiguredError()
    assert exc.env_var == "HARNESSED_PROVIDER"
    assert exc.remedy == "harnessed init"
    assert exc.env_var in str(exc)
    assert exc.remedy in str(exc)


# ---------------------------------------------------------------------------
# 2. the resolution order
# ---------------------------------------------------------------------------


def test_nothing_configured_resolves_to_no_provider(unconfigured: pytest.MonkeyPatch) -> None:
    """`load()` reports the absence rather than inventing a default. The raise happens one layer
    up, so `harnessed doctor` can describe an unconfigured harness without failing to start."""
    assert config.load().provider is None


def test_nothing_configured_raises_when_the_provider_is_built(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ProviderNotConfiguredError):
        build_provider(config.load())


def test_the_env_var_selects_the_provider(unconfigured: pytest.MonkeyPatch) -> None:
    unconfigured.setenv("HARNESSED_PROVIDER", "fake")
    assert config.load().provider == "fake"


def test_the_env_var_selection_builds_the_same_provider_as_the_flag(
    unconfigured: pytest.MonkeyPatch, tmp_harnessed_home: Path
) -> None:
    unconfigured.setenv("HARNESSED_PROVIDER", "fake")
    from harnessed.providers.fake import FakeProvider

    assert isinstance(build_provider(config.load()), FakeProvider)


def test_the_flag_selects_the_provider(unconfigured: pytest.MonkeyPatch) -> None:
    assert config.load(provider="fake").provider == "fake"


def test_the_flag_beats_the_env_var(unconfigured: pytest.MonkeyPatch) -> None:
    """The whole point of the order: an operator who types `--provider fake` gets `fake` even
    though the shell they are sitting in says otherwise."""
    unconfigured.setenv("HARNESSED_PROVIDER", "groq")
    assert config.load(provider="fake").provider == "fake"


def test_a_dotenv_file_selects_the_provider(unconfigured: pytest.MonkeyPatch) -> None:
    """The third arm. `harnessed init` writes this file, so a configured clone stays configured
    across shells without anything being exported."""
    Path(".env").write_text("# a comment\nHARNESSED_PROVIDER=fake\n", encoding="utf-8")
    assert config.load().provider == "fake"


def test_the_env_var_beats_the_dotenv_file(unconfigured: pytest.MonkeyPatch) -> None:
    Path(".env").write_text("HARNESSED_PROVIDER=groq\n", encoding="utf-8")
    unconfigured.setenv("HARNESSED_PROVIDER", "fake")
    assert config.load().provider == "fake"


def test_the_flag_beats_the_dotenv_file(unconfigured: pytest.MonkeyPatch) -> None:
    Path(".env").write_text("HARNESSED_PROVIDER=groq\n", encoding="utf-8")
    assert config.load(provider="fake").provider == "fake"


def test_a_commented_out_assignment_in_dotenv_selects_nothing(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """A `#`-prefixed line documents a variable, it does not set one. `parse_env_text` already
    draws that line for `harnessed init`; resolution reads the file the same way."""
    Path(".env").write_text("# HARNESSED_PROVIDER=fake\n", encoding="utf-8")
    assert config.load().provider is None


def test_a_typo_in_the_dotenv_file_fails_fast_with_the_known_names(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """A misspelled provider is a `ConfigError` at load, not a `KeyError` at the first call."""
    Path(".env").write_text("HARNESSED_PROVIDER=fakke\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        config.load()
    assert "fakke" in str(info.value)


# ---------------------------------------------------------------------------
# 3. what the operator sees
# ---------------------------------------------------------------------------


def _run(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    """One command through `main` - argv and all - and what it wrote to each stream.

    `CliRunner` records an escaping `HarnessError` on the result object and never renders it, so
    "fails with the reason on stderr" can only be asserted by driving `main` itself.
    """
    monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
    try:
        main()
    except SystemExit as exit_info:
        code = 1 if exit_info.code is None else int(exit_info.code)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_an_unconfigured_run_exits_non_zero_with_instructions_and_no_traceback(
    unconfigured: pytest.MonkeyPatch,
    tmp_harnessed_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole ticket in one assertion set: it fails, it says what to do, and it does not
    replay a fixture at somebody who thinks they are talking to a model."""
    code, out, err = _run(["run", "hello"], unconfigured, capsys)
    assert code != 0
    assert "harnessed init" in err
    assert "HARNESSED_PROVIDER" in err
    assert "Traceback" not in err
    assert "Traceback" not in out


def test_the_flag_makes_the_same_run_succeed(
    unconfigured: pytest.MonkeyPatch,
    tmp_harnessed_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every other test in the suite depends on this path, so it is asserted here too rather
    than assumed from `tests/test_cli.py`."""
    code, out, err = _run(["run", "--provider", "fake", ANSWERABLE], unconfigured, capsys)
    assert code == 0, err
    assert "pong" in out


def test_the_env_var_makes_the_same_run_succeed(
    unconfigured: pytest.MonkeyPatch,
    tmp_harnessed_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unconfigured.setenv("HARNESSED_PROVIDER", "fake")
    code, out, err = _run(["run", ANSWERABLE], unconfigured, capsys)
    assert code == 0, err
    assert "pong" in out


# ---------------------------------------------------------------------------
# 4. the scans - no branch anywhere may reach `fake` unnamed
# ---------------------------------------------------------------------------


def test_the_provider_package_never_names_fake() -> None:
    """`providers/__init__.py` is where `build_provider` decides. A `"fake"` literal in it is a
    fallback waiting to be written, whatever the surrounding branch currently says."""
    named = [
        node.value
        for node in ast.walk(_module_source("providers/__init__.py"))
        if isinstance(node, ast.Constant) and node.value == "fake"
    ]
    assert named == []


def test_resolution_never_names_fake() -> None:
    """The scan that was red before this ticket: `config.load` used to end its provider
    expression with `or DEFAULT_PROVIDER`, and `DEFAULT_PROVIDER` was `"fake"`.

    Scoped to the resolving functions rather than to the whole module, because
    `PROVIDER_KEY_VARS` legitimately lists `fake` as a provider that needs no key - naming it in
    a table of every provider is not selecting it.
    """
    tree = _module_source("config.py")
    resolvers = {"load", "_dotenv_values", "_resolved", "_resolve_provider"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in resolvers:
            literals = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and child.value == "fake"
            ]
            assert literals == [], f"{node.name} names 'fake'"
    module_constants = [
        target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign | ast.Assign)
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name)
    ]
    assert "DEFAULT_PROVIDER" not in module_constants


# ---------------------------------------------------------------------------
# 5. the same order, applied to the key vars (ticket 039)
# ---------------------------------------------------------------------------

#: A value with no credential shape at all, so nothing in `redact.py`'s pattern table can match
#: it and only the environment arm of `secret_values()` can. Obviously not a real key.
DOTENV_KEY = "not-a-real-key-from-the-file-2718281828"
EXPORTED_KEY = "not-a-real-key-from-the-shell-1414213562"

#: The provider whose key var these tests write. Quoted from the table rather than retyped.
KEYED_PROVIDER = "groq"
KEY_VAR = config.PROVIDER_KEY_VARS[KEYED_PROVIDER]


def write_dotenv(**assignments: str) -> None:
    """A `.env` in the working directory setting exactly `assignments`, in order."""
    body = "".join(f"{name}={value}\n" for name, value in assignments.items())
    Path(config.ENV_FILENAME).write_text(body, encoding="utf-8")


def write_evidence(name: str, text: str) -> Path:
    """One artefact under `evidence/040/`. Regenerated on every verify run, never committed."""
    directory = REPO_ROOT / "evidence" / "040"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_dotenv_file_supplies_the_key_for_the_provider_it_names(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """The whole ticket in one assertion: a file is enough, an export is not required.

    Before ticket 039 `config.py` read exactly one variable back out of `.env` and refused the
    next one, so a clone `harnessed init` had just configured still failed on every command.
    """
    write_dotenv(HARNESSED_PROVIDER=KEYED_PROVIDER, **{str(KEY_VAR): DOTENV_KEY})
    settings = config.load()
    assert settings.provider == KEYED_PROVIDER
    assert settings.api_key_for(KEYED_PROVIDER) == DOTENV_KEY
    assert settings.require_api_key(KEYED_PROVIDER) == DOTENV_KEY


def test_an_exported_key_beats_the_dotenv_file(unconfigured: pytest.MonkeyPatch) -> None:
    """The middle arm of the order, unchanged from the provider's: the shell wins over the file.

    Asserted with two *different* values rather than by unsetting one, because "the file was
    ignored" and "the environment was read" are the same assertion only when they disagree.
    """
    write_dotenv(HARNESSED_PROVIDER=KEYED_PROVIDER, **{str(KEY_VAR): DOTENV_KEY})
    unconfigured.setenv(str(KEY_VAR), EXPORTED_KEY)
    assert config.load().api_key_for(KEYED_PROVIDER) == EXPORTED_KEY


def test_a_key_in_the_file_is_stripped_of_surrounding_whitespace(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """A stray space would reach the endpoint inside an `Authorization` header and earn a 400,
    which reads as a rejected key rather than as a typo in a file."""
    Path(config.ENV_FILENAME).write_text(f"{KEY_VAR}=  {DOTENV_KEY}  \n", encoding="utf-8")
    assert config.load(provider=KEYED_PROVIDER).api_key_for(KEYED_PROVIDER) == DOTENV_KEY


def test_a_commented_out_key_in_the_file_supplies_nothing(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """`.env.example` ships commented placeholders for every key var. A file that documents a
    variable does not set it, and a placeholder must never become a credential."""
    Path(config.ENV_FILENAME).write_text(f"# {KEY_VAR}={DOTENV_KEY}\n", encoding="utf-8")
    assert config.load(provider=KEYED_PROVIDER).api_key_for(KEYED_PROVIDER) is None


def test_no_key_anywhere_still_raises_the_missing_credential_error(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """FR-27's guarantee is untouched: this ticket changes where a key may be read from and
    nothing about what happens when there is none."""
    write_dotenv(HARNESSED_PROVIDER=KEYED_PROVIDER)
    from harnessed.errors import MissingCredentialError

    with pytest.raises(MissingCredentialError):
        config.load().require_api_key(KEYED_PROVIDER)


def test_only_a_documented_variable_is_taken_from_the_file(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """The file is a configuration source, not an alternative environment: `load()` reads the
    names it documents and no others.

    This test's subject moved rather than being deleted. It once said `HARNESSED_HOME` was left
    on the floor along with `SOMETHING_ELSE`, which was true while the file arm read the provider
    and the keys alone and went false the moment ticket 040 made the file supply every documented
    variable. What survives that reversal is the half that is still true - an undocumented name
    is inert - and `HARNESSED_HOME` moved to the enumeration in section 8.
    """
    write_dotenv(HARNESSED_PROVIDER="fake", SOMETHING_ELSE=DOTENV_KEY)
    assert config.load().provider == "fake"
    assert DOTENV_KEY not in config.secret_values()


def test_a_run_configured_only_by_the_file_succeeds(
    unconfigured: pytest.MonkeyPatch,
    tmp_harnessed_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No export step, in any shell. `fake` needs no key, so what this proves is the provider
    arm end to end; the key arm reaching a real adapter is asserted in `tests/test_redaction.py`,
    where the value it carries can also be checked out of the stores."""
    write_dotenv(HARNESSED_PROVIDER="fake")
    code, out, err = _run(["run", ANSWERABLE], unconfigured, capsys)
    assert code == 0, err
    assert "pong" in out

    directory = REPO_ROOT / "evidence" / "039"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dotenv-key-works.txt").write_text(out + err, encoding="utf-8")


def test_an_unreadable_dotenv_file_is_treated_as_absent(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """A directory named `.env` is the cheapest unreadable file that exists on every platform.
    A stray one must not be fatal to a command configured perfectly well by other means."""
    Path(config.ENV_FILENAME).mkdir()
    unconfigured.setenv("HARNESSED_PROVIDER", "fake")
    assert config.load().provider == "fake"


def test_an_undecodable_dotenv_file_is_treated_as_absent(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    Path(config.ENV_FILENAME).write_bytes(b"\xff\xfe\x00 not utf-8")
    unconfigured.setenv("HARNESSED_PROVIDER", "fake")
    assert config.load().provider == "fake"


# ---------------------------------------------------------------------------
# 6. the model id the request carries (ticket 040)
# ---------------------------------------------------------------------------

#: A provider the table deliberately has no entry for. Ticket 040 seeds the table only with ids
#: confirmed against the real endpoint, so the three that were never confirmed fail loudly
#: instead of being guessed at, and this is one of them.
UNTABLED_PROVIDER = "openai"


def test_a_provider_in_the_table_supplies_the_model_when_nothing_is_configured(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """The defect this ticket exists for: with no `HARNESSED_MODEL`, the request used to carry
    the provider's own name, which is a 404 at every vendor."""
    unconfigured.setenv(config.PROVIDER_VAR, KEYED_PROVIDER)
    unconfigured.setenv(str(KEY_VAR), EXPORTED_KEY)
    settings = config.load()
    assert settings.model is None
    assert registry.model_for(settings) == quirks.PROVIDER_DEFAULT_MODELS[KEYED_PROVIDER]


@pytest.mark.parametrize("provider", sorted(quirks.PROVIDER_DEFAULT_MODELS))
def test_no_default_in_the_table_is_a_provider_name(provider: str) -> None:
    """A provider name is never a valid model id anywhere, so the table may never hold one."""
    assert quirks.PROVIDER_DEFAULT_MODELS[provider] != provider


def test_the_configured_model_beats_the_table(unconfigured: pytest.MonkeyPatch) -> None:
    unconfigured.setenv(config.PROVIDER_VAR, KEYED_PROVIDER)
    unconfigured.setenv(str(KEY_VAR), EXPORTED_KEY)
    unconfigured.setenv(config.MODEL_VAR, "a-model-the-operator-chose")
    assert registry.model_for(config.load()) == "a-model-the-operator-chose"


def test_a_provider_with_no_table_entry_fails_loudly_before_any_request(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """FR-27's pattern applied to the model: no silent guess. The message has to name the
    provider, name the variable, and say that no default is known."""
    unconfigured.setenv(config.PROVIDER_VAR, UNTABLED_PROVIDER)
    unconfigured.setenv("OPENAI_API_KEY", EXPORTED_KEY)
    settings = config.load()
    with pytest.raises(ConfigError) as raised:
        registry.model_for(settings)
    message = str(raised.value)
    assert UNTABLED_PROVIDER in message
    assert config.MODEL_VAR in message
    assert "no default" in message
    assert isinstance(raised.value, HarnessError)
    write_evidence("unknown-provider-fails-loudly.txt", message)


def test_a_run_with_no_model_configured_uses_the_default_and_completes(
    unconfigured: pytest.MonkeyPatch,
    tmp_harnessed_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole first criterion end to end: nothing but a provider configured, and the request
    carries the table's id rather than the provider's name."""
    write_dotenv(HARNESSED_PROVIDER="fake")
    session = open_session()
    assert session.settings.model is None
    assert session.model == quirks.PROVIDER_DEFAULT_MODELS["fake"]
    assert session.model != "fake"

    code, out, err = _run(["run", ANSWERABLE], unconfigured, capsys)
    assert code == 0, err
    assert "pong" in out
    write_evidence(
        "default-model-run.txt",
        f"HARNESSED_MODEL unset; request model={session.model!r}\n{out}{err}",
    )


# ---------------------------------------------------------------------------
# 7. the scan - no expression defaults a model to a provider name
# ---------------------------------------------------------------------------


def _mentions(node: ast.AST, word: str) -> bool:
    """Whether any name, attribute or literal spelled inside ``node`` contains ``word``."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and word in inner.id:
            return True
        if isinstance(inner, ast.Attribute) and word in inner.attr:
            return True
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str) and word in inner.value:
            return True
    return False


def model_defaulted_to_a_provider(tree: ast.AST) -> list[int]:
    """The lines of every ``<something model> or <something provider>`` expression.

    ``x or y`` is how the deleted fallback was written and how it would be rewritten. The scan
    reads both halves: a left side that names a model and a right side that names a provider is
    the shape, whatever module it appears in.
    """
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        head, *rest = node.values
        if _mentions(head, "model") and any(_mentions(tail, "provider") for tail in rest):
            lines.append(node.lineno)
    return lines


def test_no_expression_in_the_package_defaults_a_model_to_a_provider_name() -> None:
    """The second criterion. The fallback is deleted, not moved behind a condition."""
    offenders = {
        path.relative_to(SOURCE_ROOT).as_posix(): lines
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if (lines := model_defaulted_to_a_provider(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert offenders == {}


def test_the_scan_would_notice_the_fallback_it_deleted() -> None:
    """A scan that has never seen its own subject is a row of assertions that would pass against
    anything, so it is run twice: once over the package, once over what it forbids."""
    forbidden = ast.parse(
        "def model(self):\n    return self.settings.model or self.settings.require_provider()\n"
    )
    assert model_defaulted_to_a_provider(forbidden) == [2]
    permitted = ast.parse("chosen = model or settings.model\n")
    assert model_defaulted_to_a_provider(permitted) == []


# ---------------------------------------------------------------------------
# 8. every documented variable, from the file (ticket 040)
# ---------------------------------------------------------------------------


def _key_reader(var: str) -> Callable[[config.Settings], str | None]:
    """Read a secret var back off ``Settings`` through the provider that owns it."""
    provider = next(name for name, owned in config.PROVIDER_KEY_VARS.items() if owned == var)
    return lambda settings: settings.api_key_for(provider)


#: One probe per documented variable: what the file says, what the environment says, and how to
#: read the resolved value back. The keys are asserted equal to ``DOCUMENTED_ENV_VARS`` below,
#: so a variable added there without a probe fails here rather than being forgotten - which is
#: exactly how ``HARNESSED_MODEL`` went three tickets without a file arm.
FILE_PROBES: dict[str, tuple[str, str, Callable[[config.Settings], object]]] = {
    "HARNESSED_HOME": ("home-from-file", "home-from-env", lambda s: str(s.home)),
    "HARNESSED_PROVIDER": ("fake", "groq", lambda s: s.provider),
    "HARNESSED_MODEL": ("model-from-file", "model-from-env", lambda s: s.model),
    "HARNESSED_SMALL_MODEL": ("small-from-file", "small-from-env", lambda s: s.small_model),
    "HARNESSED_BASE_URL": ("http://from-file/v1", "http://from-env/v1", lambda s: s.base_url),
    "HARNESSED_TOKEN_BUDGET": ("4321", "1234", lambda s: str(s.retrieval_token_budget)),
    "HARNESSED_MAX_ITERATIONS": ("7", "9", lambda s: str(s.max_iterations)),
    "HARNESSED_PERMISSION_CEILING_S": ("700", "800", lambda s: str(s.permission_ceiling_s)),
    "HARNESSED_CONSOLIDATE_AFTER": ("31", "32", lambda s: str(s.consolidate_after)),
    "HARNESSED_EMBED_BASE_URL": (
        "http://embed-file/v1",
        "http://embed-env/v1",
        lambda s: s.embed_base_url,
    ),
    "HARNESSED_EMBED_MODEL": ("embed-from-file", "embed-from-env", lambda s: s.embed_model),
    "HARNESSED_ROUTER_MODEL": (
        "router-from-file",
        "router-from-env",
        lambda s: s.router_model,
    ),
    **{
        var: (f"{DOTENV_KEY}-{var.lower()}", f"{EXPORTED_KEY}-{var.lower()}", _key_reader(var))
        for var in sorted(config.SECRET_VARS)
    },
}


def test_every_documented_variable_has_a_probe() -> None:
    """The forcing function. A new documented variable with no probe is a red test here, so the
    file arm cannot be forgotten for it the way it was forgotten for the model."""
    assert set(FILE_PROBES) == set(DOCUMENTED_ENV_VARS)


@pytest.mark.parametrize("name", DOCUMENTED_ENV_VARS)
def test_a_documented_variable_resolves_from_the_dotenv_file(
    name: str, unconfigured: pytest.MonkeyPatch
) -> None:
    """The fourth criterion, one variable at a time. Before this ticket the file arm read the
    provider and the keys and nothing else."""
    from_file, _, read = FILE_PROBES[name]
    write_dotenv(**{"HARNESSED_PROVIDER": "fake", name: from_file})
    assert read(config.load()) == from_file


@pytest.mark.parametrize("name", DOCUMENTED_ENV_VARS)
def test_the_environment_beats_the_dotenv_file_for_every_documented_variable(
    name: str, unconfigured: pytest.MonkeyPatch
) -> None:
    """The fifth criterion: one order, applied to every variable, matching what 039 established
    for the keys."""
    from_file, from_env, read = FILE_PROBES[name]
    write_dotenv(**{"HARNESSED_PROVIDER": "fake", name: from_file})
    unconfigured.setenv(name, from_env)
    assert read(config.load()) == from_env


def test_a_non_integer_in_the_file_is_a_config_error_naming_the_variable(
    unconfigured: pytest.MonkeyPatch,
) -> None:
    """The file arm goes through the same parse as the environment arm, so a typo in it fails
    the same way rather than silently falling back to the default."""
    write_dotenv(HARNESSED_PROVIDER="fake", HARNESSED_MAX_ITERATIONS="lots")
    with pytest.raises(ConfigError, match="HARNESSED_MAX_ITERATIONS"):
        config.load()


# Ticket 056. Nothing may put this repository's `.env` into the test process.


@pytest.mark.parametrize("name", conftest.GUARDED_ENV_VARS)
def test_no_plugin_leaks_the_repository_dotenv_into_the_test_process(name: str) -> None:
    """Finding 9: `deepeval` ships an entry-point pytest plugin whose import calls python-dotenv.

    Every ``pytest`` invocation therefore loaded this repository's ``.env`` into ``os.environ``
    before collection, which opened the live eval tier and made ``uv run pytest`` place billable
    third-party API calls with nothing exported and no flag passed.

    Asserted over the real process rather than through a fixture: this reads the environment the
    suite is actually running in, so the day another dependency calls ``load_dotenv`` on import it
    goes red here rather than on the operator's bill. A fixture that scrubbed the environment
    first would only test the fixture.
    """
    from_file = conftest.DOTENV_AT_START.get(name)
    if from_file is None:
        pytest.skip(f"{name} is not in this machine's .env, so it cannot leak from it")
    assert os.environ.get(name) != from_file, (
        f"{name} reached os.environ carrying the value this repository's .env sets. "
        "Something loaded the dotenv file into the test process."
    )


def test_the_live_eval_tier_is_shut_when_nothing_was_exported() -> None:
    """The consequence those variables cause, checked where it is actually decided.

    ``evals.helpers`` computes ``RUN_LIVE`` and ``HAS_KEY`` from ``os.environ`` at import. The
    assertion above is the mechanism and this is the outcome; both are kept, because a later
    change could break one without the other.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from evals import helpers

    if os.environ.get("HARNESSED_RUN_LIVE_EVALS", "").strip() == "1":
        pytest.skip("the operator exported the live flag deliberately; an export must still win")
    assert helpers.RUN_LIVE is False
    assert "the live eval tier is off" in helpers.LIVE_REASON
