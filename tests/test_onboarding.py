"""FR-87: validate-then-write lives in ``cli/onboarding.py``, and ``init`` is its first caller.

``configure_provider`` is the one composition behind two callers (MUST rule 16): ``harnessed init``
today and the control app's providers route next (ticket 146). It holds ``init``'s three rules
verbatim - merge, never rewrite; write nothing until the credential is proven; never print or
return what was typed - and adds the ``chmod 0600`` on POSIX that ``init`` lacked.

``tests/test_init.py`` is the proof the behaviour did not move with the code: it passes unchanged.
This file asserts the function itself, called directly the way the control app will call it.

Every test sits in its own ``tmp_path`` with the process chdir'd into it, because ``config.py``
reads ``.env`` from the working directory (MUST rule 11): a developer's own ``.env`` must not decide
anything here. The socket guard in ``conftest.py`` keeps every provider call offline.
"""

from __future__ import annotations

import difflib
import inspect
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from harnessed import config
from harnessed.cli import init_cmd, onboarding
from harnessed.cli.app import app
from harnessed.errors import ConfigError, ProviderError
from harnessed.providers import registry as provider_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / ".env.example"
EVIDENCE = REPO_ROOT / "evidence" / "145"

#: The chosen provider's variable, quoted from ``config`` rather than retyped.
KEY_VAR = config.PROVIDER_KEY_VARS["groq"]

#: What the operator typed. Distinctive, so a leak anywhere is unmissable, and obviously not real.
TYPED = "not-a-real-value-27182818284590452353"

#: What a vendor SDK might say when it refuses: it is free to echo the credential back.
SDK_MESSAGE = f"invalid credential {TYPED} rejected by upstream"

runner = CliRunner()


def evidence(name: str, text: str) -> None:
    """Write one evidence file. Absolute, because every test runs chdir'd into ``tmp_path``."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(text, encoding="utf-8")


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> Iterator[Path]:
    """A checkout with ``.env.example``, no ``.env``, and the process sitting in it."""
    (tmp_path / ".env.example").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def configure(clone: Path, provider: str = "groq", **kwargs: Any) -> dict[str, Any]:
    """``configure_provider`` called the way both callers call it, against ``clone``'s files."""
    options: dict[str, Any] = {"validate": False, "env_path": clone / ".env"}
    options.update(kwargs)
    return onboarding.configure_provider(
        provider,
        None if config.PROVIDER_KEY_VARS.get(provider) is None else TYPED,
        example_path=clone / ".env.example",
        **options,
    )


class Refused(ProviderError):
    """A refusal in the ``Provider`` protocol's shape, carrying the SDK's echoing message."""

    status_code = 401


class RefusedWithoutStatus(ProviderError):
    """A refusal that carries no HTTP status, so the status word is the class name."""


def refusing(error: type[ProviderError]) -> Any:
    def create(_settings: config.Settings) -> object:
        class Refuses:
            def complete(self, request: object) -> object:
                raise error(SDK_MESSAGE)

        return Refuses()

    return create


def accepting_after(check: Any) -> Any:
    """A provider whose one call runs ``check`` first, so a test can look at disk mid-validation."""

    def create(_settings: config.Settings) -> object:
        class Accepts:
            def complete(self, request: object) -> object:
                check()
                return object()

        return Accepts()

    return create


# ---------------------------------------------------------------------------
# 1. The signature
# ---------------------------------------------------------------------------


def test_the_signature_takes_provider_and_credential_then_keyword_only_options() -> None:
    signature = inspect.signature(onboarding.configure_provider)
    params = signature.parameters
    positional = [p.name for p in params.values() if p.kind is p.POSITIONAL_OR_KEYWORD]
    keyword_only = [p.name for p in params.values() if p.kind is p.KEYWORD_ONLY]

    assert positional == ["provider", "credential"]
    assert keyword_only[:2] == ["validate", "env_path"]
    evidence("signature.txt", f"configure_provider{signature}\n")


# ---------------------------------------------------------------------------
# 2. Merge, never rewrite
# ---------------------------------------------------------------------------


def test_an_existing_env_is_merged_and_every_other_line_survives_byte_for_byte(clone: Path) -> None:
    before = (
        "# the operator's own note\nUNRELATED=some value  # trailing\nHARNESSED_PROVIDER=fake\n"
    )
    (clone / ".env").write_bytes(before.encode("utf-8"))

    configure(clone)

    after = (clone / ".env").read_bytes().decode("utf-8")
    assert after.splitlines()[:2] == before.splitlines()[:2]
    assert after.splitlines()[2] == "HARNESSED_PROVIDER=groq"
    assert config.parse_env_text(after)[KEY_VAR] == TYPED


def test_a_fresh_clone_is_seeded_from_the_example_file(clone: Path) -> None:
    configure(clone)
    written = (clone / ".env").read_text(encoding="utf-8").splitlines()
    example = EXAMPLE.read_text(encoding="utf-8").splitlines()
    comments = [line for line in example if line[:1] == "#"]
    assert comments
    assert [line for line in written if line[:1] == "#"] == comments


def test_a_second_call_reports_unchanged_and_writes_nothing(clone: Path) -> None:
    configure(clone)
    first = (clone / ".env").read_bytes()
    stamp = (clone / ".env").stat().st_mtime_ns

    result = configure(clone)

    assert result["unchanged"] is True
    assert (clone / ".env").read_bytes() == first
    assert (clone / ".env").stat().st_mtime_ns == stamp


# ---------------------------------------------------------------------------
# 3. Nothing written until the credential is proven
# ---------------------------------------------------------------------------


def test_validation_runs_before_the_first_byte_reaches_disk(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(
        provider_registry, "create", accepting_after(lambda: seen.append((clone / ".env").exists()))
    )

    configure(clone, validate=True)

    assert seen == [False]
    assert (clone / ".env").exists()


@pytest.mark.parametrize(
    ("error", "status"), [(Refused, "401"), (RefusedWithoutStatus, "RefusedWithoutStatus")]
)
def test_a_refused_credential_writes_nothing_and_carries_a_status_word_not_the_sdk_message(
    error: type[ProviderError],
    status: str,
    clone: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = b"UNRELATED=keep me\r\n# a comment\n"
    (clone / ".env").write_bytes(before)
    monkeypatch.setattr(provider_registry, "create", refusing(error))

    with pytest.raises(ProviderError) as refusal:
        configure(clone, validate=True)

    message = str(refusal.value)
    assert (clone / ".env").read_bytes() == before
    assert status in message
    assert SDK_MESSAGE not in message
    assert TYPED not in message
    assert TYPED not in capsys.readouterr().out
    if error is Refused:
        evidence("refusal.txt", f"{message}\n.env byte-identical: {before!r}\n")


def test_a_refusal_on_a_fresh_clone_creates_no_env(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_registry, "create", refusing(Refused))
    with pytest.raises(ProviderError):
        configure(clone, validate=True)
    assert not (clone / ".env").exists()


def test_a_keyed_provider_without_a_credential_is_refused_without_writing(clone: Path) -> None:
    with pytest.raises(ConfigError) as refusal:
        onboarding.configure_provider(
            "groq",
            "  ",
            validate=False,
            env_path=clone / ".env",
            example_path=clone / ".env.example",
        )
    assert KEY_VAR in str(refusal.value)
    assert not (clone / ".env").exists()


def test_an_unknown_provider_is_refused_before_anything_is_written(clone: Path) -> None:
    with pytest.raises(ConfigError) as refusal:
        configure(clone, provider="grok")
    assert "groq" in str(refusal.value)
    assert not (clone / ".env").exists()


# ---------------------------------------------------------------------------
# 4. The return is init --json's payload, and never what was typed
# ---------------------------------------------------------------------------


def test_the_return_is_the_payload_init_json_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    """Two fresh clones, one through each caller: the same state yields the same object."""
    payloads = []
    for name, call in (("function", "direct"), ("command", "init")):
        home = tmp_path / name
        home.mkdir()
        (home / ".env.example").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.chdir(home)
        if call == "direct":
            payloads.append(configure(home))
        else:
            argv = ["init", "--provider", "groq", "--no-validate", "--json"]
            result = runner.invoke(app, argv, input=f"{TYPED}\n")
            assert result.exit_code == 0, result.output
            payloads.append(json.loads(result.stdout))

    assert payloads[0] == payloads[1]
    assert payloads[0]["credential_var"] == KEY_VAR
    assert payloads[0]["credential_live"] is True


def test_the_credential_is_absent_from_the_return_and_from_everything_but_its_own_line(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (clone / ".env.example").read_text(encoding="utf-8")

    result = configure(clone)

    assert TYPED not in repr(result)
    assert TYPED not in json.dumps(result)
    captured = capsys.readouterr()
    assert TYPED not in captured.out + captured.err

    after = (clone / ".env").read_text(encoding="utf-8")
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0))
    carrying = [line for line in diff if TYPED in line]
    assert carrying == [f"+{KEY_VAR}={TYPED}"]

    elsewhere = [
        path.name
        for path in clone.rglob("*")
        if path.is_file() and path.name != ".env" and TYPED in path.read_text(encoding="utf-8")
    ]
    assert elsewhere == []

    redacted = "\n".join(line.replace(TYPED, "<typed>") for line in diff)
    evidence("credential-sweep.txt", f"repr(result) = {result!r}\n\nwritten diff:\n{redacted}\n")


def test_a_keyless_provider_writes_only_the_provider(clone: Path) -> None:
    result = configure(clone, provider="ollama")
    assert result["credential_var"] is None
    assert result["credential_live"] is True
    assert set(config.parse_env_text((clone / ".env").read_text(encoding="utf-8"))) == {
        "HARNESSED_PROVIDER"
    }


# ---------------------------------------------------------------------------
# 5. chmod 0600 on POSIX, skipped with a reason on Windows
# ---------------------------------------------------------------------------


def test_posix_restricts_the_written_env_to_its_owner(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch is asserted on any host: the platform flag is the module's, not the host's."""
    calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(onboarding, "POSIX", True)
    monkeypatch.setattr(onboarding.os, "chmod", lambda path, mode: calls.append((Path(path), mode)))

    configure(clone)

    assert calls == [(clone / ".env", 0o600)]


@pytest.mark.skipif(
    os.name != "posix", reason="a real mode bit is only observable on POSIX; ci: linux job"
)
def test_posix_mode_bits_on_disk_are_0600(clone: Path) -> None:
    configure(clone)
    mode = (clone / ".env").stat().st_mode & 0o777
    assert mode == 0o600
    evidence("mode-posix.txt", f".env mode {oct(mode)}\n")


def test_windows_skips_the_mode_call_and_says_why(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(onboarding, "POSIX", False)
    monkeypatch.setattr(onboarding.os, "chmod", lambda *args: calls.append(args))

    configure(clone)

    assert calls == []
    reason = onboarding.restrict(clone / ".env")
    assert reason == onboarding.MODE_SKIPPED
    assert "Windows" in reason
    assert calls == []
    evidence("mode-windows.txt", f"chmod calls: {calls}\nskipped because: {reason}\n")


def test_the_host_flag_matches_the_host() -> None:
    assert onboarding.POSIX is (os.name == "posix")


# ---------------------------------------------------------------------------
# 6. init is the first caller, and config.py stays the only reader
# ---------------------------------------------------------------------------


def test_init_calls_configure_provider(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = onboarding.configure_provider
    calls: list[str] = []

    def spy(provider: str, credential: str | None, **kwargs: Any) -> dict[str, Any]:
        calls.append(provider)
        return original(provider, credential, **kwargs)

    monkeypatch.setattr(onboarding, "configure_provider", spy)
    argv = ["init", "--provider", "groq", "--no-validate", "--json"]
    result = runner.invoke(app, argv, input=f"{TYPED}\n")

    assert result.exit_code == 0, result.output
    assert calls == ["groq"]
    evidence("init-calls-configure-provider.txt", f"calls: {calls}\nstdout: {result.stdout}")


def test_init_keeps_no_write_of_its_own() -> None:
    """The write moved; a second copy left behind in ``init`` is two compositions again."""
    source = inspect.getsource(init_cmd)
    assert "write_bytes" not in source
    assert "merge_env_text" not in source
    assert "validate_credential" not in source


def test_onboarding_reads_no_environment_itself() -> None:
    """``config.py`` is called, never duplicated (MUST rule 3)."""
    source = inspect.getsource(onboarding)
    assert "os.environ" not in source
    assert "getenv" not in source
    assert "config.merge_env_text" in source
    assert "config.load()" in source


# ---------------------------------------------------------------------------
# 6. The file-mode seam describes itself (ticket 322, decision 95)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("posix", "implementation", "reason"),
    [
        (True, "chmod 0600", None),
        (False, "skipped", onboarding.MODE_SKIPPED),
    ],
)
def test_the_file_mode_line_names_its_choice_on_each_os(
    monkeypatch: pytest.MonkeyPatch, posix: bool, implementation: str, reason: str | None
) -> None:
    monkeypatch.setattr(onboarding, "POSIX", posix)

    line = onboarding.describe()

    assert (line.seam, line.implementation, line.reason) == ("file mode", implementation, reason)
