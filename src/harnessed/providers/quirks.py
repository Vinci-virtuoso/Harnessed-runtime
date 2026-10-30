"""Endpoint differences, expressed once, as flags keyed on hostname.

FR-9. The whole point of this module is that it is a table and not a branch. An endpoint that
rejects ``temperature``, cannot batch tool calls, or insists on ``max_tokens`` is a different set
of :class:`ProviderCaps` values under a different hostname, never an ``if`` somewhere upstream.
Adding an OpenAI-compatible endpoint therefore costs one entry here plus one fixture, and
``loop/engine.py`` does not move (`providers.md`).

Hostname is the key rather than provider name because the same vendor name can point at a proxy,
a gateway or a local server, and it is the endpoint - not the label - that has the quirk. An
unrecognized hostname falls back to :data:`DEFAULT_CAPS`, the plain OpenAI-compatible shape, which
is what "OpenAI-compatible" means in practice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_CAPS",
    "DEFAULT_MODEL_CAPS",
    "DIALECTS",
    "ENDPOINTS",
    "MODELS",
    "PROVIDER_BASE_URLS",
    "PROVIDER_DEFAULT_MODELS",
    "DEFAULT_ROUTER_CAPS",
    "ROUTER_MODELS",
    "Dialect",
    "ModelCaps",
    "ProviderCaps",
    "RouterCaps",
    "base_url_for",
    "caps_for",
    "default_model_for",
    "hostname_of",
    "model_caps_for",
    "router_caps_for",
]

#: Which wire dialect ``translate.py`` speaks to this endpoint. There are exactly two, and a
#: third would be a new pair of translate functions rather than a new flag.
Dialect = Literal["openai", "anthropic"]

DIALECTS: Final[tuple[Dialect, ...]] = ("openai", "anthropic")


@dataclass(frozen=True, slots=True)
class ProviderCaps:
    """What one endpoint can do. Every field is a difference somebody would otherwise branch on.

    Defaults describe the plain OpenAI-compatible endpoint, so a new entry only spells out the
    ways it deviates.
    """

    dialect: Dialect = "openai"
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = True
    supports_system_role: bool = True
    supports_temperature: bool = True
    requires_max_tokens: bool = False
    max_tool_name_length: int = 64
    #: The endpoint accepts ``response_format`` of type ``json_schema`` (decision 98).
    json_schema: bool = False


#: The shape assumed for any endpoint not in the table. "OpenAI-compatible" is the default
#: because that is the contract a self-hosted or proxied endpoint advertises.
DEFAULT_CAPS: Final = ProviderCaps()

#: Endpoint quirks, keyed on lowercased hostname. This table is the entire cost of a new endpoint.
ENDPOINTS: Final[dict[str, ProviderCaps]] = {
    "api.openai.com": ProviderCaps(),
    "api.groq.com": ProviderCaps(max_tool_name_length=64, json_schema=True),
    # The reasoning models on this endpoint ignore a sampling temperature outright, so sending
    # one is noise on the wire and a lie in the journal.
    "api.deepseek.com": ProviderCaps(supports_temperature=False),
    "openrouter.ai": ProviderCaps(),
    # A local server speaks the dialect but serves one tool call at a time.
    "localhost": ProviderCaps(supports_parallel_tool_calls=False),
    # The Messages API has no system message and no default token ceiling.
    "api.anthropic.com": ProviderCaps(
        dialect="anthropic",
        supports_system_role=False,
        requires_max_tokens=True,
    ),
}


@dataclass(frozen=True, slots=True)
class ModelCaps:
    """What one model does that its endpoint does not decide (decision 26).

    ``reasoning_headroom`` is added to the wire ``max_tokens`` by each adapter's ``shape_request``,
    because a reasoning model spends output tokens thinking before it writes the answer.
    ``ChatRequest.max_tokens`` keeps meaning tokens for the answer.
    """

    reasons: bool = False
    reasoning_headroom: int = 0
    #: The model honours ``strict`` in a ``json_schema`` response format (decision 98).
    strict_schema: bool = False


#: Any model not in the table: no reasoning, no headroom, so its request is byte-identical to one
#: shaped before this table existed.
DEFAULT_MODEL_CAPS: Final = ModelCaps()

#: Model quirks, keyed on model-id prefix; the longest matching prefix wins. The only place in the
#: package a model id may be branched on (architecture MUST rule 19). 4096 is sized from one live
#: observation, where 8 tokens returned an empty answer and 1024 truncated mid-reply.
MODELS: Final[dict[str, ModelCaps]] = {
    "openai/gpt-oss-": ModelCaps(reasons=True, reasoning_headroom=4096, strict_schema=True),
    "gpt-oss": ModelCaps(reasons=True, reasoning_headroom=4096, strict_schema=True),
}

#: Where each provider name points when ``HARNESSED_BASE_URL`` is unset. Hostnames are spelled here
#: and nowhere else in the package, so the table above stays the only place an endpoint is named.
PROVIDER_BASE_URLS: Final[dict[str, str]] = {
    "ollama": "http://localhost:11434/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}


#: The model id each provider is sent when ``HARNESSED_MODEL`` resolves to nothing. A provider
#: name is never a valid model id at any vendor, so there is no generic fallback and never may
#: be: a provider absent from this table has no default and fails loudly instead (ticket 040).
#:
#: **Only ids confirmed against the real endpoint go in here.** ``groq`` was checked live on
#: 2026-08-22 and ``deepseek`` on 2026-09-13; ``openai``, ``anthropic`` and ``openrouter`` were
#: not, and an unverified guess would be the same 404 this table exists to remove. ``ollama`` serves
#: whatever the operator pulled locally, which nobody but they can know. ``fake`` is the id its own
#: committed corpus records, so it is verified against the only endpoint it has.
#:
#: This table goes stale as vendors retire ids, and that cost is accepted deliberately. The
#: mitigation is in ``registry.provider_error``: a rejected model id is reported as possibly
#: stale, which points the next reader here rather than at the loop. It happened once already:
#: DeepSeek retired ``deepseek-v4-flash`` and now serves ``deepseek-flash`` (ticket 086).
PROVIDER_DEFAULT_MODELS: Final[dict[str, str]] = {
    "fake": "fake-1",
    "groq": "openai/gpt-oss-20b",
    "deepseek": "deepseek-flash",
}


def hostname_of(base_url: str | None) -> str:
    """The lowercased host of ``base_url``, with no scheme, port or path.

    A bare host with no scheme is accepted too, because that is what people put in ``.env``.
    """
    if not base_url:
        return ""
    candidate = base_url if "//" in base_url else f"//{base_url}"
    return (urlsplit(candidate).hostname or "").lower()


def caps_for(base_url: str | None, *, table: dict[str, ProviderCaps] | None = None) -> ProviderCaps:
    """The flags for the endpoint at ``base_url``, or :data:`DEFAULT_CAPS` if it is unknown.

    ``table`` is an override for tests and for a caller adding an endpoint of its own; the
    shipped table is the default and is never mutated.
    """
    endpoints = ENDPOINTS if table is None else table
    return endpoints.get(hostname_of(base_url), DEFAULT_CAPS)


def model_caps_for(model: str, *, table: dict[str, ModelCaps] | None = None) -> ModelCaps:
    """The caps of the longest prefix in the table that ``model`` starts with, else the default.

    ``table`` is an override for tests; the shipped table is read at call time, never mutated.
    """
    models = MODELS if table is None else table
    matches = [prefix for prefix in models if prefix and model.startswith(prefix)]
    if not matches:
        return DEFAULT_MODEL_CAPS
    return models[max(matches, key=len)]


@dataclass(frozen=True, slots=True)
class RouterCaps:
    """What one router model can be asked, and how much state it can hold (decision 57, FR-95).

    Keyed on the exact model id in :data:`ROUTER_MODELS`, not a prefix: a router model's question
    set and context budget are facts about that one id, never a family sharing a name.
    """

    questions: tuple[str, ...] = ()
    context_tokens: int = 0


#: A model id absent from :data:`ROUTER_MODELS`: no known question types, no known budget. Never
#: guessed at, so a caller that gets this back can tell "unknown" from "the pinned model's facts".
DEFAULT_ROUTER_CAPS: Final = RouterCaps()

#: Router model quirks, keyed on the exact model id. The only place a router model id may be
#: branched on (architecture MUST rule 19, extended to the router by decision 57).
ROUTER_MODELS: Final[dict[str, RouterCaps]] = {
    "typesafe/jev-1.13-20260917": RouterCaps(
        questions=("noul", "choice", "score"), context_tokens=32_000
    ),
}


def router_caps_for(model: str, *, table: dict[str, RouterCaps] | None = None) -> RouterCaps:
    """The facts about the router model named ``model``, or :data:`DEFAULT_ROUTER_CAPS` if unknown.

    ``table`` is an override for tests; the shipped table is read at call time, never mutated.
    """
    models = ROUTER_MODELS if table is None else table
    return models.get(model, DEFAULT_ROUTER_CAPS)


def base_url_for(provider: str) -> str | None:
    """The default endpoint for ``provider``, or ``None`` when it has none (``fake``)."""
    return PROVIDER_BASE_URLS.get(provider)


def default_model_for(provider: str) -> str | None:
    """``provider``'s confirmed model id, or ``None`` when the table knows of no default.

    ``None`` is a report, not a value to fall back from. ``registry.model_for`` turns it into a
    failure that names the provider and the variable, which is FR-27's pattern applied to the
    model: an unverified guess is worse than a refusal, because the guess reaches the endpoint.
    """
    return PROVIDER_DEFAULT_MODELS.get(provider)
