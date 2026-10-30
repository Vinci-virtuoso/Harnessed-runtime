"""A Decider double that always declares its scope (ticket 164).

``route.decide`` never passes ``scope`` or ``hop`` to a live ``Decider.decide(state, questions)``
call (the two are keyword-only parameters :class:`~harnessed.providers.fake_decider.FakeDecider`
itself declares for a test, never read off a request - see that module's own docstring). A fixture
keyed on a *declared* scope, rather than the hash fallback every real message gets, therefore needs
a caller that names the scope explicitly: this class does exactly that, wrapping a real
``FakeDecider`` so the committed corpus is still parsed through the real ``parse()``, just picked
by a scope a live caller can never name.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harnessed.providers.fake_decider import FakeDecider
from harnessed.providers.jev import Decisions, Question

__all__ = ["ScopedFakeDecider"]


@dataclass
class ScopedFakeDecider:
    """``fake.decide(state, questions, scope=scope)`` on every call, counted."""

    fake: FakeDecider
    scope: str
    states: list[str] = field(default_factory=list)

    def decide(self, state: str, questions: tuple[Question, ...]) -> Decisions:
        self.states.append(state)
        return self.fake.decide(state, questions, scope=self.scope)

    @property
    def calls(self) -> int:
        return len(self.states)
