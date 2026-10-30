"""AC 7 - ``make_harness`` builds one session through the composition root that already exists.

The whole reason this suite lives outside the package is that it may call
``cli/run_cmd.py::open_session``. So it calls it. There is no second composition root here, and a
test that finds one has found a bug: the session under test must be the same object shape
``harnessed run`` builds, or nothing the deterministic tier asserts is about the real harness.

Three properties, and all three are about isolation rather than about the loop:

* the session is sandboxed to the ``tmp_path`` home it was handed;
* the operator's ``.env`` is never consulted, because the provider arrives as the ``--provider``
  flag and :func:`harnessed.config._resolve_provider` short-circuits before the file arm;
* every optional switch is off unless the caller named it in ``keep``.

There is no live tier in this file. A factory has no live counterpart; what it builds does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import helpers
from evals.helpers import (
    DOCUMENTED_ENV_VARS,
    MAX_ITERATIONS,
    OFFLINE_PROVIDER,
    ScriptedClient,
    make_harness,
    text_response,
)
from harnessed import config
from harnessed.cli.run_cmd import Session
from harnessed.errors import ProviderNotConfiguredError
from harnessed.providers.types import ToolCall
from harnessed.tools.builtin.delegate import TOOL_NAME as DELEGATE_TOOL

# ---- offline tier ------------------------------------------------------------------------------


def test_make_harness_builds_the_session_the_composition_root_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_harness(tmp_path / "home", monkeypatch)
    assert isinstance(session, Session)
    assert session.settings.provider == OFFLINE_PROVIDER
    assert session.tools.names() != ()


def test_the_session_is_sandboxed_to_the_home_it_was_handed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    session = make_harness(home, monkeypatch)
    assert session.settings.home == home.resolve()
    assert (home / "harness.db").is_file()


def test_a_scripted_client_is_injected_without_a_second_composition_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ScriptedClient([text_response("hello")])
    session = make_harness(tmp_path / "home", monkeypatch, client=client)
    assert session.provider is client
    assert session.tools.names() != ()


def test_the_operators_dotenv_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoy ``.env`` in the working directory must not decide the provider.

    ``config._resolve_provider`` is ``flag or variable or file``, so naming the provider as the
    flag means the file arm never runs. The decoy proves it rather than assuming it (learnings,
    ticket 032: a resolution arm that reads the working directory passes on a configured machine
    for the wrong reason unless the test chdirs somewhere clean and plants its own).
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / ".env").write_text("HARNESSED_PROVIDER=anthropic\n", encoding="utf-8")
    monkeypatch.chdir(decoy)
    session = make_harness(tmp_path / "home", monkeypatch)
    assert session.settings.provider == OFFLINE_PROVIDER


def test_make_harness_itself_neutralizes_the_dotenv_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray ``.env`` in the working directory must not leak a live model into the isolated
    session, even when the caller forgets to chdir (finding 7)."""
    (tmp_path / ".env").write_text("HARNESSED_MODEL=leaked-model\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = make_harness(tmp_path / "home", monkeypatch)
    assert session.settings.model is None


def test_every_optional_switch_is_pinned_off_unless_the_caller_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every documented variable is cleared, so every optional setting comes back unset.

    The chdir is load-bearing and was added in ticket 043. Clearing the environment stopped being
    enough when ticket 040 widened the environment file from the provider and the keys to *every*
    documented variable: from that commit on, this test read the developer's own file out of the
    repository root and saw a real model id where it asserts ``None``. Consistency rule 11 -
    sweep the absence-asserting tests whenever a configuration source widens - and this suite was
    outside 040's writable scope, so the sweep could not reach it.
    """
    monkeypatch.chdir(tmp_path)
    for name in DOCUMENTED_ENV_VARS:
        monkeypatch.setenv(name, "leaked")
    monkeypatch.setenv("HARNESSED_TOKEN_BUDGET", "999999")
    session = make_harness(tmp_path / "home", monkeypatch)
    assert session.settings.model is None
    assert session.settings.small_model is None
    assert session.settings.base_url is None
    assert session.settings.embed_base_url is None
    assert session.settings.embed_model is None
    assert session.settings.max_iterations == MAX_ITERATIONS


def test_a_switch_the_caller_names_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_MODEL", "some-model-id")
    session = make_harness(tmp_path / "home", monkeypatch, keep=("HARNESSED_MODEL",))
    assert session.settings.model == "some-model-id"
    assert session.model == "some-model-id"


def test_the_agent_registry_defaults_inside_the_isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delegation tool's registry path is bound explicitly, never left to the cwd.

    ``acp/config.py`` defaults it to a relative ``agents.toml``, which on a developer machine is
    the operator's own. Binding it inside the throwaway home is what makes an eval run identical
    on a configured machine and a fresh clone.
    """
    home = tmp_path / "home"
    (tmp_path / "agents.toml").write_text('[agents.leak]\ncommand = ["leak"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = make_harness(home, monkeypatch)
    outcome = session.tools.dispatch(
        ToolCall(id="call_delegate", name=DELEGATE_TOOL, arguments={"task": "x", "acceptance": "y"})
    )
    assert outcome.is_error
    assert "leak" not in outcome.content
    assert str(home.resolve()) in outcome.content


def test_asking_for_the_configured_provider_restores_it_and_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``provider=None`` is what the live tier passes, and it must survive the clearing.

    ``make_harness`` deletes every documented variable, the provider's key among them. Asking for
    "whatever the operator configured" after that, without putting the key back, raises
    ``ProviderNotConfiguredError`` - and because the live tier is skipped on a fresh clone, the
    offline suite would stay green for as long as nobody opened both gates. This is that test.
    """
    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-key")
    monkeypatch.setattr(helpers, "ACTIVE_PROVIDER", "groq")
    session = make_harness(tmp_path / "home", monkeypatch, provider=None)
    assert session.settings.provider == "groq"
    assert session.settings.api_key_for("groq") == "not-a-real-key"


def test_the_configured_provider_is_also_visible_to_anything_that_loads_settings_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The judge builds its own provider from ``config.load``, so the variable must be set too."""
    monkeypatch.setattr(helpers, "ACTIVE_PROVIDER", OFFLINE_PROVIDER)
    make_harness(tmp_path / "home", monkeypatch, provider=None)
    assert config.load().provider == OFFLINE_PROVIDER


def test_an_unconfigured_machine_asking_for_its_provider_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-27's error, not a silent fallback to the offline provider.

    Chdir'd for the same reason as the switches test above: an unconfigured machine is a machine
    with no variable *and* no environment file, and this one runs in a repository that has one.
    Without it the assertion is about the developer's own checkout rather than about FR-27.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(helpers, "ACTIVE_PROVIDER", None)
    with pytest.raises(ProviderNotConfiguredError):
        make_harness(tmp_path / "home", monkeypatch, provider=None)
