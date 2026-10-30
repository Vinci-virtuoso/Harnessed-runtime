"""A provider that only ever fails, so fail-open can be proven by injection (FR-12).

The retrieval gate's promise is that *any* error in it leaves retrieval running. The honest way
to test that is to hand the gate a provider that raises, because a monkeypatched internal proves
something about the patch rather than about the seam: this class satisfies the same one-method
Protocol every adapter satisfies, so the gate cannot tell it apart from a real one until it
calls it.

It counts its calls for the same reason ``RaisingObserver`` does - failing and never being
consulted are two different things, and only the counter tells them apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harnessed.errors import ProviderError
from harnessed.providers.types import ChatRequest, ChatResponse

__all__ = ["RaisingProvider"]


@dataclass
class RaisingProvider:
    """Raise ``error(message)`` on every ``complete()``, recording the request it refused."""

    message: str = "the gate's provider is unavailable"
    error: type[ProviderError] = ProviderError
    seen: list[ChatRequest] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.seen.append(request)
        raise self.error(self.message)

    @property
    def calls(self) -> int:
        """How many times the gate actually reached the provider before giving up."""
        return len(self.seen)
