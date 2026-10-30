"""The dispatch on a router verdict (decision 56; FR-101, FR-103, FR-104).

Called once per message, from ``run_cmd._converse``, after the run is announced and before the
gate or the loop starts. :func:`dispatch` never raises: it returns whichever of the three
verdict shapes the router settled on, and ``_converse`` tells them apart.

A ``groq_chat``/``groq_memory`` verdict at or above its floor is the shape this module does
nothing about: decision 60's v4 path is exactly what ``_converse`` already runs, so returning that
verdict and letting the caller fall through is the whole of it.

An uncertain stop (``needs_human``, or a confidence below the chosen route's floor, FR-103) is
settled the same way (ticket 164): no gate call, no loop, no episode - ``_converse`` publishes
``run.finished`` itself and nothing else runs.

A Decider fallback (FR-104, decision 59) is the third shape (ticket 165): the decision row and
``loop.route.decided`` are already written and published by ``route.decide`` before this function
returns, whichever branch answered. ``_converse`` publishes ``error.raised`` with the cause
(:func:`fallback_payload`) and then runs the exact same v4 path a routed ``groq_chat`` verdict
would - a fallback never reaches an agent and never gives Groq ``delegate_to_agent`` back
(``memory/gate.py`` is unchanged and out of this ticket's scope; ticket 168 is what removes the
tool from the routed registry).

An agent route (``handler == "agent"``, ticket 167, FR-106) is the fourth shape: ``_converse``
calls :func:`build_task` and :func:`build_acceptance` (the one static :data:`ACCEPTANCE` sentence,
never a procedure: MUST rule 34), then :func:`run_agent` with no provider
call of its own in between - **no model ever writes a delegation's arguments**. ``run_agent``
dispatches on the entry's ``kind`` to the one of :mod:`~harnessed.acp.delegate` or
:mod:`~harnessed.acp.cli_runner` that already runs it for the ``delegate_to_agent`` tool; neither
is changed here. The result is a hop output, and since ticket 198 one Groq synthesis pass over it
is the reply (the user turn's retrieved context, never a second retrieval).

**A chain** (ticket 176, FR-117, decision 60): after an agent returns, ``_converse`` calls
:func:`dispatch` again with ``hop`` one higher and the agent's result as ``previous``, which the
state quotes as ``Previous step:`` and which the ``route`` question answers with ``done`` among
its options. An agent route delegates again, handed the previous step through :func:`build_task`;
``done``, a Groq route, an uncertain answer and a fallback all end the chain with one Groq
synthesis pass over the last output.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final

from harnessed import clock
from harnessed.acp import cli_runner, ledger, metering
from harnessed.acp import outputs as acp_outputs
from harnessed.acp.config import BILLING_OPENROUTER, AgentSpec
from harnessed.acp.delegate import Delegation, delegate
from harnessed.acp.pending import Door
from harnessed.errors import (
    AcpVersionMismatch,
    DelegationError,
    DelegationTimeout,
    DirtyTreeRefused,
    QuotaRefused,
)
from harnessed.events.bus import Event, EventBus
from harnessed.memory import db
from harnessed.providers import quirks
from harnessed.providers.jev import Choice, Decider
from harnessed.providers.types import Message
from harnessed.router import decisions as decisions_store
from harnessed.router import route as route_module
from harnessed.router import state as state_module
from harnessed.tasks import store as tasks_store

__all__ = [
    "ACCEPTANCE",
    "FAILED",
    "HOP_REASONS",
    "HOP_RESULTS",
    "NO_CO_AUTHOR",
    "RAISED",
    "SUCCEEDED",
    "build_acceptance",
    "build_task",
    "dispatch",
    "failure_reason",
    "fallback_payload",
    "run_agent",
    "settle",
    "task_door",
    "uncertain_payload",
]

#: Decision 60's one static sentence (MUST rule 34): every agent route's ``acceptance``, whatever
#: procedures are active - a promoted procedure is a trait of Groq's turns, never an agent's.
ACCEPTANCE: Final = "Do the work as described in the task; report what you did and what you found."

#: Ticket 276 (FR-145): the one static line every agent route's task ends with, the same for every
#: entry. A delegated agent runs with ``settingSources = []``, so the operator's own rule never
#: reaches it; this is not a procedure (MUST rule 34) and ``ACCEPTANCE`` stays unchanged.
NO_CO_AUTHOR: Final = (
    "Never add a co-author, a Co-Authored-By line or any other agent attribution "
    "to a commit message."
)

#: Migration 018's two results, and the reasons a failed hop carries (ticket 232, decision 73).
SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"
RAISED: Final = "raised"

#: Every ``result`` and ``result_reason`` migration 018's CHECKs allow: the words the dashboard's
#: ``HUMAN_WORDS`` must hold a row for (``tests/test_dashboard_vocabulary.py``).
HOP_RESULTS: Final = (SUCCEEDED, FAILED)
HOP_REASONS: Final = (
    "timeout",
    "wrote_files",
    "error",
    ledger.QUOTA_REFUSED,
    ledger.DIRTY_TREE,
    "version_mismatch",
    RAISED,
)

#: The marker ``acp/cli_runner.py`` writes into a ``wrote_files`` failure's own message.
_WROTE_FILES_MARKER: Final = "(reason: wrote_files)"

_SETTLE_HOP: Final = (
    "UPDATE decisions SET result = ?, result_reason = ? "
    "WHERE run_id = ? AND message_seq = ? AND hop = ?"
)

#: The two roles a session digest is built from (decision 58); a `tool` message carries no text a
#: router state should quote back.
_SESSION_ROLES = ("user", "assistant")


def _explain(verdict: route_module.Verdict) -> str:
    """One sentence naming why the message fell back to Groq chat."""
    return (
        f"the router could not answer ({verdict.fallback_cause}); falling back to groq_chat "
        f"through the v4 gate, unchanged (decision 59)."
    )


def failure_reason(exc: BaseException) -> str:
    """The ``result_reason`` a hop's failure settles under (migration 018): the exception's class,
    plus the one marker the cli runner puts in a ``wrote_files`` failure's own message."""
    if isinstance(exc, DelegationTimeout):
        return "timeout"
    if isinstance(exc, AcpVersionMismatch):
        return "version_mismatch"
    if isinstance(exc, QuotaRefused):
        return "quota_refused"
    if isinstance(exc, DirtyTreeRefused):
        return ledger.DIRTY_TREE
    if isinstance(exc, DelegationError):
        return "wrote_files" if _WROTE_FILES_MARKER in str(exc) else "error"
    return RAISED


def settle(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    message_seq: int,
    hop: int,
    result: str,
    reason: str | None = None,
) -> None:
    """Fill that hop's own ``decisions`` row with how its handler settled (ticket 232).

    The row was written before the handler began (MUST rule 6); this is the second half of that
    begin-then-settle pair. ``reason`` is ``None`` for a success.
    """
    db.write(conn, ((_SETTLE_HOP, (result, reason, run_id, message_seq, hop)),))


def task_door(
    door: Door, conn: sqlite3.Connection, task_id: str, *, publish: tasks_store.Publish
) -> Door:
    """A task's half of the delegation's permission hooks, wrapped round the run's (decision 75).

    A pending request puts the task to ``needs_human`` / ``permission`` and publishes
    ``task.paused``, after the run's own hook gave the lease up; the answer takes the lease back
    first and only then sets the task ``running`` again, and answers ``False`` (ask again next
    tick) while another run holds it. Whoever answered, the task goes on to its next checkpoint: an
    operator's Deny is not the end of it, and a ceiling denial is the checkpoint's to fail closed.
    """

    def on_pending(permission_id: str) -> None:
        if door.on_pending is not None:
            door.on_pending(permission_id)
        tasks_store.pause_for_permission(conn, task_id, permission_id, publish=publish)

    def on_answered(permission_id: str) -> bool:
        if door.on_answered is not None and not door.on_answered(permission_id):
            return False
        tasks_store.resume_after_answer(conn, task_id)
        return True

    return replace(door, on_pending=on_pending, on_answered=on_answered)


def refusing_now(conn: sqlite3.Connection) -> tuple[str, ...]:
    """The entries that refused for quota inside the window, for the state's ``Recently refusing:``.

    Read here because ``router/state.py`` may not import ``acp`` (ticket 194, decision 58).
    """
    return ledger.recent_refusals(conn, clock.now())


def dispatch(
    conn: sqlite3.Connection,
    decider: Decider | None,
    task: str,
    history: tuple[Message, ...] = (),
    *,
    announced: Event | None,
    model: str,
    pinned: route_module.Verdict | None = None,
    publish: route_module.Publisher | None = None,
    agents: Mapping[str, AgentSpec] | None = None,
    hop: int = 0,
    previous: str | None = None,
    previous_by: str | None = None,
    exclude: tuple[str, ...] = (),
    the_task: str | None = None,
    continuation: bool = False,
) -> route_module.Verdict:
    """Ask the router once for ``task``; return whichever verdict it settled on.

    Returns the :class:`~harnessed.router.route.Verdict` unconditionally - ``_converse`` tells the
    four shapes apart: a Groq route above its floor (``outcome="routed"``, ``handler="groq"``,
    decision 60's v4 path is what the caller runs next), an agent route above its floor
    (``outcome="routed"``, ``handler="agent"``, ticket 167 - :func:`build_task`,
    :func:`build_acceptance` and :func:`run_agent`), an uncertain stop (``outcome="uncertain"``,
    FR-103, settled with no gate call, no loop, no episode) or a fallback
    (``outcome="fallback"``, FR-104, settled by publishing ``error.raised`` with
    :func:`fallback_payload` and then running the v4 path).

    ``history`` is filtered to ``user``/``assistant`` messages here, never in ``router/state.py``:
    that package may not import ``providers.types`` (decision 56's import row), so the one module
    that may name ``Message`` is the one that does the filtering.

    ``announced``, ``model`` and ``publish`` pass straight through to
    :func:`~harnessed.router.route.decide`, which writes the ``decisions`` row and publishes
    ``loop.route.decided`` before this function's own return (MUST rule 6, FR-109). ``agents``
    passes through the same way, and is what lets ``decide`` ever answer with an installed
    entry's own name (ticket 167, FR-102).

    ``hop`` and ``previous`` are a chain's (ticket 176): the hop's position, and the prior agent
    result the state quotes as ``Previous step:``. Both keep their defaults for a message's own
    first decision. ``previous_by`` is the entry that wrote it and ``exclude`` the options that hop
    withholds - both the entry that just answered (ticket 220).

    ``pinned`` is a work-suite case's recorded handler (ticket 236, decision 83): that verdict is
    returned as it stands, no state is built and **0** Decider calls are made, so ``decider`` may
    be ``None``. Nothing is written or published either - the router was never asked.

    ``the_task`` and ``continuation`` are a task message's (ticket 240, decision 77): the task's
    text and figures for the state's ``The task:`` section, and whether this hop-0 question also
    offers ``done``. Both keep their defaults for every message that is not a task's.
    """
    if pinned is not None:
        return pinned
    assert decider is not None and announced is not None, "decided, or pinned"
    session = tuple(
        (message.role, message.content) for message in history if message.role in _SESSION_ROLES
    )
    last_reply = None
    if continuation:
        replies = [content for role, content in session if role == "assistant"]
        last_reply = replies[-1] if replies else None
        # An agent hop's reply is stored as an output, and the session holds only Groq's synthesis
        # of it (ticket 268): the router judges "done" on the agent's own words.
        agent_reply = acp_outputs.latest_summary(conn, announced.run_id)
        if agent_reply is not None and agent_reply.strip():
            last_reply = agent_reply
    state = state_module.build_state(
        conn,
        task,
        session,
        previous=previous,
        previous_by=previous_by,
        refusing=refusing_now(conn),
        the_task=the_task,
        last_reply=last_reply,
    )
    return route_module.decide(
        decider,
        state.text,
        conn=conn,
        announced=announced,
        task=task,
        model=model,
        state_truncated=state.truncated,
        publish=publish,
        agents=agents,
        hop=hop,
        exclude=exclude,
        continuation=continuation,
    )


def fallback_payload(verdict: route_module.Verdict) -> dict[str, object]:
    """A fallback's ``error.raised`` payload (FR-104): the cause :func:`~harnessed.router.route.
    decide` already collapsed whichever of the three Decider exceptions was raised into, never a
    raised exception instance - none survives past ``route.decide``'s own catch.
    """
    return {
        "class": "DecisionError",
        "cause": verdict.fallback_cause,
        "message": _explain(verdict),
    }


def uncertain_payload(verdict: route_module.Verdict) -> dict[str, object]:
    """An uncertain stop's whole reply (AC4): the reason, the floor that failed, and the top
    routes read back with their percent strings **as stored** - :func:`~harnessed.router.
    decisions.round_percent`, the same rounding ``route.decide`` already called once to write the
    ``decision_options`` rows, never a second, independent rounding of the raw value (MUST rule
    29). Never an answer: this is the whole reply for an uncertain stop, never a fallback shape.
    """
    routes: list[dict[str, str]] = []
    if verdict.decisions is not None:
        route_answer = verdict.decisions.answers["route"]
        assert isinstance(route_answer, Choice)
        ranked = sorted(route_answer.probabilities.items(), key=lambda item: item[1], reverse=True)
        routes = [
            {"route": option, "confidence_percent": decisions_store.round_percent(probability)}
            for option, probability in ranked
        ]
    return {
        "stop_reason": verdict.stop_reason,
        "route": verdict.route,
        "floor": None if verdict.floor is None else decisions_store.round_four(verdict.floor),
        "floor_percent": (
            None if verdict.floor is None else decisions_store.round_percent(verdict.floor)
        ),
        "routes": routes,
    }


def build_task(task: str, history: tuple[Message, ...] = (), previous: str | None = None) -> str:
    """An agent route's ``task`` (decision 60, ticket 167): the operator's text, then the session
    digest - built here, in code, with **0** provider calls, so no model ever writes a
    delegation's arguments.

    There is no retrieved context yet: nothing decides an agent route needs memory until the
    router's own ``needs_memory`` answer replaces the v4 gate for it (ticket 169). Until then,
    decision 60's "when memory was retrieved" clause is always false for this door, and this
    function's whole job is the other two parts.

    ``previous`` is a chain's prior agent result (ticket 176): the agent a later hop delegates to
    is handed what the one before it produced, under the same ``Previous step:`` label the
    router's own state uses.
    """
    session = tuple(
        (message.role, message.content) for message in history if message.role in _SESSION_ROLES
    )
    parts = [task]
    if session:
        digest = "\n".join(f"{role}: {content}" for role, content in session)
        parts.append(f"This session so far:\n{digest}")
    if previous is not None:
        parts.append(f"Previous step:\n{previous}")
    parts.append(NO_CO_AUTHOR)
    return "\n\n".join(parts)


def build_acceptance() -> str:
    """An agent route's ``acceptance`` (decision 60): :data:`ACCEPTANCE`, never a procedure."""
    return ACCEPTANCE


def run_agent(
    spec: AgentSpec,
    task: str,
    acceptance: str,
    *,
    home: Path,
    bus: EventBus | None = None,
    store: sqlite3.Connection | None = None,
    message_seq: int | None = None,
    hop: int = 0,
    door: Door | None = None,
    openrouter_key: str | None = None,
    openrouter_url: str | None = None,
    allow_dirty: bool = False,
) -> Delegation:
    """Hand ``task`` to the agent named by an agent-route verdict (decision 60, ticket 167).

    ``spec.kind == "cli"`` calls :func:`~harnessed.acp.cli_runner.run`; every other kind calls
    :func:`~harnessed.acp.delegate.delegate`. Neither is changed by this ticket: both already
    write the ``delegations`` row and publish ``acp.delegation.*``/``acp.tool.*`` on ``bus``
    unchanged, exactly as the ``delegate_to_agent`` tool already calls them.

    ``message_seq`` and ``hop`` pass through to ``delegate``, which carries them onto the
    delegation's ``agent_turns`` rows (ticket 171, FR-112).

    An ``openrouter`` entry runs inside its own metering proxy (ticket 172, FR-113, decision 63):
    ``openrouter_key`` is the operator's key, which only the proxy ever holds, and
    ``openrouter_url`` the upstream, ``quirks.base_url_for("openrouter")`` unless a test names a
    loopback stub. The child gets the proxy's variables; every other entry gets none (MUST rule 33).

    **Outputs** (ticket 181, FR-127, decision 65): the working directory is snapshotted before the
    delegation runs and the diff handed to :func:`~harnessed.acp.outputs.record_hop`, which writes
    the delegation's text result at position 0 and, when the entry declares an ``outputs`` glob,
    the files it matched and the delegation changed at position 1 and up. Recorded only with both
    ``store`` and ``bus`` - the ``delegate_to_agent`` tool's own call into ``acp/delegate.py``
    hands neither and stays exactly as it was.

    A dirty working tree refuses the hop (``DirtyTreeRefused``) unless ``allow_dirty``; the refusal
    still writes the hop's position 0 output (the refusal's own words), which is what ties the
    hop to its ``repo_records`` row for the session messages read (ticket 245, FR-146).
    """
    cwd_path = Path(spec.cwd) if spec.cwd else Path.cwd()
    before = acp_outputs.snapshot(cwd_path)
    task_hash = ledger.task_hash(
        agent=spec.name, cwd=spec.cwd or str(Path.cwd()), task=task, acceptance=acceptance
    )
    try:
        delegation = _dispatch(
            spec,
            task,
            acceptance,
            home,
            bus,
            store,
            message_seq,
            hop,
            door,
            openrouter_key,
            openrouter_url,
            task_hash,
            allow_dirty,
        )
    except DirtyTreeRefused as refused:
        if store is not None and bus is not None:
            acp_outputs.record_hop(
                store,
                run_id=bus.run_id,
                message_seq=message_seq,
                hop=hop,
                task_hash=task_hash,
                summary=str(refused),
                cwd=cwd_path,
                before=before,
                produces=None,
                outputs_glob=None,
            )
        raise
    if store is not None and bus is not None:
        acp_outputs.record_hop(
            store,
            run_id=bus.run_id,
            message_seq=message_seq,
            hop=hop,
            task_hash=task_hash,
            summary=delegation.summary,
            cwd=cwd_path,
            before=before,
            produces=spec.produces,
            outputs_glob=spec.outputs,
        )
    return delegation


def _dispatch(
    spec: AgentSpec,
    task: str,
    acceptance: str,
    home: Path,
    bus: EventBus | None,
    store: sqlite3.Connection | None,
    message_seq: int | None,
    hop: int,
    door: Door | None,
    openrouter_key: str | None,
    openrouter_url: str | None,
    task_hash: str,
    allow_dirty: bool,
) -> Delegation:
    """The delegation itself, inside its metering proxy when the entry bills OpenRouter."""
    if spec.billing != BILLING_OPENROUTER:
        delegation = _run(
            spec, task, acceptance, home, bus, store, message_seq, hop, None, door, allow_dirty
        )
    else:
        if not openrouter_key:
            raise DelegationError(
                f"the agent {spec.name!r} bills OpenRouter, and OPENROUTER_API_KEY is not set. "
                f"Add it to .env; the metering proxy injects it, and the agent never sees it."
            )
        upstream = openrouter_url or quirks.base_url_for(BILLING_OPENROUTER)
        assert upstream is not None
        with metering.proxy(
            spec,
            key=openrouter_key,
            upstream=upstream,
            session_id=None if bus is None else bus.run_id,
            store=store,
            message_seq=message_seq,
            hop=hop,
            task_hash=task_hash,
        ) as proxy:
            delegation = _run(
                spec,
                task,
                acceptance,
                home,
                bus,
                store,
                message_seq,
                hop,
                proxy.child_env(),
                door,
                allow_dirty,
            )
    return delegation


def _run(
    spec: AgentSpec,
    task: str,
    acceptance: str,
    home: Path,
    bus: EventBus | None,
    store: sqlite3.Connection | None,
    message_seq: int | None,
    hop: int,
    proxy_env: Mapping[str, str] | None,
    door: Door | None = None,
    allow_dirty: bool = False,
) -> Delegation:
    """Dispatch on ``spec.kind``, handing the child ``proxy_env`` when there is a proxy."""
    if spec.kind == "cli":
        return cli_runner.run(
            spec,
            task,
            acceptance,
            bus=bus,
            store=store,
            proxy_env=proxy_env,
            allow_dirty=allow_dirty,
        )
    return delegate(
        spec,
        task,
        acceptance,
        home=home,
        bus=bus,
        store=store,
        message_seq=message_seq,
        hop=hop,
        proxy_env=proxy_env,
        door=door,
        allow_dirty=allow_dirty,
    )
