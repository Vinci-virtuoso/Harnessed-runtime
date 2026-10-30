"""The primary live adapter: every OpenAI-compatible endpoint, one class.

FR-9. There is no subclass per vendor and no ``if`` per vendor. The differences between OpenAI,
Groq, DeepSeek, OpenRouter and a local server are :class:`~harnessed.providers.quirks.
ProviderCaps` values looked up by hostname, and this module reads those flags to shape the
payload ``translate.py`` produced. Adding an endpoint changes the table, not this file.

Nothing here runs in the default test selection: the socket guard blocks the network and the
default provider is ``fake``. The adapter is constructed in tests, never called.
"""

from __future__ import annotations

from typing import Any

import openai

from harnessed.config import Settings
from harnessed.errors import TranslationError
from harnessed.providers import quirks, registry, translate
from harnessed.providers.quirks import ProviderCaps
from harnessed.providers.transport import dropped, is_transport
from harnessed.providers.types import ChatRequest, ChatResponse

__all__ = ["OpenAICompatAdapter", "shape_request"]

#: Local servers speak the dialect but authenticate nobody. The SDK still insists on a string.
LOCAL_PLACEHOLDER_KEY = "no-key-required"


def shape_request(request: ChatRequest, caps: ProviderCaps) -> dict[str, Any]:
    """Serialize ``request`` for this dialect, then drop what the endpoint cannot accept.

    ``translate.py`` owns the shape; this function owns the endpoint's refusals. Keeping them
    apart is what lets the translation stay a pure data function with one golden test.
    """
    payload = translate.to_openai_request(request)

    # A reasoning model spends output tokens before the answer; the request's ceiling is for the
    # answer, so the wire gets the model's headroom on top (decision 26).
    headroom = quirks.model_caps_for(request.model).reasoning_headroom
    if headroom:
        payload["max_tokens"] = request.max_tokens + headroom

    # Structured output needs the endpoint and the model both (decision 98); either alone sends
    # nothing, and the caller's parser checks the same schema.
    if (
        request.response_schema is not None
        and caps.json_schema
        and quirks.model_caps_for(request.model).strict_schema
    ):
        payload["response_format"] = translate.openai_response_format(request.response_schema)

    if not caps.supports_temperature:
        payload.pop("temperature", None)

    if not caps.supports_system_role:
        # The endpoint has no system role, so the instruction becomes the opening user turn
        # rather than being silently dropped.
        for message in payload["messages"]:
            if message.get("role") == "system":
                message["role"] = "user"

    if not caps.supports_tools:
        payload.pop("tools", None)
    elif request.tools:
        for tool in request.tools:
            if len(tool.name) > caps.max_tool_name_length:
                raise TranslationError(
                    f"tool name {tool.name!r} is {len(tool.name)} characters and this endpoint "
                    f"accepts at most {caps.max_tool_name_length}. Rename the tool."
                )
        if not caps.supports_parallel_tool_calls:
            payload["parallel_tool_calls"] = False

    return payload


class OpenAICompatAdapter:
    """One live endpoint, selected by hostname. Satisfies the ``Provider`` Protocol."""

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
        self._client = openai.OpenAI(
            api_key=self.api_key or LOCAL_PLACEHOLDER_KEY,
            base_url=self.base_url,
            max_retries=openai.DEFAULT_MAX_RETRIES if max_retries is None else max_retries,
        )

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Answer one request, or raise a ``ProviderError`` naming what the endpoint answered.

        The catch is the SDK's status-error base and nothing wider: a 4xx or 5xx is a report
        from the endpoint and belongs to the operator, while anything else raised in here is a
        bug in this adapter and has to stay a traceback. This is the only place in the package
        that may name that exception, which is what keeps ``openai`` a word only ``providers/``
        knows (`providers.md`).
        """
        try:
            raw = self._client.chat.completions.create(**shape_request(request, self.caps))
        except openai.APIStatusError as exc:
            raise registry.provider_error(
                provider=self.provider,
                model=request.model,
                status=exc.status_code,
                message=str(exc),
            ) from exc
        except openai.APIConnectionError as exc:
            if is_transport(exc):
                raise dropped(self.provider, exc) from exc
            raise
        return translate.from_openai_response(raw.model_dump())
