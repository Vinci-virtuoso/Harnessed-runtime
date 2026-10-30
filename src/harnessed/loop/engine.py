"""The whole trick, in one file: reason, act, observe, until one of two guardrails fires (FR-4).

An agent is a while loop around a model that can ask for tools. That is the entire idea, and
this module is written so a person can read it top to bottom in one sitting and see it. Nothing
here is clever; everything that could have been clever was moved out.

* **Reason.** Hand the model the conversation so far and the registry's declarations, and get
  one response back.
* **Act.** If the response asks for no tools, the work is done - that is guardrail 1. Otherwise
  every call it made goes to ``registry.dispatch``, which answers with a
  :class:`~harnessed.providers.types.ToolResult` whether the tool succeeded, raised, was
  never registered, or arrived with arguments that never parsed. Four outcomes, one shape, zero
  branches here.
* **Observe.** Append the results to the transcript so the next turn can read them. A tool
  failure is an observation the model can correct from, never an exception out of this function.

Two exits and no third, both named in ``guardrails.py``. The loop is synchronous - async lives
behind ``acp/runtime.py::submit()`` - and provider-ignorant: it knows a model is something with
a ``complete`` method, and not one thing more. Nothing here decodes a wire shape - that is
quarantined to ``providers/translate.py`` - and there is no tool-name conditional, no vendor
name, and no middleware, plugin, hook or graph abstraction. The absence of indirection is the
point (`loop-readability.md`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from harnessed import clock
from harnessed.loop.guardrails import (
    ITERATION_LIMIT,
    NO_MORE_TOOL_CALLS,
    limit_reached,
    model_is_done,
)
from harnessed.loop.journal import NO_JOURNAL, Ledger
from harnessed.providers.types import ChatRequest, ChatResponse, Message, StopReason, ToolCall

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from harnessed.events.bus import EventBus
    from harnessed.tools.registry import ToolRegistry

__all__ = ["LoopResult", "Model", "run_loop"]

_STARTED = "loop.iteration.started"
_FINISHED = "loop.iteration.finished"
_TRIPPED = "loop.guardrail.tripped"
#: The ceiling a trip counts against: one call to the composition, which is one message.
_SCOPE = "message"


class Model(Protocol):
    """What the loop knows about a provider: it turns one request into one response.

    Structurally identical to ``providers.base.Provider`` and deliberately not imported from
    there. ``loop/`` may reach ``providers.types`` and nothing else under ``providers/``, so the
    seam is restated in the vocabulary of the caller. Anything satisfying one satisfies the
    other, which is what a Protocol is for.
    """

    def complete(self, request: ChatRequest) -> ChatResponse: ...


@dataclass(frozen=True, slots=True)
class LoopResult:
    """What one run produced. The first three fields are positional and stay in this order.

    ``reply``, ``tool_calls`` and ``iterations`` are the answer, the work and the cost, and a
    caller may unpack them positionally. Everything added later is keyword-only with a default,
    so a new field can never displace them (`loop-readability.md`).
    """

    reply: str
    tool_calls: tuple[ToolCall, ...]
    iterations: int
    guardrail: str = field(default=NO_MORE_TOOL_CALLS, kw_only=True)
    messages: tuple[Message, ...] = field(default=(), kw_only=True)
    #: How the model's last turn ended, read off that response and never inferred. ``None`` only
    #: when the run took no turn at all. The guardrail says why the loop stopped; this says how
    #: the answer ended, and the two are independent (decision 27).
    stop_reason: StopReason | None = field(default=None, kw_only=True)

    @property
    def truncated(self) -> bool:
        """The provider cut the last answer off at its token ceiling."""
        return self.stop_reason == "max_tokens"


def run_loop(
    model: Model,
    registry: ToolRegistry,
    request: ChatRequest,
    *,
    limit: int,
    bus: EventBus | None = None,
    journal: Ledger = NO_JOURNAL,
    start_iteration: int = 0,
) -> LoopResult:
    """Run reason-act-observe until a guardrail fires, and report which one did.

    ``limit`` is the configured iteration ceiling, passed in rather than read here because this
    package reads no process variable of its own; the caller resolves it from
    ``Settings.max_iterations``.

    ``journal`` is the run ledger (FR-7). A turn it already holds is replayed rather than asked
    for again, so an interrupted run resumes instead of repeating itself; the default records
    nothing and recognises nothing, which is a run that always starts from the beginning.

    ``start_iteration`` is where a continued session's turns begin (decision 52): the journal
    key is ``start_iteration + taken``, so a follow-up never replays the first message's turns,
    while the ceiling counts ``taken`` alone - it bounds one message, not the whole run.
    """
    messages = list(request.messages)
    calls: list[ToolCall] = []
    reply = ""
    stop_reason: StopReason | None = None
    taken = 0

    while True:
        if limit_reached(taken, limit):
            # guardrail 2: the model would keep going and the ceiling says otherwise.
            return _ended(ITERATION_LIMIT, reply, stop_reason, calls, taken, messages, bus)
        taken += 1
        iteration = start_iteration + taken
        _publish(bus, _STARTED, {"iteration": iteration})

        # ---- reason: the conversation so far, plus every tool the registry declares. A turn the
        # journal already holds is replayed, which is what makes a resumed run free.
        turn = replace(request, messages=tuple(messages), tools=tuple(registry.schemas()))
        recorded = journal.recorded(iteration)
        sent = clock.now()
        response = model.complete(turn) if recorded is None else recorded.response
        messages.append(
            Message(role="assistant", content=response.content, tool_calls=response.tool_calls)
        )
        reply, stop_reason = response.content, response.stop_reason
        calls.extend(response.tool_calls)

        # ---- act: dispatch every call. One shape comes back from all four outcomes.
        if model_is_done(response.tool_calls):
            journal.record(iteration, turn, response, (), requested_at=sent)
            _publish(bus, _FINISHED, {"iteration": iteration, "tool_calls": 0})
            # guardrail 1: the model answered without asking for a tool.
            return _ended(NO_MORE_TOOL_CALLS, reply, stop_reason, calls, taken, messages, bus)
        results = (
            tuple(registry.dispatch(call) for call in response.tool_calls)
            if recorded is None
            else recorded.results
        )

        # ---- observe: the results join the transcript, failures included, and the model reads
        # them on the next turn. This is the one place a run's turn is complete, so it is where
        # the journal persists it - before the next pass, never after (FR-7).
        messages.append(Message(role="tool", tool_results=results))
        journal.record(iteration, turn, response, results, requested_at=sent)
        _publish(bus, _FINISHED, {"iteration": iteration, "tool_calls": len(results)})


# ---- the two small helpers the loop leans on ---------------------------------------------------


def _ended(
    guardrail: str,
    reply: str,
    stop_reason: StopReason | None,
    calls: list[ToolCall],
    iterations: int,
    messages: list[Message],
    bus: EventBus | None,
) -> LoopResult:
    """Announce the guardrail that ended the run, and how the last turn ended, and package both.

    Two facts, not one: a truncated answer ends on guardrail 1 exactly as a finished one does,
    and only ``stop_reason`` tells them apart. ``scope`` says which ceiling ``iterations`` counts
    against: one message's, never the whole session's (decision 52).
    """
    _publish(
        bus,
        _TRIPPED,
        {
            "guardrail": guardrail,
            "iterations": iterations,
            "stop_reason": stop_reason,
            "scope": _SCOPE,
        },
    )
    return LoopResult(
        reply,
        tuple(calls),
        iterations,
        guardrail=guardrail,
        messages=tuple(messages),
        stop_reason=stop_reason,
    )


def _publish(bus: EventBus | None, name: str, payload: dict[str, object]) -> None:
    """Announce one moment of the run, when there is a bus listening."""
    if bus is not None:
        bus.publish(name, payload)
