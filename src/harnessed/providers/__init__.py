"""The provider quarantine.

Everything above this package speaks :mod:`harnessed.providers.types` and nothing else. Wire
shapes exist only in :mod:`harnessed.providers.translate`.

:func:`build_provider` lives here for a different reason: FR-27 says an unconfigured harness
must fail with instructions rather than quietly replay fixtures, and the single door every
command walks through to obtain a provider is the only place that can be true of. It names no
provider at all - the name arrives already resolved on ``Settings``.

:func:`validate_credential` lives here rather than in the CLI for the same reason: a rejected
key arrives as the vendor SDK's own exception, and ``providers/`` is the only package permitted
to know that such a thing exists. What leaves this function is a ``ProviderError`` naming the
provider, the endpoint and the status, and nothing the operator typed.
"""

from __future__ import annotations

from typing import Final

from harnessed.config import Settings
from harnessed.errors import ProviderError
from harnessed.providers.base import Provider
from harnessed.providers.types import (
    ChatRequest,
    ChatResponse,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSchema,
    Usage,
)

__all__ = [
    "ChatRequest",
    "build_provider",
    "ChatResponse",
    "Message",
    "Provider",
    "Role",
    "StopReason",
    "ToolCall",
    "ToolResult",
    "ToolSchema",
    "Usage",
    "validate_credential",
]

#: The cheapest thing that still proves a credential: one token, one message, no tools. FR-26
#: says exactly one call, so this is the whole validation.
VALIDATION_PROMPT: Final = "ping"
VALIDATION_MAX_TOKENS: Final = 1


def build_provider(settings: Settings) -> Provider:
    """The configured provider, or ``ProviderNotConfiguredError`` naming the fix (FR-27).

    There is no fallback arm here and there must never be one: the registry can build an offline
    provider without a key, so a default would be indistinguishable from a working harness right
    up until the operator noticed their agent was replaying somebody else's recordings.

    ``require_provider`` is asked first, and asked for its own sake - the registry would fail on
    an unresolved name too, but with a sentence about an unknown provider rather than the one an
    operator who has configured nothing needs to read.
    """
    from harnessed.providers import registry

    settings.require_provider()
    return registry.create(settings)


def validate_credential(settings: Settings, provider: str) -> None:
    """Make one cheap call and return, or raise ``ProviderError`` naming what refused it.

    The imports are function-local because this module is the package's own ``__init__``: a
    top-level import of :mod:`~harnessed.providers.registry` would run while this file is still
    being defined, and a top-level import of a vendor SDK would pull one into every offline run.

    What is caught is the two vendor error trees plus ``ProviderError``, never ``Exception``:
    the broad catch is sanctioned in exactly the two seams ``tests/test_events.py`` enumerates,
    and widening it here would also swallow a mistake in this function and report it to the
    operator as a rejected credential.

    The refusal reports the provider, the endpoint tried and the status the endpoint answered
    with. It deliberately does not carry the original message: an SDK is free to echo the
    credential back inside it, and this string is printed.
    """
    import anthropic
    import openai

    from harnessed.providers import quirks, registry
    from harnessed.providers.types import ChatRequest, Message

    endpoint = settings.base_url or quirks.base_url_for(provider) or "no endpoint"
    client = registry.create(settings)
    request = ChatRequest(
        model=registry.model_for(settings, provider),
        messages=(Message(role="user", content=VALIDATION_PROMPT),),
        max_tokens=VALIDATION_MAX_TOKENS,
    )
    try:
        client.complete(request)
    except (ProviderError, anthropic.AnthropicError, openai.OpenAIError) as exc:
        status = getattr(exc, "status_code", None)
        answered = type(exc).__name__ if status is None else str(status)
        raise ProviderError(
            f"provider {provider!r} refused the credential: {endpoint} answered {answered}. "
            f"Nothing was written."
        ) from exc
