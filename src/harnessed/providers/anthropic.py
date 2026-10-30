"""The second live adapter: the Anthropic Messages API.

FR-9. Second, not special. It exists to keep the OpenAI-compatible shape from quietly becoming
the internal truth (`providers.md`); the loop cannot tell which of the two it is holding, because
both satisfy the same Protocol and both return normalized types.

Nothing here runs in the default test selection: the socket guard blocks the network and the
default provider is ``fake``. The adapter is constructed in tests, never called.
"""

from __future__ import annotations

from typing import Any

import anthropic

from harnessed.config import Settings
from harnessed.errors import TranslationError
from harnessed.providers import quirks, registry, translate
from harnessed.providers.quirks import ProviderCaps
from harnessed.providers.transport import dropped, is_transport
from harnessed.providers.types import ChatRequest, ChatResponse

__all__ = ["AnthropicAdapter", "shape_request"]

#: What to send when an endpoint demands a ceiling and the request carries none.
FALLBACK_MAX_TOKENS = 1024


def shape_request(request: ChatRequest, caps: ProviderCaps) -> dict[str, Any]:
    """Serialize ``request`` for this dialect, then drop what the endpoint cannot accept."""
    payload = translate.to_anthropic_request(request)

    if not caps.supports_temperature:
        payload.pop("temperature", None)

    if caps.requires_max_tokens and not payload.get("max_tokens"):
        payload["max_tokens"] = FALLBACK_MAX_TOKENS

    # Same rule as the other dialect: the request's ceiling is for the answer, and a reasoning
    # model gets its headroom on top (decision 26).
    headroom = quirks.model_caps_for(request.model).reasoning_headroom
    if headroom:
        payload["max_tokens"] = int(payload.get("max_tokens") or 0) + headroom

    if not caps.supports_tools:
        payload.pop("tools", None)
    else:
        for tool in request.tools:
            if len(tool.name) > caps.max_tool_name_length:
                raise TranslationError(
                    f"tool name {tool.name!r} is {len(tool.name)} characters and this endpoint "
                    f"accepts at most {caps.max_tool_name_length}. Rename the tool."
                )

    return payload


class AnthropicAdapter:
    """The Messages API endpoint. Satisfies the ``Provider`` Protocol."""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: str,
        base_url: str | None = None,
        caps: ProviderCaps | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.provider = provider
        self.base_url = base_url if base_url is not None else quirks.base_url_for(provider)
        self.caps = caps if caps is not None else quirks.caps_for(self.base_url)
        # The key check is the registry's, so the message exists in exactly one place. It runs
        # at construction, which is why no call site can reach a raw 401.
        self.api_key = registry.credential_for(settings, provider)
        self._client = anthropic.Anthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=anthropic.DEFAULT_MAX_RETRIES if max_retries is None else max_retries,
        )

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Answer one request, or raise a ``ProviderError`` naming what the endpoint answered.

        Second dialect, same contract as ``openai_compat``: the SDK's status-error base is
        caught and restated, anything wider is a bug in this adapter and stays a traceback.
        """
        try:
            raw = self._client.messages.create(**shape_request(request, self.caps))
        except anthropic.APIStatusError as exc:
            raise registry.provider_error(
                provider=self.provider,
                model=request.model,
                status=exc.status_code,
                message=str(exc),
            ) from exc
        except anthropic.APIConnectionError as exc:
            if is_transport(exc):
                raise dropped(self.provider, exc) from exc
            raise
        return translate.from_anthropic_response(raw.model_dump())
