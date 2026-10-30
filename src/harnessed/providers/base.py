"""The provider seam: one method, checked statically and at runtime.

FR-2. Every adapter and FakeProvider satisfies this Protocol, which is what lets the loop stay
provider-ignorant and lets every offline test swap in a fake without a subclass hierarchy.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from harnessed.providers.types import ChatRequest, ChatResponse

__all__ = ["Provider"]


@runtime_checkable
class Provider(Protocol):
    """Anything that can turn a normalized request into a normalized response."""

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Answer one request. Raises a child of ``ProviderError`` and nothing else."""
        ...
