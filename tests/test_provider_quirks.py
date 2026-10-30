"""FR-9: endpoint differences are flags, and a missing key reads like a sentence.

Two claims are under test here and both are structural rather than behavioural:

1. Adding an OpenAI-compatible endpoint costs one entry in ``providers/quirks.py`` plus one
   fixture, and nothing else in the repository moves. ``loop/engine.py`` is fingerprinted before
   and after the addition to make "zero changes to the loop" a measurement.
2. A live adapter constructed without its key raises ``MissingCredentialError`` naming the exact
   variable and the line to add to ``.env``. Never a ``KeyError``, never a raw 401.

Every test is offline. The adapters are constructed with a throwaway key and never used, and the
session socket guard fails any test that tries to open a connection.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from harnessed import config, errors
from harnessed.config import Settings
from harnessed.providers import anthropic as anthropic_adapter
from harnessed.providers import base, openai_compat, quirks, registry, translate, types

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "harnessed"
PROVIDERS_ROOT = SRC_ROOT / "providers"
QUIRKS_SOURCE = PROVIDERS_ROOT / "quirks.py"
# Decision 57's separate vendor seam: the Decisions API happens to live at the same hostname as
# the openrouter.ai chat endpoint, but jev.py naming it is not the FR-9 quarantine this scan
# guards - that seam is asserted for on its own terms in test_jev.py and test_boundaries.py.
JEV_SOURCE = PROVIDERS_ROOT / "jev.py"
LOOP_ROOT = SRC_ROOT / "loop"
ENGINE_SOURCE = LOOP_ROOT / "engine.py"
FIXTURES = Path(__file__).parent / "fixtures" / "wire"
EVIDENCE_DIR_154 = REPO_ROOT / "evidence" / "154"


def capture_154(name: str, text: str) -> None:
    """Write one ticket-154 evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR_154.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR_154 / name).write_text(text, encoding="utf-8")


# The drill this ticket exists to prove: one hostname entry plus one fixture is the whole cost of
# a new OpenAI-compatible endpoint. The vendor is fictional on purpose, so the fixture cannot be
# mistaken for a recording of a real endpoint and no shipped table entry depends on it.
NEW_HOSTNAME = "api.newvendor.example"
NEW_FIXTURE = FIXTURES / "newvendor" / "single_tool_call.json"
NEW_CAPS = quirks.ProviderCaps(
    dialect="openai",
    supports_parallel_tool_calls=False,
    requires_max_tokens=True,
)

# Both directions of the golden round trip, keyed by the dialect a caps entry can name.
ROUND_TRIP: Mapping[str, tuple[Callable[..., Any], Callable[..., Any]]] = {
    "openai": (translate.from_openai_response, translate.to_openai_response),
    "anthropic": (translate.from_anthropic_response, translate.to_anthropic_response),
}

LIVE_PROVIDERS = tuple(
    sorted(name for name, var in config.PROVIDER_KEY_VARS.items() if var is not None)
)
KEYLESS_PROVIDERS = tuple(
    sorted(name for name, var in config.PROVIDER_KEY_VARS.items() if var is None)
)

VENDOR_SDKS = frozenset({"openai", "anthropic"})
THROWAWAY_KEY = "not-a-real-key-0000"


def load(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def projection(response: types.ChatResponse) -> tuple[Any, ...]:
    """Everything downstream of the quarantine can observe (see ``test_providers.py``)."""
    return (
        response.response_id,
        response.model,
        response.content,
        response.stop_reason,
        response.usage,
        tuple((c.id, c.name, dict(c.arguments)) for c in response.tool_calls),
    )


def python_sources(root: Path) -> Iterator[Path]:
    """Every module under ``root``, ignoring bytecode caches."""
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def engine_fingerprint() -> str:
    """A digest of ``loop/engine.py``, or a sentinel while the loop is still unbuilt.

    The sentinel matters: this test has to mean the same thing before and after ticket 011 lands
    the engine, and "the file did not change" is the claim either way.
    """
    if not ENGINE_SOURCE.exists():
        return "absent"
    return hashlib.sha256(ENGINE_SOURCE.read_bytes()).hexdigest()


def add_the_new_endpoint() -> quirks.ProviderCaps:
    """The entire cost of a new endpoint: one entry in the hostname table."""
    table = {**quirks.ENDPOINTS, NEW_HOSTNAME: NEW_CAPS}
    return quirks.caps_for(f"https://{NEW_HOSTNAME}/v1", table=table)


def explode(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("a credential check ran for a provider that needs no key")


def string_constants(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def imported_roots(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module.split(".")[0]


# ---- a new endpoint costs one entry and one fixture --------------------------------------------


def test_the_round_trip_table_covers_every_dialect_a_caps_entry_can_name() -> None:
    assert set(ROUND_TRIP) == set(quirks.DIALECTS)


def test_a_new_endpoint_passes_the_golden_round_trip() -> None:
    caps = add_the_new_endpoint()
    decode, encode = ROUND_TRIP[caps.dialect]
    once = decode(load(NEW_FIXTURE))
    twice = decode(encode(once))
    assert projection(twice) == projection(once)
    # The provider-issued id survives verbatim, which is the part a regenerated id would break.
    assert once.tool_calls[0].id == "call_Nv7Qk2Ls9Pd4Rt6y"
    assert once.tool_calls[0].arguments == {"path": "notes/todo.md"}
    assert once.stop_reason == "tool_use"


def test_adding_a_new_endpoint_leaves_loop_engine_byte_identical() -> None:
    before = engine_fingerprint()
    caps = add_the_new_endpoint()
    decode, _ = ROUND_TRIP[caps.dialect]
    assert decode(load(NEW_FIXTURE)).model == "newvendor/nv-small"
    assert engine_fingerprint() == before


def test_the_new_endpoint_is_not_in_the_shipped_table() -> None:
    # Otherwise the drill above would be asserting nothing.
    assert NEW_HOSTNAME not in quirks.ENDPOINTS


# ---- endpoint differences are flags, never conditionals ----------------------------------------


def test_every_shipped_endpoint_entry_is_reachable_and_names_a_known_dialect() -> None:
    for hostname, caps in quirks.ENDPOINTS.items():
        assert caps.dialect in quirks.DIALECTS
        assert quirks.caps_for(f"https://{hostname}/v1") is caps


def test_every_provider_that_needs_a_key_has_a_default_base_url_in_the_table() -> None:
    for name in LIVE_PROVIDERS:
        url = quirks.base_url_for(name)
        assert url is not None, name
        assert quirks.hostname_of(url) in quirks.ENDPOINTS


def test_caps_are_keyed_on_hostname_not_on_scheme_port_or_path() -> None:
    caps = quirks.caps_for("https://api.anthropic.com/v1")
    assert quirks.caps_for("http://API.Anthropic.com:8443/v1/messages") is caps
    assert quirks.caps_for("api.anthropic.com") is caps


def test_an_unknown_endpoint_falls_back_to_the_openai_compatible_default() -> None:
    assert quirks.caps_for("https://llm.example.internal/v1") is quirks.DEFAULT_CAPS
    assert quirks.caps_for(None) is quirks.DEFAULT_CAPS
    assert quirks.DEFAULT_CAPS.dialect == "openai"


def test_the_anthropic_endpoint_differs_from_the_openai_one_only_by_flags() -> None:
    anthropic_caps = quirks.caps_for("https://api.anthropic.com")
    openai_caps = quirks.caps_for("https://api.openai.com")
    assert anthropic_caps.dialect == "anthropic"
    assert anthropic_caps.requires_max_tokens is True
    assert anthropic_caps.supports_system_role is False
    assert openai_caps.dialect == "openai"
    assert openai_caps.supports_system_role is True


def test_caps_shape_the_outbound_openai_payload() -> None:
    request = types.ChatRequest(
        model="nv-small",
        messages=(types.Message(role="user", content="hi"),),
        system="be brief",
        tools=(types.ToolSchema(name="fs_read"),),
        temperature=0.7,
    )
    full = openai_compat.shape_request(request, quirks.DEFAULT_CAPS)
    assert full["temperature"] == 0.7
    assert full["tools"]
    assert full["messages"][0]["role"] == "system"

    narrow = openai_compat.shape_request(
        request,
        quirks.ProviderCaps(dialect="openai", supports_temperature=False, supports_tools=False),
    )
    assert "temperature" not in narrow
    assert "tools" not in narrow


def test_caps_shape_the_outbound_anthropic_payload() -> None:
    request = types.ChatRequest(
        model="claude-x",
        messages=(types.Message(role="user", content="hi"),),
        system="be brief",
        temperature=0.7,
    )
    payload = anthropic_adapter.shape_request(request, quirks.caps_for("https://api.anthropic.com"))
    assert payload["system"] == "be brief"
    assert payload["max_tokens"] == request.max_tokens
    assert all(message["role"] != "system" for message in payload["messages"])


@pytest.mark.parametrize("shape", [openai_compat.shape_request, anthropic_adapter.shape_request])
def test_a_tool_name_longer_than_the_endpoint_accepts_is_a_named_error(
    shape: Callable[..., Any],
) -> None:
    request = types.ChatRequest(
        model="m",
        messages=(types.Message(role="user", content="hi"),),
        tools=(types.ToolSchema(name="fs_" + "x" * 80),),
    )
    with pytest.raises(errors.TranslationError) as excinfo:
        shape(request, quirks.ProviderCaps(max_tool_name_length=8))
    assert "8" in str(excinfo.value)


def test_an_endpoint_without_a_system_role_folds_the_instruction_into_the_first_turn() -> None:
    request = types.ChatRequest(
        model="m", messages=(types.Message(role="user", content="hi"),), system="be brief"
    )
    payload = openai_compat.shape_request(request, quirks.ProviderCaps(supports_system_role=False))
    assert [message["role"] for message in payload["messages"]] == ["user", "user"]
    assert payload["messages"][0]["content"] == "be brief"


def test_an_endpoint_without_parallel_tool_calls_says_so_on_the_wire() -> None:
    request = types.ChatRequest(
        model="m",
        messages=(types.Message(role="user", content="hi"),),
        tools=(types.ToolSchema(name="fs_read"),),
    )
    payload = openai_compat.shape_request(request, NEW_CAPS)
    assert payload["parallel_tool_calls"] is False
    assert "parallel_tool_calls" not in openai_compat.shape_request(request, quirks.DEFAULT_CAPS)


def test_an_endpoint_that_requires_a_ceiling_always_gets_one() -> None:
    request = types.ChatRequest(
        model="m", messages=(types.Message(role="user", content="hi"),), max_tokens=0
    )
    caps = quirks.caps_for("https://api.anthropic.com")
    assert anthropic_adapter.shape_request(request, caps)["max_tokens"] == (
        anthropic_adapter.FALLBACK_MAX_TOKENS
    )
    without = anthropic_adapter.shape_request(request, quirks.ProviderCaps(dialect="anthropic"))
    assert without["max_tokens"] == 0


def test_a_dialect_that_cannot_take_tools_sends_none() -> None:
    request = types.ChatRequest(
        model="m",
        messages=(types.Message(role="user", content="hi"),),
        tools=(types.ToolSchema(name="fs_read"),),
    )
    caps = quirks.ProviderCaps(
        dialect="anthropic", supports_tools=False, supports_temperature=False
    )
    payload = anthropic_adapter.shape_request(request, caps)
    assert "tools" not in payload
    assert "temperature" not in payload


def test_no_module_outside_providers_branches_on_a_provider_name_or_hostname() -> None:
    """An ``if provider.name == "anthropic"`` above the quarantine is the failure FR-9 prevents."""
    watched = frozenset(config.PROVIDER_KEY_VARS) | frozenset(quirks.ENDPOINTS)
    offenders: list[str] = []
    for path in python_sources(SRC_ROOT):
        if PROVIDERS_ROOT in path.parents:
            continue
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Compare | ast.MatchValue):
                hits = string_constants(node) & watched
                if hits:
                    offenders.append(f"{relative(path)}:{node.lineno} branches on {sorted(hits)}")
    assert offenders == []


# ---- the router model is pinned by a setting, its facts a third quirks.py table (ticket 154) ---


def test_the_router_model_defaults_to_the_pinned_id(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = config.load()
    assert settings.router_model == "typesafe/jev-1.13-20260917"
    assert settings.router_model == config.DEFAULT_ROUTER_MODEL


def test_a_router_model_setting_overrides_the_default(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_ROUTER_MODEL", "typesafe/jev-2.0-preview")
    assert config.load().router_model == "typesafe/jev-2.0-preview"


def test_jev_latest_is_a_default_nowhere_in_src() -> None:
    """FR-95: ``typesafe/jev-latest`` is never a shipped default, pinned or otherwise."""
    for path in python_sources(SRC_ROOT):
        assert "jev-latest" not in path.read_text(encoding="utf-8"), relative(path)


def test_router_caps_for_answers_the_pinned_models_questions_and_context_budget() -> None:
    caps = quirks.router_caps_for(config.DEFAULT_ROUTER_MODEL)
    assert caps.questions == ("noul", "choice", "score")
    assert caps.context_tokens == 32_000


def test_router_caps_for_an_unknown_model_falls_back_to_the_default() -> None:
    assert quirks.router_caps_for("not-a-router-model") is quirks.DEFAULT_ROUTER_CAPS


def test_no_module_outside_providers_branches_on_a_router_model_id() -> None:
    """MUST rule 19 extended to the router model (decision 57): a model difference is a
    ``ROUTER_MODELS`` entry read through ``router_caps_for``, never an ``if`` on the id
    upstream of ``quirks.py``."""
    watched = frozenset(quirks.ROUTER_MODELS)
    offenders: list[str] = []
    for path in python_sources(SRC_ROOT):
        if PROVIDERS_ROOT in path.parents:
            continue
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Compare | ast.MatchValue):
                hits = string_constants(node) & watched
                if hits:
                    offenders.append(f"{relative(path)}:{node.lineno} branches on {sorted(hits)}")
    capture_154(
        "branch_scan.txt",
        f"watched router model ids: {sorted(watched)}\noffenders: {offenders}\n",
    )
    assert offenders == []


def test_endpoint_hostnames_are_spelled_only_in_quirks() -> None:
    offenders: list[str] = []
    for path in python_sources(SRC_ROOT):
        if path in (QUIRKS_SOURCE, JEV_SOURCE):
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{relative(path)} spells {hostname}"
            for hostname in quirks.ENDPOINTS
            if hostname in text
        )
    assert offenders == []


def test_the_loop_package_never_names_an_endpoint_or_a_provider() -> None:
    # Vacuous until ticket 011 lands the engine, and load-bearing from that moment on.
    watched = frozenset(config.PROVIDER_KEY_VARS) | frozenset(quirks.ENDPOINTS) | VENDOR_SDKS
    if not LOOP_ROOT.exists():
        pytest.skip("loop/ is built by ticket 011; this assertion arms itself when it lands")
    for path in python_sources(LOOP_ROOT):
        text = path.read_text(encoding="utf-8")
        assert not [word for word in watched if word in text], relative(path)


# ---- the vendor SDKs stay inside the quarantine ------------------------------------------------


def test_the_vendor_sdks_are_imported_only_under_providers() -> None:
    offenders: list[str] = []
    for root in (SRC_ROOT, REPO_ROOT / "tests"):
        for path in python_sources(root):
            if PROVIDERS_ROOT in path.parents:
                continue
            offenders.extend(
                f"{relative(path)}:{lineno} imports {name}"
                for lineno, name in imported_roots(parse(path))
                if name in VENDOR_SDKS
            )
    assert offenders == []


def test_the_quarantine_actually_imports_them() -> None:
    # The scan above is only meaningful if the SDKs are imported somewhere at all.
    imported = {
        name
        for path in python_sources(PROVIDERS_ROOT)
        for _, name in imported_roots(parse(path))
        if name in VENDOR_SDKS
    }
    assert imported == VENDOR_SDKS


# ---- a missing key reads like a sentence -------------------------------------------------------


@pytest.mark.parametrize("name", LIVE_PROVIDERS)
def test_constructing_a_live_adapter_without_its_key_raises_missing_credential(
    name: str, clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chdir is what makes "without its key" true anywhere (ticket 039).

    ``config.load()`` reads the key vars out of a ``.env`` in the working directory since ticket
    039, and a developer who has configured this checkout has one - so without the chdir this
    would pass in CI and fail on their machine, which is not a defect it should be reporting.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_PROVIDER", name)
    settings = config.load()
    with pytest.raises(errors.MissingCredentialError) as excinfo:
        registry.create(settings)
    var = config.PROVIDER_KEY_VARS[name]
    message = str(excinfo.value)
    assert var is not None
    assert f"{var}=" in message
    assert ".env" in message
    assert name in message
    assert isinstance(excinfo.value, errors.HarnessError)
    assert not isinstance(excinfo.value, KeyError)


def test_the_adapter_refuses_to_construct_itself_without_a_key(clean_env: None) -> None:
    """The check lives at construction, so no path reaches a 401 by skipping the registry."""
    settings = config.load()
    with pytest.raises(errors.MissingCredentialError):
        openai_compat.OpenAICompatAdapter(settings=settings, provider="openai")
    with pytest.raises(errors.MissingCredentialError):
        anthropic_adapter.AnthropicAdapter(settings=settings, provider="anthropic")


@pytest.mark.parametrize("name", LIVE_PROVIDERS)
def test_a_live_adapter_constructs_when_its_key_is_present(
    name: str, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    var = config.PROVIDER_KEY_VARS[name]
    assert var is not None
    monkeypatch.setenv("HARNESSED_PROVIDER", name)
    monkeypatch.setenv(var, THROWAWAY_KEY)
    provider = registry.create(config.load())
    assert isinstance(provider, base.Provider)
    assert provider.caps is quirks.caps_for(quirks.base_url_for(name))


def test_an_unknown_provider_name_is_a_config_error_not_a_key_error(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_PROVIDER", "not-a-provider")
    with pytest.raises(errors.ConfigError) as excinfo:
        registry.create(config.load())
    assert not isinstance(excinfo.value, errors.MissingCredentialError)
    assert "not-a-provider" in str(excinfo.value)


def test_a_custom_base_url_selects_the_caps_for_that_hostname(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", THROWAWAY_KEY)
    monkeypatch.setenv("HARNESSED_BASE_URL", "https://llm.example.internal/v1")
    provider = registry.create(config.load())
    assert provider.caps is quirks.DEFAULT_CAPS
    assert provider.base_url == "https://llm.example.internal/v1"


# ---- the keyless path never asks for a credential ----------------------------------------------


@pytest.mark.parametrize("name", KEYLESS_PROVIDERS)
def test_a_keyless_provider_requires_no_credential(name: str) -> None:
    assert registry.requires_credential(name) is False


def test_the_fake_provider_is_constructed_without_any_key_check(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_PROVIDER", "fake")
    settings = config.load()
    # Any credential lookup at all, successful or not, fails this test.
    monkeypatch.setattr(Settings, "require_api_key", explode)
    monkeypatch.setattr(Settings, "api_key_for", explode)

    assert registry.requires_credential("fake") is False
    assert registry.credential_for(settings) is None

    if importlib.util.find_spec("harnessed.providers.fake") is not None:
        # Arms itself the moment ticket 005 lands FakeProvider; until then the gate above is
        # the whole claim, because there is nothing yet to construct.
        assert isinstance(registry.create(settings), base.Provider)


def test_requires_credential_is_the_single_table_lookup_the_registry_owns() -> None:
    for name, var in config.PROVIDER_KEY_VARS.items():
        assert registry.requires_credential(name) is (var is not None)


# ---- ticket 296: structured output is a capability of the endpoint and the model ----------------


def _schema_request(model: str) -> types.ChatRequest:
    return types.ChatRequest(
        model=model,
        messages=(types.Message(role="user", content="hi"),),
        response_schema=types.ResponseSchema("facts", {"type": "object"}),
    )


def test_a_groq_gpt_oss_request_carries_a_strict_json_schema_and_an_ollama_one_does_not() -> None:
    groq = openai_compat.shape_request(
        _schema_request("openai/gpt-oss-20b"), quirks.caps_for("https://api.groq.com/openai/v1")
    )
    assert groq["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "facts", "strict": True, "schema": {"type": "object"}},
    }
    ollama = openai_compat.shape_request(
        _schema_request("gpt-oss:20b"), quirks.caps_for("http://localhost:11434/v1")
    )
    assert "response_format" not in ollama


def test_both_capabilities_must_hold_before_the_schema_is_sent() -> None:
    groq_caps = quirks.caps_for("https://api.groq.com/openai/v1")
    assert groq_caps.json_schema and quirks.model_caps_for("gpt-oss-120b").strict_schema
    plain = openai_compat.shape_request(_schema_request("llama-3.1-8b"), groq_caps)
    assert "response_format" not in plain
    assert not quirks.DEFAULT_CAPS.json_schema and not quirks.DEFAULT_MODEL_CAPS.strict_schema
    anthropic = anthropic_adapter.shape_request(
        _schema_request("claude-x"), quirks.caps_for("https://api.anthropic.com")
    )
    assert "response_format" not in anthropic
