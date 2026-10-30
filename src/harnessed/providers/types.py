"""The internal truth. No vendor wire shape is canonical - these types are.

Everything outside ``providers/`` speaks only what is declared here. The OpenAI-compatible
dialect is the primary live path and Anthropic is the second adapter, but both are translations
(`providers.md`, DECISIONS 2026-08-22). That is the harness-outlives-the-vendor bet: swapping a
vendor costs one adapter and one fixture directory, and changes nothing above this line.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

__all__ = [
    "STOP_REASONS",
    "ChatRequest",
    "ChatResponse",
    "ConsolidatedFact",
    "Message",
    "ResponseSchema",
    "Role",
    "StopReason",
    "ToolCall",
    "ToolResult",
    "ToolSchema",
    "Usage",
]

Role = Literal["system", "user", "assistant", "tool"]

#: The one normalized stop vocabulary. Both dialects map onto exactly these terms; anything a
#: vendor invents later lands on ``other`` rather than leaking a vendor word upward.
StopReason = Literal[
    "end_turn", "tool_use", "max_tokens", "stop_sequence", "content_filter", "other"
]

STOP_REASONS: Final[tuple[StopReason, ...]] = (
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "content_filter",
    "other",
)


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counters, named once. Anthropic and OpenAI-compatible both fold into this.

    ``cached_input_tokens`` is a breakdown of ``input_tokens``, never an addition to it: a token
    served from the provider's prompt cache is still an input token, so ``total_tokens`` ignores it.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to run a tool.

    ``arguments`` is **always** a parsed dict, even when the endpoint sent a JSON string and even
    when that string was malformed. ``parse_error`` is the typed marker for the malformed case -
    a field, deliberately not a magic key inside the dict the model controls, so it cannot
    collide with a real argument and mypy can see it. Its serialized name on the wire and in the
    ledger is ``_arg_parse_error``; ``translate.py`` owns that spelling.
    """

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of one tool call. One shape for success, failure and unknown tool alike."""

    call_id: str
    content: str = ""
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """A tool declaration in normalized form. Each dialect wraps it differently."""

    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of history.

    A ``tool`` message carries N results as a batch, because that is what the loop produces after
    one round of parallel calls. How the batch reaches the wire is a dialect difference and lives
    in ``translate.py``: one ``user`` message for Anthropic, N ``tool`` messages for the
    OpenAI-compatible path.
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ResponseSchema:
    """A JSON Schema the reply must satisfy (decision 98).

    Sent on the wire only where the endpoint and the model both enforce it; elsewhere the caller's
    own parser validates the same schema.
    """

    name: str
    schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """One completion request, in normalized form.

    ``metadata`` is a harness-only field. FakeProvider keys its fixture selection on
    ``metadata["scope"]`` (see THE CRUX), and ``translate.py`` drops it on the way out, so it
    never reaches a real endpoint. ``response_schema`` is a request for structured output that
    FakeProvider ignores and ``quirks.py``'s capability tables decide whether to send.
    """

    model: str
    messages: tuple[Message, ...] = ()
    system: str = ""
    tools: tuple[ToolSchema, ...] = ()
    max_tokens: int = 1024
    temperature: float = 0.0
    stop_sequences: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    response_schema: ResponseSchema | None = None


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """One completion response, in normalized form."""

    response_id: str
    model: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class ConsolidatedFact:
    """One fact a consolidation reply claims (ticket 247, FR-149; decision 98).

    ``actor`` is who the fact is about or who did the work, ``path`` the place it belongs to or
    ``None``, ``kind`` one of preference, convention, setup or decision. ``relation`` is ``new`` or
    ``contradicts`` (v6's ``kind``); ``replaces`` is the ``mem_id`` a ``contradicts`` fact retires,
    ``None`` for a ``new`` one. Built only by ``translate.consolidation_facts``.
    """

    text: str
    actor: str = "operator"
    path: str | None = None
    kind: Literal["preference", "convention", "setup", "decision"] = "preference"
    relation: Literal["new", "contradicts"] = "new"
    replaces: str | None = None
