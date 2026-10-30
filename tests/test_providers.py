"""FR-8: every wire shape lives in ``providers/translate.py`` and nowhere else.

Almost all of these are pure data tests: they never construct a client, never touch the network
and never generate a fixture - the corpus under ``tests/fixtures/wire/`` is committed data
recorded once and reviewed like code (All AI agents MUST rule 8).

The last section is the exception and says so. Ticket 040 gave each adapter one job beyond
translating - turning a vendor status error into a ``ProviderError`` - and a translation nobody
calls is not a translation, so those tests build the adapter and stand in for the **endpoint**
by replacing the one SDK call. The configuration around it is left real, which is the split
ticket 039 recorded: stub the endpoint, never the thing under test.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harnessed import config
from harnessed.config import Settings
from harnessed.errors import HarnessError, ProviderError
from harnessed.providers import anthropic as anthropic_adapter
from harnessed.providers import base, openai_compat, translate, types

AnthropicAdapter = anthropic_adapter.AnthropicAdapter
OpenAICompatAdapter = openai_compat.OpenAICompatAdapter

#: The SDK exception types, reached through the adapter that owns them rather than imported.
#: `tests/test_provider_quirks.py` scans `tests/` as well as `src/` for a vendor SDK import, and
#: the quarantine it enforces is the point of this file - a test that broke it to assert it
#: would be asserting something it had just stopped being true.
OpenAIStatusError = openai_compat.openai.APIStatusError
AnthropicStatusError = anthropic_adapter.anthropic.APIStatusError

FIXTURES = Path(__file__).parent / "fixtures" / "wire"
OPENAI_FIXTURES = sorted((FIXTURES / "openai").glob("*.json"))
ANTHROPIC_FIXTURES = sorted((FIXTURES / "anthropic").glob("*.json"))

PACKAGE_ROOT = Path(base.__file__).parent.parent
TRANSLATE_SOURCE = PACKAGE_ROOT / "providers" / "translate.py"


def load(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def projection(response: types.ChatResponse) -> tuple[Any, ...]:
    """Everything downstream of the quarantine can observe.

    ``parse_error`` is deliberately absent: it is a translation artifact, not a wire field, so a
    round trip is not expected to carry it back onto the wire.
    """
    return (
        response.response_id,
        response.model,
        response.content,
        response.stop_reason,
        response.usage,
        tuple((c.id, c.name, dict(c.arguments)) for c in response.tool_calls),
    )


# ---- golden round trip -------------------------------------------------------------------------


@pytest.mark.parametrize("path", OPENAI_FIXTURES, ids=lambda p: p.stem)
def test_openai_fixture_round_trips_without_loss(path: Path) -> None:
    once = translate.from_openai_response(load(path))
    twice = translate.from_openai_response(translate.to_openai_response(once))
    assert projection(twice) == projection(once)


@pytest.mark.parametrize("path", ANTHROPIC_FIXTURES, ids=lambda p: p.stem)
def test_anthropic_fixture_round_trips_without_loss(path: Path) -> None:
    once = translate.from_anthropic_response(load(path))
    twice = translate.from_anthropic_response(translate.to_anthropic_response(once))
    assert projection(twice) == projection(once)


def test_the_corpus_covers_both_dialects() -> None:
    assert len(OPENAI_FIXTURES) >= 5
    assert len(ANTHROPIC_FIXTURES) >= 5


# ---- arguments are always a parsed dict --------------------------------------------------------


def test_openai_string_arguments_arrive_as_a_dict() -> None:
    response = translate.from_openai_response(load(FIXTURES / "openai" / "single_tool_call.json"))
    (call,) = response.tool_calls
    assert call.arguments == {"path": "notes/todo.md"}
    assert call.parse_error is None


def test_anthropic_dict_input_arrives_as_a_dict() -> None:
    response = translate.from_anthropic_response(
        load(FIXTURES / "anthropic" / "single_tool_use.json")
    )
    (call,) = response.tool_calls
    assert call.arguments == {"path": "notes/todo.md"}
    assert call.parse_error is None


def test_malformed_arguments_do_not_raise_and_carry_a_parse_error() -> None:
    response = translate.from_openai_response(
        load(FIXTURES / "openai" / "malformed_arguments.json")
    )
    (call,) = response.tool_calls
    assert call.arguments == {}
    assert call.parse_error is not None
    assert "fs_write" in call.parse_error
    assert call.id == "call_Br0k3nJs0nArgs01"


def test_the_glossary_name_is_what_reaches_the_ledger() -> None:
    response = translate.from_openai_response(
        load(FIXTURES / "openai" / "malformed_arguments.json")
    )
    payload = translate.tool_call_payload(response.tool_calls[0])
    assert payload["_arg_parse_error"] == response.tool_calls[0].parse_error
    clean = translate.from_openai_response(load(FIXTURES / "openai" / "single_tool_call.json"))
    assert "_arg_parse_error" not in translate.tool_call_payload(clean.tool_calls[0])


def test_tool_calls_are_frozen_value_types() -> None:
    call = types.ToolCall(id="call_x", name="fs_read", arguments={})
    with pytest.raises(AttributeError):
        call.name = "other"  # type: ignore[misc]


# ---- provider-issued ids survive verbatim ------------------------------------------------------


def test_openai_call_ids_survive_verbatim() -> None:
    once = translate.from_openai_response(load(FIXTURES / "openai" / "parallel_tool_calls.json"))
    twice = translate.from_openai_response(translate.to_openai_response(once))
    assert [c.id for c in once.tool_calls] == ["call_A1b2C3d4E5f6G7h8", "call_Z9y8X7w6V5u4T3s2"]
    assert [c.id for c in twice.tool_calls] == [c.id for c in once.tool_calls]


def test_anthropic_call_ids_survive_verbatim() -> None:
    once = translate.from_anthropic_response(
        load(FIXTURES / "anthropic" / "parallel_tool_use.json")
    )
    twice = translate.from_anthropic_response(translate.to_anthropic_response(once))
    assert [c.id for c in once.tool_calls] == [
        "toolu_01Bx9pQr7ZmKtVn3sLd4Hf",
        "toolu_01Cy8oPs6YnJsUm2rKc3Gd",
    ]
    assert [c.id for c in twice.tool_calls] == [c.id for c in once.tool_calls]


# ---- tool results shape per dialect ------------------------------------------------------------


RESULTS = (
    types.ToolResult(call_id="toolu_one", content="alpha"),
    types.ToolResult(call_id="toolu_two", content="boom", is_error=True),
    types.ToolResult(call_id="toolu_three", content="gamma"),
)


def test_anthropic_batches_n_results_into_one_user_message() -> None:
    messages = translate.to_anthropic_tool_result_messages(RESULTS)
    assert len(messages) == 1
    (message,) = messages
    assert message["role"] == "user"
    blocks = message["content"]
    assert len(blocks) == len(RESULTS)
    assert {b["type"] for b in blocks} == {"tool_result"}
    assert [b["tool_use_id"] for b in blocks] == [r.call_id for r in RESULTS]
    assert blocks[1]["is_error"] is True


def test_openai_emits_one_tool_message_per_result() -> None:
    messages = translate.to_openai_tool_result_messages(RESULTS)
    assert len(messages) == len(RESULTS)
    assert {m["role"] for m in messages} == {"tool"}
    assert [m["tool_call_id"] for m in messages] == [r.call_id for r in RESULTS]
    # The OpenAI-compatible dialect has no is_error field; the error has to survive as content.
    assert all("is_error" not in m for m in messages)
    assert "boom" in messages[1]["content"]


# ---- one normalized vocabulary for stop reasons and usage --------------------------------------


@pytest.mark.parametrize(
    ("wire", "normalized"),
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("content_filter", "content_filter"),
        ("function_call", "other"),
    ],
)
def test_openai_stop_reasons_normalize(wire: str, normalized: str) -> None:
    payload = load(FIXTURES / "openai" / "text_only.json")
    payload["choices"][0]["finish_reason"] = wire
    assert translate.from_openai_response(payload).stop_reason == normalized


@pytest.mark.parametrize(
    ("wire", "normalized"),
    [
        ("end_turn", "end_turn"),
        ("tool_use", "tool_use"),
        ("max_tokens", "max_tokens"),
        ("stop_sequence", "stop_sequence"),
        ("pause_turn", "other"),
    ],
)
def test_anthropic_stop_reasons_normalize(wire: str, normalized: str) -> None:
    payload = load(FIXTURES / "anthropic" / "text_only.json")
    payload["stop_reason"] = wire
    assert translate.from_anthropic_response(payload).stop_reason == normalized


def test_both_dialects_share_one_stop_reason_vocabulary() -> None:
    openai_reasons = {translate.from_openai_response(load(p)).stop_reason for p in OPENAI_FIXTURES}
    anthropic_reasons = {
        translate.from_anthropic_response(load(p)).stop_reason for p in ANTHROPIC_FIXTURES
    }
    assert openai_reasons <= set(types.STOP_REASONS)
    assert anthropic_reasons <= set(types.STOP_REASONS)
    # Both corpora reach the same normalized terms for the same situations.
    assert {"end_turn", "tool_use", "max_tokens"} <= openai_reasons
    assert {"end_turn", "tool_use", "max_tokens"} <= anthropic_reasons


def test_usage_counters_normalize_from_both_dialects() -> None:
    openai_usage = translate.from_openai_response(
        load(FIXTURES / "openai" / "text_only.json")
    ).usage
    anthropic_usage = translate.from_anthropic_response(
        load(FIXTURES / "anthropic" / "text_only.json")
    ).usage
    assert openai_usage == types.Usage(input_tokens=24, output_tokens=8)
    assert openai_usage == anthropic_usage
    assert openai_usage.total_tokens == 32


def test_a_missing_usage_block_normalizes_to_zeroes() -> None:
    payload = load(FIXTURES / "openai" / "text_only.json")
    del payload["usage"]
    assert translate.from_openai_response(payload).usage == types.Usage()


# ---- outbound requests -------------------------------------------------------------------------


def request_with_metadata() -> types.ChatRequest:
    return types.ChatRequest(
        model="a-model",
        system="You are Vinci.",
        messages=(
            types.Message(role="user", content="read a.txt"),
            types.Message(
                role="assistant",
                content="on it",
                tool_calls=(
                    types.ToolCall(id="call_keepme", name="fs_read", arguments={"path": "a.txt"}),
                ),
            ),
            types.Message(
                role="tool",
                tool_results=(types.ToolResult(call_id="call_keepme", content="alpha"),),
            ),
        ),
        tools=(
            types.ToolSchema(
                name="fs_read",
                description="Read a file under HARNESSED_HOME.",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ),
        metadata={"scope": "case_capital_of_france"},
    )


def test_metadata_never_reaches_the_openai_wire() -> None:
    payload = translate.to_openai_request(request_with_metadata())
    assert "metadata" not in payload
    assert "case_capital_of_france" not in json.dumps(payload)


def test_metadata_never_reaches_the_anthropic_wire() -> None:
    payload = translate.to_anthropic_request(request_with_metadata())
    assert "metadata" not in payload
    assert "case_capital_of_france" not in json.dumps(payload)


def test_openai_request_carries_the_system_prompt_as_the_first_message() -> None:
    payload = translate.to_openai_request(request_with_metadata())
    assert "system" not in payload
    assert payload["messages"][0] == {"role": "system", "content": "You are Vinci."}
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "fs_read"
    assert "parameters" in payload["tools"][0]["function"]


def test_anthropic_request_carries_the_system_prompt_as_a_top_level_param() -> None:
    payload = translate.to_anthropic_request(request_with_metadata())
    assert payload["system"] == "You are Vinci."
    assert {m["role"] for m in payload["messages"]} == {"user", "assistant"}
    assert payload["tools"][0]["name"] == "fs_read"
    assert "input_schema" in payload["tools"][0]
    assert "type" not in payload["tools"][0]


def test_tool_results_inside_a_request_take_the_dialect_shape() -> None:
    openai_payload = translate.to_openai_request(request_with_metadata())
    assert [m["role"] for m in openai_payload["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    anthropic_payload = translate.to_anthropic_request(request_with_metadata())
    assert [m["role"] for m in anthropic_payload["messages"]] == ["user", "assistant", "user"]
    assert anthropic_payload["messages"][-1]["content"][0]["type"] == "tool_result"


def test_outbound_tool_calls_keep_their_provider_ids() -> None:
    openai_payload = translate.to_openai_request(request_with_metadata())
    assistant = next(m for m in openai_payload["messages"] if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["id"] == "call_keepme"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"path": "a.txt"}

    anthropic_payload = translate.to_anthropic_request(request_with_metadata())
    assistant = next(m for m in anthropic_payload["messages"] if m["role"] == "assistant")
    use = next(b for b in assistant["content"] if b["type"] == "tool_use")
    assert use["id"] == "call_keepme"
    assert use["input"] == {"path": "a.txt"}


# ---- the quarantine itself ---------------------------------------------------------------------


def translate_ast() -> ast.Module:
    return ast.parse(TRANSLATE_SOURCE.read_text(encoding="utf-8"))


def test_translate_imports_nothing_from_loop_or_tools() -> None:
    forbidden = {"loop", "tools", "memory", "evals", "improve", "acp", "cli"}
    for node in ast.walk(translate_ast()):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert not forbidden & set(name.split(".")), f"translate.py imports {name!r}"


def test_translate_performs_no_io() -> None:
    io_modules = {
        "os",
        "io",
        "pathlib",
        "socket",
        "subprocess",
        "urllib",
        "httpx",
        "requests",
        "sqlite3",
        "openai",
        "anthropic",
    }
    io_calls = {"open", "print", "input"}
    for node in ast.walk(translate_ast()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in io_modules, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in io_modules, node.module
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in io_calls, node.func.id


def test_translate_is_the_only_module_that_parses_chat_output() -> None:
    """The chat wire format's parser is quarantined to ``translate.py``.

    The router's Decisions wire format is a sibling, not an exception to this: ``jev.py::parse``
    is that format's own ``translate.py`` (decision 57, ticket 153), and ``fake_decider.py``
    reads the same committed-fixture-corpus discipline ``fake.py`` does over it (ticket 155).
    Neither parses a *chat* completion, which is what this scan guards.

    ``acp/metering.py`` is the one module outside ``providers/`` that reads a chat completion, and
    it does so by design (decision 63, ticket 172): the metering proxy tees an ``openrouter``
    agent's own stream to journal the turn. It never builds a ``ChatResponse`` or feeds the loop,
    and ``acp/`` may not import ``translate.py`` (the import table), so it is named here rather
    than hidden.
    """
    offenders = sorted(
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if "json.loads" in path.read_text(encoding="utf-8")
    )
    assert offenders == [
        "acp/metering.py",
        "memory/consolidation.py",  # reads persisted diagnostic JSON, not model chat output
        "providers/fake_decider.py",
        "providers/jev.py",
        "providers/translate.py",
    ]


def test_provider_protocol_is_runtime_checkable() -> None:
    class Stub:
        def complete(self, request: types.ChatRequest) -> types.ChatResponse:
            return types.ChatResponse(response_id="r", model=request.model)

    assert isinstance(Stub(), base.Provider)
    assert not isinstance(object(), base.Provider)


# ---- degenerate wire shapes ------------------------------------------------------------------


@pytest.mark.parametrize("raw", [42, ["a", "b"], True])
def test_non_object_arguments_become_a_parse_error_not_an_exception(raw: object) -> None:
    payload = load(FIXTURES / "openai" / "single_tool_call.json")
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = raw
    (call,) = translate.from_openai_response(payload).tool_calls
    assert call.arguments == {}
    assert call.parse_error is not None
    assert call.id == "call_9NfQ2mZk1pLbVt3s"


def test_arguments_that_parse_to_a_json_array_are_a_parse_error() -> None:
    payload = load(FIXTURES / "openai" / "single_tool_call.json")
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '["a"]'
    (call,) = translate.from_openai_response(payload).tool_calls
    assert call.arguments == {}
    assert call.parse_error is not None
    assert "list" in call.parse_error


def test_empty_arguments_are_an_empty_dict_and_not_an_error() -> None:
    payload = load(FIXTURES / "openai" / "single_tool_call.json")
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = ""
    (call,) = translate.from_openai_response(payload).tool_calls
    assert call.arguments == {}
    assert call.parse_error is None


def test_a_missing_finish_reason_still_yields_a_normalized_term() -> None:
    payload = load(FIXTURES / "openai" / "text_only.json")
    del payload["choices"][0]["finish_reason"]
    assert translate.from_openai_response(payload).stop_reason in types.STOP_REASONS


def test_an_empty_response_body_does_not_raise() -> None:
    assert translate.from_openai_response({}).content == ""
    assert translate.from_anthropic_response({}).content == ""


def test_a_null_usage_count_is_zero_not_a_crash() -> None:
    """A provider that sends ``prompt_tokens: null`` must not crash ``int(None)`` (finding 2)."""
    payload = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": None, "completion_tokens": None},
    }
    response = translate.from_openai_response(payload)
    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 0


def test_an_openai_content_array_becomes_text_not_a_python_repr() -> None:
    """An OpenAI content array is text parts, not a list to stringify (finding 4)."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "text", "text": "world"},
                    ]
                }
            }
        ]
    }
    assert translate.from_openai_response(payload).content == "hello\nworld"


def test_no_results_produce_no_messages_in_either_dialect() -> None:
    assert translate.to_anthropic_tool_result_messages(()) == []
    assert translate.to_openai_tool_result_messages(()) == []


def test_stop_sequences_take_each_dialects_key() -> None:
    request = types.ChatRequest(model="m", stop_sequences=("\nHuman:",))
    assert translate.to_openai_request(request)["stop"] == ["\nHuman:"]
    assert translate.to_anthropic_request(request)["stop_sequences"] == ["\nHuman:"]


def test_a_stray_system_message_folds_into_the_anthropic_top_level_param() -> None:
    request = types.ChatRequest(
        model="m",
        system="You are Vinci.",
        messages=(types.Message(role="system", content="ignored"), types.Message(role="user")),
    )
    payload = translate.to_anthropic_request(request)
    assert [m["role"] for m in payload["messages"]] == ["user"]
    assert payload["system"] == "You are Vinci."


def test_a_bare_request_declares_no_tools() -> None:
    payload = translate.to_openai_request(types.ChatRequest(model="m"))
    assert "tools" not in payload
    assert "tools" not in translate.to_anthropic_request(types.ChatRequest(model="m"))


# ---- a vendor status error reads like one (ticket 040) -----------------------------------------

#: Obviously not a credential, so the repository write guard accepts the literal and nothing in
#: `redact.py`'s pattern table matches it. The endpoint it authenticates is a stub.
NOT_A_KEY = "not-a-real-key-for-a-stubbed-endpoint-1618033988"


class Raises:
    """Stands in for the one SDK call each adapter makes. Raises what it was handed."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __call__(self, **kwargs: Any) -> Any:
        raise self.exc


def vendor_response(status: int) -> Any:
    """The minimum an SDK status error reads off a response: the code, the request, the headers.

    A real `httpx.Response` would do too, but httpx is a transitive dependency of both SDKs and
    the architecture pins neither, so the test names nothing it was not given.
    """
    return SimpleNamespace(status_code=status, request=None, headers={})


def openai_error(status: int, message: str) -> BaseException:
    return OpenAIStatusError(message, response=vendor_response(status), body=None)


def anthropic_error(status: int, message: str) -> BaseException:
    return AnthropicStatusError(message, response=vendor_response(status), body=None)


@pytest.fixture
def live_settings(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """A configured live provider whose endpoint is never reached. The chdir keeps a developer's
    own `.env` out of the resolution, which since ticket 040 supplies every documented variable.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", NOT_A_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", NOT_A_KEY)
    monkeypatch.setenv("HARNESSED_MODEL", STALE_MODEL)
    return config.load()


#: A model id no endpoint serves, which is what a table entry becomes when a vendor retires one.
STALE_MODEL = "a-model-that-was-retired"

REQUEST = types.ChatRequest(
    model=STALE_MODEL, messages=(types.Message(role="user", content="ping"),)
)


def test_an_openai_status_error_leaves_the_adapter_as_a_provider_error(
    live_settings: Settings,
) -> None:
    """A 4xx from a real endpoint is not a bug report, so it may not arrive as a traceback. The
    vendor exception is caught here because `providers/` is the one package that may name an SDK.
    """
    adapter = OpenAICompatAdapter(settings=live_settings, provider="groq")
    adapter._client.chat.completions.create = Raises(  # type: ignore[method-assign]
        openai_error(400, "the supported model names are a, b, but you passed groq")
    )
    with pytest.raises(ProviderError) as raised:
        adapter.complete(REQUEST)
    message = str(raised.value)
    assert isinstance(raised.value, HarnessError)
    assert "400" in message
    assert "but you passed groq" in message, "the vendor's own message has to survive"
    assert "groq" in message


def test_a_bad_model_id_says_the_configured_model_may_be_stale(live_settings: Settings) -> None:
    """The seventh criterion, and the mitigation the notes accept the table's staleness for: the
    next person to hit a retired id is pointed at the table rather than at the loop."""
    adapter = OpenAICompatAdapter(settings=live_settings, provider="groq")
    adapter._client.chat.completions.create = Raises(  # type: ignore[method-assign]
        openai_error(404, f"The model '{STALE_MODEL}' does not exist")
    )
    with pytest.raises(ProviderError) as raised:
        adapter.complete(REQUEST)
    message = str(raised.value)
    assert STALE_MODEL in message
    assert "stale" in message
    assert config.MODEL_VAR in message


def test_a_server_side_failure_carries_the_status_without_blaming_the_model(
    live_settings: Settings,
) -> None:
    """A 503 is the endpoint's problem, not the table's. Telling an operator to change their
    model id would send them to fix something that is not broken."""
    adapter = OpenAICompatAdapter(settings=live_settings, provider="groq")
    adapter._client.chat.completions.create = Raises(  # type: ignore[method-assign]
        openai_error(503, "service unavailable")
    )
    with pytest.raises(ProviderError) as raised:
        adapter.complete(REQUEST)
    message = str(raised.value)
    assert "503" in message
    assert "stale" not in message


def test_an_anthropic_status_error_translates_the_same_way(live_settings: Settings) -> None:
    """Second dialect, same contract. Neither adapter is the special one."""
    adapter = AnthropicAdapter(settings=live_settings, provider="anthropic")
    adapter._client.messages.create = Raises(  # type: ignore[method-assign]
        anthropic_error(404, f"model: {STALE_MODEL}")
    )
    with pytest.raises(ProviderError) as raised:
        adapter.complete(REQUEST)
    message = str(raised.value)
    assert "404" in message
    assert STALE_MODEL in message
    assert "stale" in message


def test_a_credential_echoed_back_by_the_endpoint_is_not_in_the_rendered_message(
    live_settings: Settings,
) -> None:
    """An SDK is free to quote the request back inside its message, and this string is printed.
    `validate_credential` drops the vendor's message outright for that reason; this path is
    required to carry it, so it scrubs the values `config.secret_values()` knows about instead.
    """
    adapter = OpenAICompatAdapter(settings=live_settings, provider="groq")
    adapter._client.chat.completions.create = Raises(  # type: ignore[method-assign]
        openai_error(401, f"invalid api key: {NOT_A_KEY}")
    )
    with pytest.raises(ProviderError) as raised:
        adapter.complete(REQUEST)
    message = str(raised.value)
    assert NOT_A_KEY not in message
    assert "401" in message


def test_an_error_that_is_not_a_status_error_still_propagates(live_settings: Settings) -> None:
    """The catch is the SDK's status-error base, not `Exception`. A bug inside the adapter has
    to stay a traceback, which is what `tests/test_events.py`'s two-seam allowlist protects."""
    adapter = OpenAICompatAdapter(settings=live_settings, provider="groq")
    adapter._client.chat.completions.create = Raises(  # type: ignore[method-assign]
        RuntimeError("the disk caught fire")
    )
    with pytest.raises(RuntimeError, match="the disk caught fire"):
        adapter.complete(REQUEST)


# ---- ticket 231 (FR-160): a dropped connection leaves an adapter as TransportDropped -----------


def _connection_error(module: Any) -> BaseException:
    import httpx

    request = httpx.Request("POST", "https://example.invalid")
    error = module.APIConnectionError(request=request)
    error.__cause__ = httpx.RemoteProtocolError("Remote end closed connection")
    return error  # type: ignore[no-any-return]


def test_both_adapters_restate_a_dropped_connection_as_transport_dropped(
    live_settings: Settings,
) -> None:
    from harnessed.errors import TransportDropped

    first = OpenAICompatAdapter(settings=live_settings, provider="groq")
    first._client.chat.completions.create = Raises(  # type: ignore[method-assign]
        _connection_error(openai_compat.openai)
    )
    second = AnthropicAdapter(settings=live_settings, provider="anthropic")
    second._client.messages.create = Raises(  # type: ignore[method-assign]
        _connection_error(anthropic_adapter.anthropic)
    )
    for adapter in (first, second):
        with pytest.raises(TransportDropped) as raised:
            adapter.complete(REQUEST)
        assert raised.value.error_class == "RemoteProtocolError"
        assert isinstance(raised.value, ProviderError)


def test_a_connection_error_that_is_not_a_drop_stays_a_traceback(live_settings: Settings) -> None:
    import httpx

    request = httpx.Request("POST", "https://example.invalid")
    error = openai_compat.openai.APITimeoutError(request=request)
    error.__cause__ = httpx.ReadTimeout("read")
    adapter = OpenAICompatAdapter(settings=live_settings, provider="groq")
    adapter._client.chat.completions.create = Raises(error)  # type: ignore[method-assign]
    with pytest.raises(openai_compat.openai.APITimeoutError):
        adapter.complete(REQUEST)


def test_a_live_eval_builds_its_client_with_no_sdk_retries_of_its_own(
    live_settings: Settings,
) -> None:
    """The bounded retry is ``RetryingProvider``'s alone, so the SDK's own two are switched off."""
    from harnessed.providers import registry

    quiet = registry.create(live_settings, max_retries=0)
    assert quiet._client.max_retries == 0  # type: ignore[attr-defined]
    default = registry.create(live_settings)
    assert default._client.max_retries == 2  # type: ignore[attr-defined]
