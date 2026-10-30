"""The client half of the protocol: what the harness answers when the agent calls back.

Two methods carry the whole conversation. ``session_update`` is the agent narrating - tool calls,
message chunks, plan changes - and ``request_permission`` is the agent asking whether it may act.
Everything else the ACP client surface offers is a capability this harness does not have in v1: a
sub-agent may not read or write files through us, and it may not open a terminal on us. Those
methods answer ``method_not_found`` rather than quietly returning nothing, because a capability
we do not offer must be a refusal the agent can see.

**This class only buffers.** It runs on :mod:`~harnessed.acp.runtime`'s background loop, and both
the event bus's sequence counter and a SQLite connection belong to the thread that made them, so
publishing or writing from here would be a data race with a plausible-looking test. Every
observation lands in a list, and :mod:`~harnessed.acp.delegate` flushes the lists from the
calling thread once the coroutine is done - on the failure paths too, which is exactly what makes
a timeout's partial transcript available.

**Steps are the one thing read before the end.** Each tool call the agent starts or finishes also
goes on :attr:`DelegationClient.steps`, a thread-safe queue, so the calling thread can drain it
while it waits and publish ``acp.tool.called`` and ``acp.tool.finished`` as they happen. That is
what lets the console show the agent working instead of a silence followed by a summary. The
publishing still happens on the calling thread, so the rule above holds.

The permission answer itself is decided by :func:`~harnessed.acp.permissions.decide`, which is
pure and cannot block; when only a person can say, the request goes on
:attr:`DelegationClient.asked` and the coroutine awaits the future the calling thread resolves
(ticket 234). The agent is answered with one of the options *it* offered, matched by kind; if it
offered nothing matching the decision, the turn is cancelled, which is the
fail-closed reading of "we could not say yes in a way you understand".
"""

from __future__ import annotations

import asyncio
import queue
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from typing import Any

from acp import Client, RequestError
from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallUpdate,
)

from harnessed.acp.permissions import Decision, Pending, Policy, Prompter, decide, option_for

__all__ = ["DelegationClient", "PermissionAsked"]

#: What a tool call is called for permission purposes: its ACP kind when it has one, its title
#: otherwise. ``agents.toml`` lists kinds, and a kind is the only name two different agents agree
#: on - a title is prose.
_UNNAMED = "other"

#: The tool-call statuses that end a step. ``pending`` and ``in_progress`` are the step still going.
_TERMINAL: frozenset[str] = frozenset({"completed", "failed"})


@dataclass
class PermissionAsked:
    """One request the agent made, handed to the calling thread on :attr:`DelegationClient.asked`.

    ``decision`` is the answer when the configuration gave one at once, and ``None`` when only a
    person can (``Pending``). ``answered`` is what the coroutine awaits: already resolved for a
    rule-decided request, resolved by the calling thread for an open one (ticket 234, decision 74).
    """

    tool_kind: str
    description: str
    decision: Decision | None
    #: The agent's own tool-call input, as sent (ticket 288); ``None`` when it sent none.
    raw_input: Any = None
    answered: ConcurrentFuture[Decision] = field(default_factory=ConcurrentFuture)


@dataclass
class DelegationClient(Client):
    """One delegation's client. Mutable on purpose: it is a recorder, not a value."""

    policy: Policy
    prompter: Prompter | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    #: Every permission request, for the calling thread to record and publish live (never batched).
    asked: queue.SimpleQueue[PermissionAsked] = field(default_factory=queue.SimpleQueue)
    messages: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    steps: queue.SimpleQueue[dict[str, str]] = field(default_factory=queue.SimpleQueue)
    #: Each call's title by id, because a finishing update rarely repeats it.
    titles: dict[str, str] = field(default_factory=dict)
    #: The login methods ``initialize`` advertised (ticket 175): none means the agent said nothing.
    auth_methods: tuple[str, ...] = ()

    # ---- the two methods that matter ----------------------------------------------------

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Record one narration frame and harvest the two things the summary needs from it."""
        body = _dump(update)
        self.transcript.append({"kind": "session_update", "session_id": session_id, **body})
        text = _chunk_text(body)
        if text:
            self.messages.append(text)
        for path in _locations(body):
            if path not in self.files_changed:
                self.files_changed.append(path)
        step = _step(body)
        if step is not None:
            if step["phase"] == "called":
                self.titles[step["tool_call_id"]] = step["title"]
            elif not step["title"]:
                step["title"] = self.titles.get(step["tool_call_id"], "")
            self.steps.put(step)

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        """Decide, record the decision, and answer with one of the agent's own options."""
        tool = tool_call.kind or tool_call.title or _UNNAMED
        self.transcript.append(
            {
                "kind": "permission_request",
                "session_id": session_id,
                "tool": tool,
                "options": [option.option_id for option in options],
            }
        )
        decided = decide(self.policy, tool, prompter=self.prompter)
        description = tool_call.title or ""
        if isinstance(decided, Pending):
            # Only a person can say, and this coroutine may neither publish nor write: queue the
            # request for the calling thread and wait for the answer it resolves.
            asked = PermissionAsked(tool, description, None, tool_call.raw_input)
            self.asked.put(asked)
            decision = await asyncio.wrap_future(asked.answered)
        else:
            decision = decided
            asked = PermissionAsked(tool, description, decision, tool_call.raw_input)
            asked.answered.set_result(decision)
            self.asked.put(asked)
        chosen = _chosen(decision, options)
        self.transcript.append(
            {
                "kind": "permission_decision",
                "session_id": session_id,
                "tool": tool,
                "allowed": decision.allowed,
                "rule": decision.rule,
                "option_id": chosen or "",
            }
        )
        if chosen is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=chosen)
        )

    # ---- capabilities this harness does not offer ---------------------------------------

    async def write_text_file(self, *args: Any, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/kill")

    async def create_elicitation(self, *args: Any, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("elicitation/create")

    async def complete_elicitation(self, *args: Any, **kwargs: Any) -> None:
        raise RequestError.method_not_found("elicitation/complete")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        """A notification has no answer, so an unknown one is dropped rather than raised."""
        self.transcript.append({"kind": "ext_notification", "method": method})

    def on_connect(self, conn: Any) -> None:
        """The SDK offers the connection back. Nothing here needs it."""

    # ---- what delegate.py reads ---------------------------------------------------------

    def summary(self) -> str:
        """Everything the agent said, joined. This is the one field the parent model reads."""
        return "".join(self.messages).strip()


# ---- internals -------------------------------------------------------------------------------


def _step(body: dict[str, Any]) -> dict[str, str] | None:
    """One started or finished tool call, in the words the agent used, or ``None``.

    A started call carries its kind and title. A finished one carries its status, and its title
    only when the update repeats it. Message chunks, plans and usage are not steps: they are
    hundreds of frames per delegation and stay in the transcript.
    """
    update = body.get("sessionUpdate")
    call_id = str(body.get("toolCallId") or "")
    if update == "tool_call":
        return {
            "phase": "called",
            "tool_call_id": call_id,
            "kind": str(body.get("kind") or _UNNAMED),
            "title": str(body.get("title") or ""),
        }
    status = str(body.get("status") or "")
    if update == "tool_call_update" and status in _TERMINAL:
        return {
            "phase": "finished",
            "tool_call_id": call_id,
            "status": status,
            "title": str(body.get("title") or ""),
        }
    return None


def _dump(update: object) -> dict[str, Any]:
    """A JSON-shaped view of one SDK model, or a best-effort mapping when it is already one."""
    dump = getattr(update, "model_dump", None)
    if callable(dump):
        body: dict[str, Any] = dump(mode="json", by_alias=True, exclude_none=True)
        return body
    return {"update": repr(update)}


def _chunk_text(body: dict[str, Any]) -> str:
    """The text of an agent message chunk, or the empty string for any other frame."""
    if body.get("sessionUpdate") != "agent_message_chunk":
        return ""
    content = body.get("content")
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text", ""))
    return ""


def _locations(body: dict[str, Any]) -> list[str]:
    """Every file path a tool-call frame touched, in the order the agent reported them."""
    locations = body.get("locations")
    if not isinstance(locations, list):
        return []
    return [
        str(item["path"])
        for item in locations
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    ]


def _chosen(decision: Decision, options: list[PermissionOption]) -> str | None:
    """The option id matching ``decision``, chosen from what the agent actually offered."""
    kind = option_for(decision.allowed, tuple(option.kind for option in options))
    if kind is None:
        return None
    return next(option.option_id for option in options if option.kind == kind)
