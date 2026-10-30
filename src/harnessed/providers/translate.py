"""Every wire shape in the repo, quarantined here as pure functions.

FR-8. Nothing in this module does I/O, and nothing here imports ``loop/``, ``tools/`` or any
vendor SDK - a shape is data, and translating it is arithmetic over dicts. That is what makes the
golden round-trip test a pure data test.

This is also the **only** module in the codebase permitted to call :func:`json.loads` on model
output, and the sole producer of the ``_arg_parse_error`` marker. Its sole consumer is
``tools/registry.py``; a third module naming it is a bug (architecture, open question 8).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Final

from harnessed.errors import TranslationError
from harnessed.providers.types import (
    STOP_REASONS,
    ChatRequest,
    ChatResponse,
    ConsolidatedFact,
    Message,
    ResponseSchema,
    Role,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSchema,
    Usage,
)

__all__ = [
    "ARG_PARSE_ERROR_KEY",
    "consolidation_facts",
    "openai_response_format",
    "from_anthropic_response",
    "from_openai_response",
    "journal_message",
    "to_anthropic_request",
    "to_anthropic_response",
    "to_anthropic_tool_result_messages",
    "to_openai_request",
    "to_openai_response",
    "to_openai_tool_result_messages",
    "tool_call_payload",
]

Payload = dict[str, Any]

#: The PRD's glossary term, preserved verbatim as the serialized name of ``ToolCall.parse_error``
#: so the journal and every event payload spell it the way the spec does.
ARG_PARSE_ERROR_KEY: Final = "_arg_parse_error"

# ---- the consolidation reply -------------------------------------------------------------------

_RELATIONS: Final = ("new", "contradicts")
_FACT_KINDS: Final = ("preference", "convention", "setup", "decision")
_FACT_KEYS: Final = frozenset(("text", "actor", "path", "kind", "relation", "replaces"))


def openai_response_format(schema: ResponseSchema) -> Payload:
    """The ``response_format`` field that makes an OpenAI-compatible endpoint enforce ``schema``.

    Only the adapter calls this, and only when the endpoint and the model both hold the capability
    (decision 98); ``to_openai_request`` itself never sends it.
    """
    return {
        "type": "json_schema",
        "json_schema": {"name": schema.name, "strict": True, "schema": dict(schema.schema)},
    }


def consolidation_facts(
    text: str, known: Collection[str] | None = None
) -> tuple[ConsolidatedFact, ...]:
    """Parse a consolidation reply into its facts (ticket 247, FR-149; decision 98).

    The reply is ``{"facts": [{"text", "actor", "path", "kind", "relation", "replaces"}]}``.
    Anything else raises :class:`TranslationError`, which the caller settles as ``unparseable``:
    a body that is not that object, a fact with a missing or extra key, an empty text or actor, a
    kind or relation outside its words, a ``contradicts`` fact that replaces nothing, and - when
    ``known`` is given - a ``replaces`` naming a ``mem_id`` the prompt never showed the model.
    """
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"the consolidation reply is not JSON: {exc.msg}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("facts"), list):
        raise TranslationError('the consolidation reply is not an object with a "facts" list')
    facts: list[ConsolidatedFact] = []
    for item in body["facts"]:
        if not isinstance(item, dict) or set(item) != _FACT_KEYS:
            raise TranslationError(
                "a consolidation fact is not an object of text, actor, path, kind, relation "
                "and replaces"
            )
        fact_text, actor, path = item["text"], item["actor"], item["path"]
        kind, relation, replaces = item["kind"], item["relation"], item["replaces"]
        if not isinstance(fact_text, str) or not fact_text.strip():
            raise TranslationError("a consolidation fact has no text")
        if not isinstance(actor, str) or not actor.strip():
            raise TranslationError("a consolidation fact has no actor")
        if path is not None and not isinstance(path, str):
            raise TranslationError("a consolidation fact's path is not a string or null")
        if kind not in _FACT_KINDS:
            raise TranslationError(
                f"a consolidation fact has kind {kind!r}, not one of {', '.join(_FACT_KINDS)}"
            )
        if relation not in _RELATIONS:
            raise TranslationError(
                f"a consolidation fact has relation {relation!r}, not new or contradicts"
            )
        if replaces is not None and not isinstance(replaces, str):
            raise TranslationError("a consolidation fact's replaces is not a mem_id or null")
        if relation == "contradicts" and replaces is None:
            raise TranslationError("a contradicting consolidation fact replaces nothing")
        if relation == "new" and replaces is not None:
            raise TranslationError("a new consolidation fact names a fact it replaces")
        if replaces is not None and known is not None and replaces not in known:
            raise TranslationError(
                f"a consolidation fact replaces {replaces!r}, which was not offered"
            )
        facts.append(
            ConsolidatedFact(
                text=fact_text.strip(),
                actor=actor.strip(),
                path=path.strip() if path and path.strip() else None,
                kind=kind,
                relation=relation,
                replaces=replaces,
            )
        )
    return tuple(facts)


# ---- the one stop vocabulary -------------------------------------------------------------------

_OPENAI_STOP: Final[Mapping[str, StopReason]] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filter",
}
_ANTHROPIC_STOP: Final[Mapping[str, StopReason]] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
}

# The reverse maps are written out rather than inverted, because neither dialect has a term for
# every normalized reason. Where a dialect is silent, it falls back to its own generic terminal
# value: a wire word we never recorded must not be invented here.
_TO_OPENAI_STOP: Final[Mapping[StopReason, str]] = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "content_filter": "content_filter",
    "other": "stop",
}
_TO_ANTHROPIC_STOP: Final[Mapping[StopReason, str]] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "content_filter": "end_turn",
    "other": "end_turn",
}


def _normalize_stop(wire: object, table: Mapping[str, StopReason]) -> StopReason:
    """Map a dialect's stop word onto the normalized vocabulary. Unknown words become ``other``."""
    if isinstance(wire, str):
        return table.get(wire, "other")
    return "end_turn"


# ---- arguments: always a parsed dict -----------------------------------------------------------


def _parse_arguments(raw: object, tool_name: str) -> tuple[dict[str, Any], str | None]:
    """Return ``(arguments, parse_error)``. Never raises - a weak model is not an exception.

    OpenAI-compatible endpoints send ``arguments`` as a JSON string; Anthropic sends ``input`` as
    a dict. Both land here, and both leave as a dict, so the registry above this line has exactly
    one shape to dispatch on.
    """
    if isinstance(raw, Mapping):
        return dict(raw), None
    if raw is None or raw == "":
        return {}, None
    if not isinstance(raw, str):
        return (
            {},
            f"tool {tool_name!r} sent arguments of type {type(raw).__name__}, expected an object",
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, (
            f"tool {tool_name!r} sent arguments that are not valid JSON: {exc.msg} "
            f"at position {exc.pos}. Re-issue the call with a valid JSON object."
        )
    if not isinstance(parsed, dict):
        return {}, (
            f"tool {tool_name!r} sent arguments that parsed to "
            f"{type(parsed).__name__}, expected a JSON object."
        )
    return parsed, None


def tool_call_payload(call: ToolCall) -> Payload:
    """Serialize a call for the journal and for event payloads.

    The marker keeps the glossary spelling here and only here: everything upstream reads the
    typed ``parse_error`` field instead.
    """
    payload: Payload = {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
    if call.parse_error is not None:
        payload[ARG_PARSE_ERROR_KEY] = call.parse_error
    return payload


#: The four roles a stored message may carry, so a row from an older build cannot widen them.
_ROLES: Final[Mapping[str, Role]] = {
    "system": "system",
    "user": "user",
    "assistant": "assistant",
    "tool": "tool",
}


def journal_message(
    role: str, content: str, tool_calls: str | None, tool_results: str | None
) -> Message:
    """One ``run_messages`` row back to the :class:`Message` it was (FR-80, decision 50).

    The inverse of what the journal writes: ``tool_calls`` is a JSON list of
    :func:`tool_call_payload` objects and ``tool_results`` a JSON list of ``call_id`` /
    ``content`` / ``is_error`` objects, each ``NULL`` on a message that carries none. It lives
    here because ``loop/`` may not decode JSON and may not spell the parse marker; the journal is
    handed this function exactly as it is handed :func:`tool_call_payload` to write.
    """
    calls = tuple(
        ToolCall(
            id=str(call["id"]),
            name=str(call["name"]),
            arguments=dict(call.get("arguments") or {}),
            parse_error=call.get(ARG_PARSE_ERROR_KEY),
        )
        for call in (json.loads(tool_calls) if tool_calls else ())
    )
    results = tuple(
        ToolResult(
            call_id=str(result["call_id"]),
            content=str(result["content"]),
            is_error=bool(result["is_error"]),
        )
        for result in (json.loads(tool_results) if tool_results else ())
    )
    if role not in _ROLES:
        raise TranslationError(f"a stored message has role {role!r}, not one of {sorted(_ROLES)}")
    return Message(role=_ROLES[role], content=content, tool_calls=calls, tool_results=results)


# ---- OpenAI-compatible: inbound ----------------------------------------------------------------


def _openai_content_text(content: object) -> str:
    """OpenAI's ``content`` field, a string or an array of typed parts, as plain text.

    ``str(parts)`` would be the list's Python repr, not the model's words (finding 4).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return str(content or "")


def _tokens(value: Any) -> int:
    """A usage count, with ``null`` counting as zero.

    ``usage.get(name, 0)`` only substitutes its default when the key is absent; a provider that
    sends ``prompt_tokens: null`` still hands back ``None``, and ``int(None)`` is a crash, not a
    missing count (finding 2).
    """
    return 0 if value is None else int(value)


def _openai_cached_tokens(usage: Mapping[str, Any]) -> int:
    """The prompt-cache hit count, under whichever name the endpoint uses for it.

    Groq nests it as ``prompt_tokens_details.cached_tokens``; DeepSeek flattens it to
    ``prompt_cache_hit_tokens``. Both are the OpenAI dialect, so both names are read here rather
    than branching on which vendor answered.
    """
    details: Mapping[str, Any] = usage.get("prompt_tokens_details") or {}
    nested = details.get("cached_tokens")
    return _tokens(usage.get("prompt_cache_hit_tokens") if nested is None else nested)


def from_openai_response(payload: Mapping[str, Any]) -> ChatResponse:
    """Normalize one ``chat.completion`` body."""
    choices = payload.get("choices") or [{}]
    choice: Mapping[str, Any] = choices[0]
    message: Mapping[str, Any] = choice.get("message") or {}

    calls: list[ToolCall] = []
    for raw_call in message.get("tool_calls") or ():
        function: Mapping[str, Any] = raw_call.get("function") or {}
        name = str(function.get("name", ""))
        arguments, parse_error = _parse_arguments(function.get("arguments"), name)
        calls.append(
            ToolCall(
                id=str(raw_call.get("id", "")),
                name=name,
                arguments=arguments,
                parse_error=parse_error,
            )
        )

    usage: Mapping[str, Any] = payload.get("usage") or {}
    return ChatResponse(
        response_id=str(payload.get("id", "")),
        model=str(payload.get("model", "")),
        content=_openai_content_text(message.get("content")),
        tool_calls=tuple(calls),
        stop_reason=_normalize_stop(choice.get("finish_reason"), _OPENAI_STOP),
        usage=Usage(
            input_tokens=_tokens(usage.get("prompt_tokens")),
            output_tokens=_tokens(usage.get("completion_tokens")),
            cached_input_tokens=_openai_cached_tokens(usage),
        ),
    )


# ---- OpenAI-compatible: outbound ---------------------------------------------------------------


def _openai_tool_call(call: ToolCall) -> Payload:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments))},
    }


def _openai_assistant_message(content: str, calls: Sequence[ToolCall]) -> Payload:
    message: Payload = {"role": "assistant", "content": content or None}
    if calls:
        message["tool_calls"] = [_openai_tool_call(call) for call in calls]
    return message


def to_openai_response(response: ChatResponse) -> Payload:
    """Serialize a normalized response back into a ``chat.completion`` body.

    Only the round-trip test and the fixture recorder need this direction, but it is what makes
    "no field the loop uses is lost" a testable claim rather than an assertion.
    """
    usage: Payload = {
        "prompt_tokens": response.usage.input_tokens,
        "completion_tokens": response.usage.output_tokens,
        "total_tokens": response.usage.total_tokens,
    }
    if response.usage.cached_input_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": response.usage.cached_input_tokens}
    return {
        "id": response.response_id,
        "object": "chat.completion",
        "model": response.model,
        "choices": [
            {
                "index": 0,
                "message": _openai_assistant_message(response.content, response.tool_calls),
                "finish_reason": _TO_OPENAI_STOP[response.stop_reason],
            }
        ],
        "usage": usage,
    }


def to_openai_tool_result_messages(results: Iterable[ToolResult]) -> list[Payload]:
    """N results become N ``role:"tool"`` messages.

    The dialect has no ``is_error`` field, so a failure has to survive as content or it is lost.
    """
    messages: list[Payload] = []
    for result in results:
        content = f"ERROR: {result.content}" if result.is_error else result.content
        messages.append({"role": "tool", "tool_call_id": result.call_id, "content": content})
    return messages


def _openai_tool_declaration(tool: ToolSchema) -> Payload:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        },
    }


def to_openai_request(request: ChatRequest) -> Payload:
    """Serialize a normalized request. ``metadata`` is dropped and never reaches the wire."""
    messages: list[Payload] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for message in request.messages:
        if message.role == "tool" or message.tool_results:
            messages.extend(to_openai_tool_result_messages(message.tool_results))
        elif message.role == "assistant":
            messages.append(_openai_assistant_message(message.content, message.tool_calls))
        else:
            messages.append({"role": message.role, "content": message.content})

    payload: Payload = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    if request.tools:
        payload["tools"] = [_openai_tool_declaration(tool) for tool in request.tools]
    if request.stop_sequences:
        payload["stop"] = list(request.stop_sequences)
    return payload


# ---- Anthropic: inbound ------------------------------------------------------------------------


def from_anthropic_response(payload: Mapping[str, Any]) -> ChatResponse:
    """Normalize one Messages API body."""
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in payload.get("content") or ():
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            name = str(block.get("name", ""))
            arguments, parse_error = _parse_arguments(block.get("input"), name)
            calls.append(
                ToolCall(
                    id=str(block.get("id", "")),
                    name=name,
                    arguments=arguments,
                    parse_error=parse_error,
                )
            )

    usage: Mapping[str, Any] = payload.get("usage") or {}
    return ChatResponse(
        response_id=str(payload.get("id", "")),
        model=str(payload.get("model", "")),
        content="\n".join(texts),
        tool_calls=tuple(calls),
        stop_reason=_normalize_stop(payload.get("stop_reason"), _ANTHROPIC_STOP),
        usage=Usage(
            input_tokens=_tokens(usage.get("input_tokens")),
            output_tokens=_tokens(usage.get("output_tokens")),
            cached_input_tokens=_tokens(usage.get("cache_read_input_tokens")),
        ),
    )


# ---- Anthropic: outbound -----------------------------------------------------------------------


def _anthropic_content_blocks(content: str, calls: Sequence[ToolCall]) -> list[Payload]:
    blocks: list[Payload] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for call in calls:
        blocks.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": dict(call.arguments)}
        )
    return blocks


def to_anthropic_response(response: ChatResponse) -> Payload:
    """Serialize a normalized response back into a Messages API body."""
    usage: Payload = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    if response.usage.cached_input_tokens:
        usage["cache_read_input_tokens"] = response.usage.cached_input_tokens
    return {
        "id": response.response_id,
        "type": "message",
        "role": "assistant",
        "model": response.model,
        "content": _anthropic_content_blocks(response.content, response.tool_calls),
        "stop_reason": _TO_ANTHROPIC_STOP[response.stop_reason],
        "stop_sequence": None,
        "usage": usage,
    }


def to_anthropic_tool_result_messages(results: Iterable[ToolResult]) -> list[Payload]:
    """N results become ONE ``role:"user"`` message carrying N ``tool_result`` blocks."""
    blocks: list[Payload] = []
    for result in results:
        block: Payload = {
            "type": "tool_result",
            "tool_use_id": result.call_id,
            "content": result.content,
        }
        if result.is_error:
            block["is_error"] = True
        blocks.append(block)
    if not blocks:
        return []
    return [{"role": "user", "content": blocks}]


def _anthropic_tool_declaration(tool: ToolSchema) -> Payload:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.parameters),
    }


def to_anthropic_request(request: ChatRequest) -> Payload:
    """Serialize a normalized request. ``metadata`` is dropped and never reaches the wire."""
    messages: list[Payload] = []
    for message in request.messages:
        if message.role == "tool" or message.tool_results:
            messages.extend(to_anthropic_tool_result_messages(message.tool_results))
        elif message.role == "system":
            # The dialect has no system message; a stray one folds into the top-level param.
            continue
        elif message.role == "assistant":
            messages.append(
                {
                    "role": "assistant",
                    "content": _anthropic_content_blocks(message.content, message.tool_calls),
                }
            )
        else:
            messages.append({"role": "user", "content": message.content})

    payload: Payload = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    if request.system:
        payload["system"] = request.system
    if request.tools:
        payload["tools"] = [_anthropic_tool_declaration(tool) for tool in request.tools]
    if request.stop_sequences:
        payload["stop_sequences"] = list(request.stop_sequences)
    return payload


def _assert_vocabulary_is_closed() -> None:
    """Both reverse tables cover the whole vocabulary; a new term cannot be half-added."""
    assert set(_TO_OPENAI_STOP) == set(STOP_REASONS)
    assert set(_TO_ANTHROPIC_STOP) == set(STOP_REASONS)


_assert_vocabulary_is_closed()
