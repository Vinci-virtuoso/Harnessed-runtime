"""A scripted router and a canned provider for the task composition (ticket 240).

``run_task`` opens its own session through ``run_cmd.open_session``, so a test reaches it by
patching the two builders that session calls: the Decider (the router, answered from a script) and
the provider (one canned reply per turn, advancing the frozen clock when a test wants a message to
take time). Nothing here is a fixture of the committed corpus.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from harnessed import clock
from harnessed.cli import run_cmd
from harnessed.providers.jev import Choice, Decisions, Noul, Question
from harnessed.providers.types import ChatRequest, ChatResponse, Usage

__all__ = ["CannedProvider", "ScriptedDecider", "answer", "capture", "install"]

MODEL = "router-test-model"
EVIDENCE = Path(__file__).resolve().parents[2] / "evidence" / "240"


def capture(name: str, body: Mapping[str, object] | str) -> Path:
    """Write one evidence artefact for ticket 240; regenerated on every run, never committed."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    text = body if isinstance(body, str) else json.dumps(body, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def answer(route: str, confidence: str = "0.90", *, needs_memory: str = "0.2") -> Decisions:
    """One Decider answer: ``route`` at ``confidence``, no memory wanted."""
    return Decisions(
        answers={
            "route": Choice(
                value=route,
                confidence=Decimal(confidence),
                probabilities={route: Decimal(confidence)},
            ),
            "needs_memory": Noul(probability=Decimal(needs_memory)),
            "memory_kind": Choice(
                value="semantic",
                confidence=Decimal("0.6"),
                probabilities={"semantic": Decimal("0.6")},
            ),
        },
        id="gen-task-1",
        model=MODEL,
        usage={"input_tokens": 100, "output_tokens": 10, "cost": "0.0000100"},
        cost="0.0000100",
    )


@dataclass
class ScriptedDecider:
    """Answers each call from the next entry of ``script``; keeps what it was asked."""

    script: Sequence[Decisions]
    states: list[str] = field(default_factory=list)
    asked: list[tuple[Question, ...]] = field(default_factory=list)

    def decide(self, state: str, questions: tuple[Question, ...]) -> Decisions:
        self.states.append(state)
        self.asked.append(questions)
        return self.script[len(self.states) - 1]

    def route_options(self, call: int) -> tuple[str, ...]:
        route = next(q for q in self.asked[call] if q.name == "route")
        return route.options


@dataclass
class CannedProvider:
    """One end-of-turn reply per ``complete``; ``tick_s`` moves the frozen clock on every call."""

    tick_s: float = 0.0
    requests: list[ChatRequest] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if self.tick_s:
            clock.freeze(clock.now() + timedelta(seconds=self.tick_s))
        return ChatResponse(
            response_id=f"resp-{len(self.requests)}",
            model=request.model,
            content=f"step {len(self.requests)} done",
            usage=Usage(input_tokens=10, output_tokens=5),
        )


def install(
    monkeypatch: pytest.MonkeyPatch, decider: ScriptedDecider, provider: CannedProvider
) -> None:
    """Make every session ``run_cmd.open_session`` builds use ``decider`` and ``provider``."""
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings, **_kwargs: decider)
    monkeypatch.setattr(run_cmd, "build_provider", lambda settings, **_kwargs: provider)
