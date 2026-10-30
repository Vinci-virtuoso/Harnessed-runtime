"""Fault injection for the live-retry claims (ticket 231, FR-160): a test seam, never a predicate
inside FakeProvider or FakeDecider (MUST rule 2).

``FaultyProvider`` and ``FaultyDecider`` wrap the real offline double and consume a script, one
entry per call: an exception is raised, ``None`` lets the call through to the wrapped double. A
script that has run out lets every later call through, so "one drop" is ``[drop]`` and "three
drops" is ``[drop, drop, drop]``. Nothing here opens a socket.
"""

from __future__ import annotations

from collections.abc import Sequence

from harnessed.errors import DecisionDropped, TransportDropped
from harnessed.providers.base import Provider
from harnessed.providers.jev import Decider, Decisions, Question
from harnessed.providers.types import ChatRequest, ChatResponse

__all__ = ["FaultyDecider", "FaultyProvider", "decision_drop", "drop"]


def drop(error_class: str = "SSLError") -> TransportDropped:
    """The error an adapter raises for a lost connection."""
    return TransportDropped(f"the connection dropped: {error_class}.", error_class=error_class)


def decision_drop(error_class: str = "SSLError") -> DecisionDropped:
    """The error ``JevDecider`` raises for a lost connection."""
    return DecisionDropped(f"the connection dropped: {error_class}.", error_class=error_class)


class FaultyProvider:
    """A ``Provider`` that raises its script's faults before delegating."""

    def __init__(self, inner: Provider, script: Sequence[BaseException | None]) -> None:
        self._inner = inner
        self._script = list(script)
        self.calls = 0

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        fault = self._script.pop(0) if self._script else None
        if fault is not None:
            raise fault
        return self._inner.complete(request)


class FaultyDecider:
    """A ``Decider`` that raises its script's faults before delegating."""

    def __init__(self, inner: Decider, script: Sequence[BaseException | None]) -> None:
        self._inner = inner
        self._script = list(script)
        self.calls = 0

    def decide(self, state: str, questions: tuple[Question, ...]) -> Decisions:
        self.calls += 1
        fault = self._script.pop(0) if self._script else None
        if fault is not None:
            raise fault
        return self._inner.decide(state, questions)
