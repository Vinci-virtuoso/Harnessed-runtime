"""``decide(...) -> Verdict``: one Decider call, then the verdict (decisions 58, 59).

The ``decisions`` row and its ``decision_options`` rows are written here, before this function
returns to its one caller (``cli/routed.py::dispatch``) and so before the handler the verdict
names ever starts (MUST rule 6, decision 61). ``loop.route.decided`` is published right after,
still before that return - the same eight columns the row just took, as stored strings (FR-109,
decision 12).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final, Literal

from harnessed.acp.config import AgentSpec
from harnessed.errors import DecisionError, DecisionRefused, DecisionTimeout, DecisionUnreachable
from harnessed.events.bus import Event
from harnessed.providers.jev import Choice, Decider, Decisions, Noul
from harnessed.router import decisions as decisions_store
from harnessed.router.questions import (
    DONE,
    FLOORS,
    MEMORY_KIND_QUESTION,
    NEEDS_MEMORY_QUESTION,
    TASK_DONE_FLOOR,
    build_route_question,
    floor_for,
)

__all__ = ["FallbackCause", "Handler", "Outcome", "Publisher", "StopReason", "Verdict", "decide"]

#: How a decision leaves this module: a name and a payload, exactly ``EventBus.publish``'s shape.
Publisher = Callable[[str, Mapping[str, object]], object]

Outcome = Literal["routed", "uncertain", "fallback"]
#: ``"agent"`` is ticket 167's: any route outside :data:`~harnessed.router.questions.FLOORS` is a
#: registry entry, never Groq and never the human stop - ``cli/routed.py`` reads it to know a
#: verdict is one it must delegate rather than let fall through into the v4 gate-then-loop path.
Handler = Literal["groq", "human", "agent"]
StopReason = Literal["needs_human", "below_floor"]
FallbackCause = Literal["unreachable", "timeout", "refused"]

#: Decision 59's four fallback causes collapse to three here: `DecisionRefused` already covers a
#: non-2xx response and a parsed-but-invalid one alike (``providers/jev.py``'s own docstring), so
#: there is one Python exception type behind each of the three words this module can report.
_CAUSE_OF: Final[dict[type[DecisionError], FallbackCause]] = {
    DecisionUnreachable: "unreachable",
    DecisionTimeout: "timeout",
    DecisionRefused: "refused",
}


@dataclass(frozen=True, slots=True)
class Verdict:
    """One message's routing decision. Not persisted or published by this ticket."""

    outcome: Outcome
    handler: Handler | None
    route: str | None
    confidence: Decimal | None
    floor: Decimal | None
    stop_reason: StopReason | None
    needs_memory: Decimal | None
    memory_kind: str | None
    state_truncated: bool
    fallback_cause: FallbackCause | None
    decisions: Decisions | None

    @classmethod
    def pinned(cls, handler: str) -> Verdict:
        """A verdict nobody decided: the handler a work-suite case was recorded on (decision 83).

        ``groq_chat``/``groq_memory`` pin the Groq handler, any other name an agent entry. Certain
        and above no floor, with ``needs_memory`` at zero so the gate is never asked: a replayed
        case is served from its own fixture, not from whatever memory the home holds.
        """
        return cls(
            outcome="routed",
            handler="groq" if handler in FLOORS else "agent",
            route=handler,
            confidence=Decimal(1),
            floor=None,
            stop_reason=None,
            needs_memory=Decimal(0),
            memory_kind=None,
            state_truncated=False,
            fallback_cause=None,
            decisions=None,
        )


def _fallback(cause: FallbackCause, *, state_truncated: bool) -> Verdict:
    return Verdict(
        outcome="fallback",
        handler=None,
        route=None,
        confidence=None,
        floor=None,
        stop_reason=None,
        needs_memory=None,
        memory_kind=None,
        state_truncated=state_truncated,
        fallback_cause=cause,
        decisions=None,
    )


def decide(
    decider: Decider,
    state: str,
    *,
    conn: sqlite3.Connection,
    announced: Event,
    task: str,
    model: str,
    state_truncated: bool = False,
    publish: Publisher | None = None,
    agents: Mapping[str, AgentSpec] | None = None,
    hop: int = 0,
    exclude: tuple[str, ...] = (),
    continuation: bool = False,
) -> Verdict:
    """Ask the ``route``/``needs_memory``/``memory_kind`` triple once, then the verdict (decision
    59). ``route`` is :func:`~harnessed.router.questions.build_route_question`'s, widened by
    ``agents`` - the fixed Groq pair and ``needs_human`` when ``agents`` names none, exactly
    :data:`~harnessed.router.questions.QUESTIONS`'s own shape.

    A `DecisionError` is a fallback verdict, never a raised exception: the caller decides what to
    do with "the router itself could not answer" - ``cli/routed.py::dispatch`` returns it
    unchanged, and ``run_cmd._converse`` publishes ``error.raised`` with the cause before running
    the same v4 path a routed ``groq_chat`` verdict would (ticket 165).

    ``announced`` is the message's own ``run.started`` (``run_cmd.announce`` returns it), so its
    ``run_id`` and ``seq`` are the row's key and its ``message_seq`` (decision 61); ``task`` is the
    raw prompt, scrubbed once at write rather than here; ``model`` is the configured router model,
    used only when the call never answers to name what was asked. The row and its options are
    written before this function returns, whichever branch answers, then ``loop.route.decided`` is
    published through ``publish`` (``EventBus.publish``, wired in by the one caller that has a bus)
    before this function returns - ``publish=None`` is a test double's opt-out, never taken in
    ``cli/routed.py::dispatch``'s own call.

    ``agents`` names the per-entry ``floor`` override :func:`~harnessed.router.questions.floor_for`
    reads for a route outside :data:`~harnessed.router.questions.FLOORS` (decision 59, FR-103), and
    widens the ``route`` question so an installed entry can ever be answered (ticket 167,
    FR-106): a route it names comes back with ``handler="agent"``, never ``"groq"``.

    ``hop`` is the chain position (ticket 176, FR-117): ``0`` for a message's own decision, one
    more for each decision asked after an agent returns. A hop's ``route`` question offers
    :data:`~harnessed.router.questions.DONE` last, and a ``done`` answer is ``routed`` with no
    handler and no floor - it ends the chain whatever its confidence, exactly as an uncertain
    answer would, so a floor would decide nothing. ``exclude`` is the entry that produced the
    previous step: a hop does not offer it, so answering it is a refusal (ticket 220).

    ``continuation`` is a task's own continuation message (ticket 240, decision 77): its hop-0
    ``route`` question offers :data:`~harnessed.router.questions.DONE` last, and a ``done`` answer
    is acted on only when ``done``'s own probability is at or above
    :data:`~harnessed.router.questions.TASK_DONE_FLOOR`, and that probability is the verdict's
    ``confidence`` (ticket 281) - below it the answer is an uncertain stop
    (``below_floor``), never a guess that the job is finished. On a chain hop ``done`` still ends
    the chain whatever its confidence.
    """
    started = time.perf_counter()
    route_question = build_route_question(
        agents,
        hop=hop > 0 or continuation,
        exclude=exclude,
        continuation=continuation and hop == 0,
    )
    questions = (route_question, NEEDS_MEMORY_QUESTION, MEMORY_KIND_QUESTION)
    try:
        answered = decider.decide(state, questions)
    except DecisionError as exc:
        verdict = _fallback(_CAUSE_OF.get(type(exc), "refused"), state_truncated=state_truncated)
        _record(conn, announced, task, model, time.perf_counter() - started, verdict, publish, hop)
        return verdict

    route_answer = answered.answers["route"]
    needs_memory_answer = answered.answers["needs_memory"]
    memory_kind_answer = answered.answers["memory_kind"]
    assert isinstance(route_answer, Choice)
    if exclude and hop > 0:
        # A recorded fixture may price an option this hop never offered; the stored rows hold
        # only what was asked (ticket 220).
        kept = {k: v for k, v in route_answer.probabilities.items() if k not in exclude}
        route_answer = replace(route_answer, probabilities=kept)
        answered = replace(answered, answers={**answered.answers, "route": route_answer})
    assert isinstance(needs_memory_answer, Noul)
    assert isinstance(memory_kind_answer, Choice)

    route = route_answer.value
    confidence = route_answer.confidence
    needs_memory = needs_memory_answer.probability
    memory_kind = memory_kind_answer.value

    if route == DONE:
        task_floor = TASK_DONE_FLOOR if continuation and hop == 0 else None
        # A continuation's bar reads ``done``'s own probability (ticket 281, decision 77), and that
        # figure is the one stored and drawn beside the floor; elsewhere it stays the confidence.
        done_score = (
            route_answer.probabilities.get(DONE, confidence)
            if task_floor is not None
            else confidence
        )
        below_done = task_floor is not None and done_score < task_floor
        verdict = Verdict(
            outcome="uncertain" if below_done else "routed",
            handler=None,
            route=route,
            confidence=done_score,
            floor=task_floor,
            stop_reason="below_floor" if below_done else None,
            needs_memory=needs_memory,
            memory_kind=memory_kind,
            state_truncated=state_truncated,
            fallback_cause=None,
            decisions=answered,
        )
    elif route == "needs_human":
        verdict = Verdict(
            outcome="uncertain",
            handler="human",
            route=route,
            confidence=confidence,
            floor=None,
            stop_reason="needs_human",
            needs_memory=needs_memory,
            memory_kind=memory_kind,
            state_truncated=state_truncated,
            fallback_cause=None,
            decisions=answered,
        )
    else:
        floor = floor_for(route, agents)
        below = confidence < floor
        handler: Handler = "groq" if route in FLOORS else "agent"
        verdict = Verdict(
            outcome="uncertain" if below else "routed",
            handler=handler,
            route=route,
            confidence=confidence,
            floor=floor,
            stop_reason="below_floor" if below else None,
            needs_memory=needs_memory,
            memory_kind=memory_kind,
            state_truncated=state_truncated,
            fallback_cause=None,
            decisions=answered,
        )

    _record(conn, announced, task, model, time.perf_counter() - started, verdict, publish, hop)
    return verdict


def _record(
    conn: sqlite3.Connection,
    announced: Event,
    task: str,
    model: str,
    elapsed: float,
    verdict: Verdict,
    publish: Publisher | None,
    hop: int,
) -> None:
    """The one call site for :func:`~harnessed.router.decisions.record_decision`, so every branch
    of :func:`decide` writes the same shape - before returning to its caller (MUST rule 6) - then
    the one call site for ``loop.route.decided``, so every branch publishes exactly once too
    (decision 61, FR-109)."""
    idx = decisions_store.record_decision(
        conn,
        run_id=announced.run_id,
        message_seq=announced.seq,
        hop=hop,
        task=task,
        model=model,
        latency_seconds=f"{elapsed:.3f}",
        outcome=verdict.outcome,
        route=verdict.route,
        handler=verdict.handler,
        confidence=verdict.confidence,
        floor=verdict.floor,
        needs_memory=verdict.needs_memory,
        memory_kind=verdict.memory_kind,
        stop_reason=verdict.stop_reason,
        fallback_cause=verdict.fallback_cause,
        state_truncated=verdict.state_truncated,
        answered=verdict.decisions,
    )
    if publish is not None:
        publish(
            "loop.route.decided",
            {
                "decision": str(idx),
                "hop": str(hop),
                "route": verdict.route,
                "handler": verdict.handler,
                "outcome": verdict.outcome,
                "confidence": (
                    None
                    if verdict.confidence is None
                    else decisions_store.round_four(verdict.confidence)
                ),
                "confidence_percent": (
                    None
                    if verdict.confidence is None
                    else decisions_store.round_percent(verdict.confidence)
                ),
                "memory_kind": verdict.memory_kind,
            },
        )
