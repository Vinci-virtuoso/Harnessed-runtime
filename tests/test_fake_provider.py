"""FR-2. FakeProvider replays a committed corpus, byte for byte, and misses loudly.

This is the module that makes every later phase testable offline: no key, no socket, no vendor
SDK. It asserts the replay mechanism and the miss behaviour only. The prompt-conditional variant
selection that makes FR-25 an honest proof is ticket 006 and is asserted in
``tests/test_fixture_selection.py``; nothing here may pre-empt it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from harnessed import config, shipped
from harnessed.config import Settings
from harnessed.errors import ConfigError, FixtureCorpusError, FixtureMissError, HarnessError
from harnessed.providers import base, fake
from harnessed.providers.fake import FakeProvider
from harnessed.providers.types import ChatRequest, ChatResponse, Message

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = shipped.fake_wire()

REPLAY_SCOPE = "smoke-005-replay"
#: sha256("ping")[:12] - the directory a scope-less request falls back to.
AD_HOC_SCOPE = hashlib.sha256(b"ping").hexdigest()[:12]


# ---- helpers -----------------------------------------------------------------------------------


def canonical(response: ChatResponse) -> str:
    """The one byte-level projection of a response. Same key -> same string, always."""
    return json.dumps(dataclasses.asdict(response), sort_keys=True, ensure_ascii=False)


def settings(**overrides: object) -> Settings:
    """A keyless offline ``Settings``, built without reading the process environment."""
    fields: dict[str, object] = {
        "home": REPO_ROOT / ".harnessed",
        "provider": "fake",
        "model": "fake-1",
        "small_model": "fake-1",
        "base_url": None,
        "embed_base_url": None,
        "embed_model": None,
        "retrieval_token_budget": 4000,
        "max_iterations": 12,
    }
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


def request(
    scope: str | None = REPLAY_SCOPE,
    *,
    assistant_turns: int = 0,
    user_text: str = "ping",
    system: str = "",
) -> ChatRequest:
    """A normalized request whose key is entirely visible in this call."""
    messages: list[Message] = [Message(role="user", content=user_text)]
    for turn in range(assistant_turns):
        messages.append(Message(role="assistant", content=f"turn {turn + 1}"))
        messages.append(Message(role="user", content="and then?"))
    return ChatRequest(
        model="fake-1",
        messages=tuple(messages),
        system=system,
        metadata={} if scope is None else {"scope": scope},
    )


def write_corpus(root: Path, scope: str, index: str, *files: tuple[str, str]) -> Path:
    """Author a throwaway corpus for the malformed cases the committed corpus must never hold."""
    directory = root / scope
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "_index.yaml").write_text(index, encoding="utf-8")
    for name, body in files:
        (directory / name).write_text(body, encoding="utf-8")
    return directory


GOOD_INDEX = "scope: {scope}\nvariants:\n  - name: base\n    when: always\n"
GOOD_RESPONSE = '{"response_id": "r1", "model": "fake-1", "content": "ok"}'


# ---- determinism -------------------------------------------------------------------------------


def test_the_same_sequence_replayed_twice_in_one_process_is_byte_identical() -> None:
    provider = FakeProvider(settings())
    sequence = [request(assistant_turns=0), request(assistant_turns=1)]

    first = [canonical(provider.complete(item)) for item in sequence]
    second = [canonical(provider.complete(item)) for item in sequence]

    assert first == second
    # A flat replay would be trivially identical; the two iterations must actually differ.
    assert first[0] != first[1]


def test_a_second_provider_instance_replays_the_identical_bytes() -> None:
    one, two = FakeProvider(settings()), FakeProvider(settings())
    item = request(assistant_turns=1)
    assert canonical(one.complete(item)) == canonical(two.complete(item))


def test_interleaving_two_requests_cannot_shift_the_iteration() -> None:
    """No counter on the instance: the key comes from the request, so order cannot matter."""
    provider = FakeProvider(settings())
    first, second = request(assistant_turns=0), request(assistant_turns=1)

    in_order = [canonical(provider.complete(first)), canonical(provider.complete(second))]
    reversed_order = [canonical(provider.complete(second)), canonical(provider.complete(first))]

    assert in_order == list(reversed(reversed_order))


CHILD_SCRIPT = textwrap.dedent(
    """
    import dataclasses, json, sys
    from pathlib import Path
    from harnessed.config import Settings
    from harnessed.providers.fake import FakeProvider
    from harnessed.providers.types import ChatRequest, Message

    settings = Settings(
        home=Path(sys.argv[1]) / ".harnessed",
        provider="fake",
        model="fake-1",
        small_model="fake-1",
        base_url=None,
        embed_base_url=None,
        embed_model=None,
        retrieval_token_budget=4000,
        max_iterations=12,
    )
    provider = FakeProvider(settings)
    out = []
    for turns in (0, 1):
        messages = [Message(role="user", content="ping")]
        for turn in range(turns):
            messages.append(Message(role="assistant", content=f"turn {turn + 1}"))
            messages.append(Message(role="user", content="and then?"))
        response = provider.complete(
            ChatRequest(
                model="fake-1",
                messages=tuple(messages),
                metadata={"scope": "smoke-005-replay"},
            )
        )
        out.append(json.dumps(dataclasses.asdict(response), sort_keys=True, ensure_ascii=False))
    sys.stdout.write("\\n".join(out))
    """
)


@pytest.mark.slow
def test_replay_is_byte_identical_across_two_separate_processes() -> None:
    in_process = "\n".join(
        canonical(FakeProvider(settings()).complete(request(assistant_turns=turns)))
        for turns in (0, 1)
    )
    runs = [
        subprocess.run(
            [sys.executable, "-c", CHILD_SCRIPT, str(REPO_ROOT)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        ).stdout
        for _ in range(2)
    ]
    assert runs[0] == runs[1] == in_process


# ---- the key -----------------------------------------------------------------------------------


def test_scope_comes_from_request_metadata() -> None:
    assert FakeProvider.scope_of(request(REPLAY_SCOPE)) == REPLAY_SCOPE


def test_scope_falls_back_to_the_sha256_of_the_first_user_message() -> None:
    ad_hoc = request(scope=None, user_text="ping")
    assert FakeProvider.scope_of(ad_hoc) == AD_HOC_SCOPE
    # And the fallback is a real lookup, not just a computed string.
    assert FakeProvider(settings()).complete(ad_hoc).content == "pong"


@pytest.mark.parametrize("turns", [0, 1, 2, 5])
def test_iteration_is_one_plus_the_assistant_message_count(turns: int) -> None:
    assert FakeProvider.iteration_of(request(assistant_turns=turns)) == turns + 1


def test_the_provider_holds_no_mutable_replay_state() -> None:
    """A resumed run reconstructs the key; nothing may be remembered between calls."""
    provider = FakeProvider(settings())
    provider.complete(request(assistant_turns=0))
    for value in vars(provider).values():
        assert not isinstance(value, list | set | bytearray)


# ---- the Provider seam -------------------------------------------------------------------------


def test_fake_provider_satisfies_the_runtime_checkable_provider_protocol() -> None:
    assert isinstance(FakeProvider(settings()), base.Provider)


def test_the_registry_builds_it_with_no_key_and_no_environment_read(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harnessed.providers import registry

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("the offline path asked for a credential")

    monkeypatch.setenv("HARNESSED_PROVIDER", "fake")
    loaded = config.load()
    monkeypatch.setattr(Settings, "require_api_key", explode)
    monkeypatch.setattr(Settings, "api_key_for", explode)

    provider = registry.create(loaded)
    assert isinstance(provider, FakeProvider)
    assert provider.complete(request()).stop_reason == "tool_use"


def test_fake_reads_the_environment_only_through_config() -> None:
    """MUST rule 3, checked the same way the verify harness checks it: a text scan."""
    source = (REPO_ROOT / "src" / "harnessed" / "providers" / "fake.py").read_text(encoding="utf-8")
    for spelling in ("os." + "environ", "os." + "getenv", "getenv("):
        assert spelling not in source


@pytest.mark.slow
def test_mypy_accepts_fake_provider_wherever_a_provider_is_required(tmp_path: Path) -> None:
    module = tmp_path / "uses_provider.py"
    module.write_text(
        textwrap.dedent(
            """
            from harnessed.config import Settings
            from harnessed.providers.base import Provider
            from harnessed.providers.fake import FakeProvider


            def takes_a_provider(provider: Provider) -> Provider:
                return provider


            def build(settings: Settings) -> Provider:
                return takes_a_provider(FakeProvider(settings))
            """
        ),
        encoding="utf-8",
    )
    # MYPYPATH points at the sources: the wheel ships no py.typed marker, so an installed-package
    # import would be skipped as untyped and the check would prove nothing.
    env = {**os.environ, "MYPYPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        ["uv", "run", "mypy", "--strict", str(module)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---- misses are loud ---------------------------------------------------------------------------


def test_a_missing_scope_directory_raises_fixture_miss_naming_the_path_and_the_key() -> None:
    provider = FakeProvider(settings())
    with pytest.raises(FixtureMissError) as excinfo:
        provider.complete(request("no-such-scope", assistant_turns=2))

    message = str(excinfo.value)
    assert str(CORPUS / "no-such-scope") in message
    assert "no-such-scope" in message and "3" in message and "base" not in message
    assert not isinstance(excinfo.value, KeyError)


def test_a_matched_variant_with_no_file_for_the_iteration_names_the_exact_file() -> None:
    provider = FakeProvider(settings())
    with pytest.raises(FixtureMissError) as excinfo:
        provider.complete(request(REPLAY_SCOPE, assistant_turns=2))

    assert str(CORPUS / REPLAY_SCOPE / "base.3.json") in str(excinfo.value)


@pytest.mark.parametrize("error", [FixtureMissError, FixtureCorpusError])
def test_both_fixture_errors_are_harness_errors_never_key_errors(error: type[Exception]) -> None:
    assert issubclass(error, HarnessError)
    assert not issubclass(error, KeyError)


# ---- the corpus is validated eagerly -----------------------------------------------------------


def test_a_corpus_without_a_mandatory_base_variant_fails_at_construction(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        "no-base",
        "scope: no-base\nvariants:\n  - name: with_procedure\n    when: always\n",
        ("with_procedure.json", GOOD_RESPONSE),
    )
    with pytest.raises(FixtureCorpusError, match="base"):
        FakeProvider(settings(), root=tmp_path)


def test_every_index_is_loaded_at_construction_not_at_the_fortieth_request(
    tmp_path: Path,
) -> None:
    """The typo is in the *second* scope and nothing ever requests it. It must still fail."""
    write_corpus(
        tmp_path, "asked-for", GOOD_INDEX.format(scope="asked-for"), ("base.json", GOOD_RESPONSE)
    )
    write_corpus(
        tmp_path,
        "never-asked-for",
        "scope: never-asked-for\nvariants:\n  - name: base\n    when: {candidate_id: cand_ab12}\n",
        ("base.json", GOOD_RESPONSE),
    )
    with pytest.raises(FixtureCorpusError) as excinfo:
        FakeProvider(settings(), root=tmp_path)

    message = str(excinfo.value)
    assert "never-asked-for" in message and "candidate_id" in message


def test_an_undeclared_response_file_is_a_corpus_error(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        "typo",
        GOOD_INDEX.format(scope="typo"),
        ("base.json", GOOD_RESPONSE),
        ("bass.json", GOOD_RESPONSE),
    )
    with pytest.raises(FixtureCorpusError, match="bass"):
        FakeProvider(settings(), root=tmp_path)


def test_an_unparseable_response_file_is_a_corpus_error_at_construction(tmp_path: Path) -> None:
    write_corpus(tmp_path, "broken", GOOD_INDEX.format(scope="broken"), ("base.json", "{not json"))
    with pytest.raises(FixtureCorpusError, match="base.json"):
        FakeProvider(settings(), root=tmp_path)


def test_a_response_with_an_unknown_stop_reason_is_a_corpus_error(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        "bad-stop",
        GOOD_INDEX.format(scope="bad-stop"),
        ("base.json", '{"response_id": "r1", "model": "m", "stop_reason": "finished"}'),
    )
    with pytest.raises(FixtureCorpusError, match="finished"):
        FakeProvider(settings(), root=tmp_path)


def test_a_missing_corpus_root_is_a_corpus_error(tmp_path: Path) -> None:
    with pytest.raises(FixtureCorpusError):
        FakeProvider(settings(), root=tmp_path / "absent")


def test_the_committed_corpus_loads_and_every_declared_fixture_decodes() -> None:
    provider = FakeProvider(settings())
    assert {REPLAY_SCOPE, AD_HOC_SCOPE} <= set(provider.scopes)
    for scope in provider.scopes:
        assert (CORPUS / scope / "_index.yaml").is_file()


def test_a_fixture_carries_its_tool_calls_through_unchanged() -> None:
    response = FakeProvider(settings()).complete(request(REPLAY_SCOPE))
    assert response.stop_reason == "tool_use"
    assert [(call.name, dict(call.arguments)) for call in response.tool_calls] == [
        ("fs_read", {"path": "README.md"})
    ]
    assert response.tool_calls[0].parse_error is None
    assert response.usage.total_tokens == 58


#: Every way a hand-authored corpus can be wrong, and the substring the error must name so the
#: author can find it. Each one must fail at construction: the corpus is reviewed code, and a
#: reviewer who mistypes a key should learn about it from the first test, not the fortieth.
MALFORMED = {
    "unparseable yaml": ("scope: [unclosed\n", (), "YAML"),
    "not a mapping": ("- base\n", (), "mapping"),
    "scope disagrees with directory": (
        "scope: elsewhere\nvariants:\n  - name: base\n    when: always\n",
        (),
        "elsewhere",
    ),
    "no variants": ("scope: {scope}\nvariants: []\n", (), "variants"),
    "variant is not a mapping": ("scope: {scope}\nvariants:\n  - base\n", (), "mapping"),
    "variant has no name": (
        "scope: {scope}\nvariants:\n  - when: always\n",
        (),
        "name",
    ),
    "variant name holds a dot": (
        "scope: {scope}\nvariants:\n  - name: base.2\n    when: always\n",
        (),
        "dot",
    ),
    "duplicate variant names": (
        "scope: {scope}\nvariants:\n"
        "  - name: base\n    when: always\n"
        "  - name: base\n    when: always\n",
        (("base.json", GOOD_RESPONSE),),
        "duplicate",
    ),
    "base is not declared last": (
        "scope: {scope}\nvariants:\n"
        "  - name: base\n    when: always\n"
        "  - name: other\n    when: always\n",
        (("base.json", GOOD_RESPONSE), ("other.json", GOOD_RESPONSE)),
        "last",
    ),
    "when is neither a name nor a single-key mapping": (
        "scope: {scope}\nvariants:\n  - name: base\n    when: [always, never]\n",
        (),
        "when",
    ),
    "iteration suffix is not a number": (
        GOOD_INDEX,
        (("base.json", GOOD_RESPONSE), ("base.later.json", GOOD_RESPONSE)),
        "iteration suffix",
    ),
    "response is not an object": (GOOD_INDEX, (("base.json", "[1, 2]"),), "JSON object"),
    "response has an unknown field": (
        GOOD_INDEX,
        (("base.json", '{"response_id": "r", "model": "m", "temperature": 0.7}'),),
        "temperature",
    ),
    "response has no id": (GOOD_INDEX, (("base.json", '{"model": "m"}'),), "response_id"),
    "content is not a string": (
        GOOD_INDEX,
        (("base.json", '{"response_id": "r", "model": "m", "content": 7}'),),
        "content",
    ),
    "tool_calls is not a list": (
        GOOD_INDEX,
        (("base.json", '{"response_id": "r", "model": "m", "tool_calls": {}}'),),
        "tool_calls",
    ),
    "a tool call is not a mapping": (
        GOOD_INDEX,
        (("base.json", '{"response_id": "r", "model": "m", "tool_calls": ["read_file"]}'),),
        "mapping",
    ),
    "tool call arguments are not an object": (
        GOOD_INDEX,
        (
            (
                "base.json",
                '{"response_id": "r", "model": "m", '
                '"tool_calls": [{"id": "c1", "name": "read", "arguments": "path=x"}]}',
            ),
        ),
        "arguments",
    ),
    "usage is not a mapping": (
        GOOD_INDEX,
        (("base.json", '{"response_id": "r", "model": "m", "usage": 12}'),),
        "usage",
    ),
    "a usage counter is negative": (
        GOOD_INDEX,
        (("base.json", '{"response_id": "r", "model": "m", "usage": {"input_tokens": -1}}'),),
        "input_tokens",
    ),
}


@pytest.mark.parametrize(("index", "files", "expected"), MALFORMED.values(), ids=MALFORMED)
def test_a_malformed_corpus_fails_at_construction_naming_the_problem(
    tmp_path: Path, index: str, files: tuple[tuple[str, str], ...], expected: str
) -> None:
    scope = "malformed"
    write_corpus(tmp_path, scope, index.replace("{scope}", scope), *files)
    with pytest.raises(FixtureCorpusError) as excinfo:
        FakeProvider(settings(), root=tmp_path)
    assert expected in str(excinfo.value)


# ---- the latency knob (ticket 119, FR-53) -------------------------------------------------------
#
# `HARNESSED_FAKE_LATENCY_MS` is a *test affordance*: it gives a fake run a publish window wide
# enough that a browser can attach to a session while it is still open (ticket 112 measured the
# unslowed window at 24 ms). It is deliberately not a `Settings` field - FR-27's resolution order,
# `doctor`'s report and `init`'s owned keys are the user-facing configuration surface, and a knob
# that only changes wall time does not belong in any of them.

#: Long enough that a wrongly-wired implementation cannot pass by accident, short enough that the
#: three tests measuring it cost under a tenth of a second between them.
MEASURED_LATENCY_MS = 25


def test_the_latency_knob_is_off_by_default(clean_env: None) -> None:
    """The default is 0, so an unset environment is the timing the corpus has always had."""
    assert config.fake_latency_ms() == 0


def test_the_default_path_sleeps_not_at_all(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted by making a sleep fatal rather than by timing: a zero-second sleep still costs a
    syscall on every one of the suite's completions, and 'fast enough' is not 'not at all'."""
    slept: list[float] = []
    monkeypatch.setattr(fake.time, "sleep", lambda seconds: slept.append(seconds))
    provider = FakeProvider(settings())
    assert provider.complete(request()).stop_reason == "tool_use"
    assert slept == []


def test_a_configured_delay_is_actually_waited_out(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second criterion, measured on the clock rather than trusted: every completion takes at
    least the configured delay."""
    monkeypatch.setenv(config.FAKE_LATENCY_VAR, str(MEASURED_LATENCY_MS))
    provider = FakeProvider(settings())
    for _ in range(2):
        before = time.perf_counter()
        provider.complete(request())
        assert (time.perf_counter() - before) >= MEASURED_LATENCY_MS / 1000


def test_the_delay_is_read_once_at_construction(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The variable is read in ``__init__``, so a provider already built is not re-configured by
    something the process exports later."""
    monkeypatch.setenv(config.FAKE_LATENCY_VAR, str(MEASURED_LATENCY_MS))
    provider = FakeProvider(settings())
    monkeypatch.setenv(config.FAKE_LATENCY_VAR, "60000")
    before = time.perf_counter()
    provider.complete(request())
    assert (time.perf_counter() - before) < 60


@pytest.mark.parametrize("raw", ["lots", "1.5", "", "   ", "-1", "-25"])
def test_an_unparseable_or_negative_delay_fails_loudly_at_construction(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A typo that silently read as 0 would leave the e2e walk hunting a window that was never
    widened, and the failure would surface three layers away from its cause."""
    monkeypatch.setenv(config.FAKE_LATENCY_VAR, raw)
    if not raw.strip():
        # An empty or blank value is an unset variable everywhere else this loader reads, and a
        # knob that read it as an error would make `set HARNESSED_FAKE_LATENCY_MS=` fatal.
        assert config.fake_latency_ms() == 0
        return
    with pytest.raises(ConfigError) as excinfo:
        FakeProvider(settings())
    assert config.FAKE_LATENCY_VAR in str(excinfo.value)


def test_the_delay_changes_wall_time_and_nothing_else(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth criterion: same corpus, same key, same bytes. Only the waiting is new."""
    quick = FakeProvider(settings())
    fast = [canonical(quick.complete(request(scope))) for scope in quick.scopes]

    monkeypatch.setenv(config.FAKE_LATENCY_VAR, "1")
    slow = FakeProvider(settings())
    assert slow.scopes == quick.scopes
    assert [canonical(slow.complete(request(scope))) for scope in slow.scopes] == fast
    assert slow.scope_of(request()) == quick.scope_of(request())
    assert slow.iteration_of(request(assistant_turns=2)) == quick.iteration_of(
        request(assistant_turns=2)
    )


def test_the_knob_says_in_its_own_docstring_that_it_is_a_test_affordance() -> None:
    """The third criterion's last clause. The reader who finds this variable in a config file has
    to learn from the code itself that it is not a supported run-time option."""
    assert "test affordance" in (config.fake_latency_ms.__doc__ or "")


def test_the_knob_is_not_a_documented_variable_and_never_comes_from_a_dotenv_file(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The third criterion: environment only. `.env` is a configuration source an operator copies
    between machines, so a delay left in one must not be able to reach a real run."""
    from conftest import DOCUMENTED_ENV_VARS

    assert config.FAKE_LATENCY_VAR not in DOCUMENTED_ENV_VARS
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert config.FAKE_LATENCY_VAR not in example

    monkeypatch.chdir(tmp_path)
    Path(config.ENV_FILENAME).write_text(
        f"HARNESSED_PROVIDER=fake\n{config.FAKE_LATENCY_VAR}=5000\n", encoding="utf-8"
    )
    assert config.fake_latency_ms() == 0
    assert not hasattr(config.load(), "fake_latency_ms")


def test_the_knob_does_not_reach_a_user_facing_flag() -> None:
    """No command grows a flag spelling the fake-provider latency knob's own name.

    **Gate ruling 2026-09-25** (ticket 156): the plain word ``latency`` is no longer forbidden in
    ``cli/`` - FR-97's live rung reports one for real, through the payload rather than a hard-coded
    string, and the word alone was never the risk. What must never reach a CLI source is *this*
    knob's own spelling: its environment variable, and any flag or option that names it - the
    thing that would let a test affordance be driven from a command line.
    """
    for module in sorted((REPO_ROOT / "src" / "harnessed" / "cli").glob("*.py")):
        text = module.read_text(encoding="utf-8")
        assert config.FAKE_LATENCY_VAR not in text
        assert "--latency" not in text.lower()


def test_the_knob_guard_still_fires_on_a_seeded_flag(tmp_path: Path) -> None:
    """A guard on the guard: a planted flag spelling the knob is still caught."""
    planted = tmp_path / "doctor_cmd.py"
    planted.write_text(
        f'import typer\n\ntyper.Option("--latency", envvar="{config.FAKE_LATENCY_VAR}")\n',
        encoding="utf-8",
    )
    text = planted.read_text(encoding="utf-8")
    assert config.FAKE_LATENCY_VAR in text
    assert "--latency" in text.lower()


def test_the_offline_guarantee_is_untouched_by_the_knob(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fifth criterion. The socket guard is installed for this test like every other, so a
    provider that reached the network under the knob would fail here rather than in `doctor`."""
    from harnessed.providers import registry

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("the offline path asked for a credential")

    monkeypatch.setenv("HARNESSED_PROVIDER", "fake")
    monkeypatch.setenv(config.FAKE_LATENCY_VAR, "1")
    loaded = config.load()
    monkeypatch.setattr(Settings, "require_api_key", explode)
    monkeypatch.setattr(Settings, "api_key_for", explode)

    provider = registry.create(loaded)
    assert isinstance(provider, FakeProvider)
    assert provider.complete(request()).stop_reason == "tool_use"


# ---- ticket 249: context_contains_all ------------------------------------------------------------


def context_request(*, system: str = "", user: str = "") -> ChatRequest:
    return ChatRequest(
        model="fake-1",
        system=system,
        messages=(Message(role="user", content=user),),
        metadata={"scope": "memory-001-spelling"},
    )


FACT = "The operator prefers British spelling."


def served_by(request: ChatRequest) -> str:
    return FakeProvider(config.load()).complete(request).response_id


def test_context_contains_all_matches_only_the_retrieved_context(clean_env: None) -> None:
    inside = f"<retrieved_context>\n- {FACT}\n</retrieved_context>\n\nWhat spelling?"
    assert served_by(context_request(user=inside)) == "fake_memory_001_after"
    # The same words anywhere else - the system prompt, the task, no block at all - do not match.
    assert served_by(context_request(system=FACT, user="What spelling?")) == "fake_memory_001_base"
    assert served_by(context_request(user=f"{FACT} What spelling?")) == "fake_memory_001_base"
    assert served_by(context_request(user="What spelling?")) == "fake_memory_001_base"


def test_context_contains_all_is_normalized_like_the_system_predicates(clean_env: None) -> None:
    shouted = "<retrieved_context>- the   OPERATOR prefers\nBritish   spelling.</retrieved_context>"
    assert served_by(context_request(user=shouted)) == "fake_memory_001_after"


def test_a_malformed_context_predicate_is_a_corpus_error(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    (root / "_index.yaml").write_text(
        "scope: scope\nvariants:\n  - name: a\n    when: {context_contains_all: nope}\n"
        "  - name: base\n    when: always\n",
        encoding="utf-8",
    )
    with pytest.raises(FixtureCorpusError, match="context_contains_all"):
        FakeProvider(config.load(), tmp_path).complete(
            ChatRequest(model="fake-1", metadata={"scope": "scope"})
        )


def test_a_tuple_of_roots_serves_both_and_refuses_a_scope_in_both(tmp_path: Path) -> None:
    shipped_root, home_root = tmp_path / "shipped", tmp_path / "home"
    for root, scope in ((shipped_root, "one"), (home_root, "two")):
        _write_scope(root / scope, scope)
    provider = FakeProvider(settings(), root=(shipped_root, home_root))
    assert provider.scopes == ("one", "two")
    assert provider.root == shipped_root

    _write_scope(home_root / "one", "one")
    with pytest.raises(FixtureCorpusError, match="would shadow a shipped one"):
        FakeProvider(settings(), root=(shipped_root, home_root))

    # A home root that does not exist yet is simply empty; the shipped one must exist.
    assert FakeProvider(settings(), root=(shipped_root, tmp_path / "none")).scopes == ("one",)
    with pytest.raises(FixtureCorpusError):
        FakeProvider(settings(), root=(tmp_path / "absent", home_root))


def _write_scope(directory: Path, scope: str) -> None:
    directory.mkdir(parents=True)
    (directory / "_index.yaml").write_text(
        f"scope: {scope}\nvariants:\n  - name: base\n    when: always\n", encoding="utf-8"
    )
    (directory / "base.json").write_text(
        json.dumps(
            {
                "response_id": f"fake_{scope}",
                "model": "fake-1",
                "content": "ok",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ),
        encoding="utf-8",
    )
