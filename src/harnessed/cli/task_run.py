"""The task composition: a task runs message by message until it is done, spends its budget or
needs the operator (FR-141, decisions 75, 76 and 77).

A task is sent through **the same composition as the operator's own messages** (``run_cmd``), routed
exactly as a console message is. This module only gives that composition its two hooks
(:class:`~harnessed.cli.run_cmd.TaskHooks`) and loops:

- the first message is the task's text (author ``task``); each later one is the **continuation**,
  ``tasks/store.py::CONTINUATION`` with the task's text, written in code and never by a model;
- ``on_announced`` writes who wrote the message as it starts (MUST rule 40) and publishes
  ``task.started`` on the first;
- ``on_settle`` is the checkpoint: the message's own figures are kept and decision 77's order says
  whether the task is done, failed, paused or goes on - all before ``run.finished``;
- a pending permission on the task's session pauses it (``routed.task_door``) and the answer
  resumes it.

``run_task`` is the one call ``harnessed task run`` and the control app's ``resume_with_message``
both make. It never takes the run lease itself: ``run_cmd._converse`` takes it for each message and
gives it up between messages, so a task waiting on the operator holds nothing (MUST rule 39).
"""

from __future__ import annotations

import dataclasses
import functools
import sqlite3
from collections.abc import Callable
from typing import Final

from harnessed import proc
from harnessed.acp import pending
from harnessed.cli import routed, run_cmd
from harnessed.cli.run_cmd import Session, Settled, TaskHooks
from harnessed.errors import (
    DirtyTreeRefused,
    ParameterError,
    RunInFlight,
    SurfaceNotFound,
    TaskAwaitingAnswer,
    TaskStateError,
)
from harnessed.events.bus import Event
from harnessed.loop import messages
from harnessed.memory import db
from harnessed.router import decisions as decisions_store
from harnessed.router import route as route_module
from harnessed.tasks import lease, schedule, store

__all__ = ["Due", "open_task_session", "route_message", "run_due", "run_task", "sweep"]


#: What ``author`` a message may carry; ``operator`` only on a resume that brings the operator's
#: own text.
AUTHORS: Final = ("task", "operator")


def route_message(task: dict[str, str | None]) -> bool:
    """What a message posted to a task's session means, decided before anything is published.

    ``True`` when it resumes the task (paused ``uncertain``); ``False`` when the task is finished
    and the message is an ordinary follow-up with no ``task_messages`` row and no counters.
    Raises :class:`TaskAwaitingAnswer` when the task waits on a permission answer and
    :class:`RunInFlight` when it is running.
    """
    state = str(task["state"])
    session_id = str(task["session_id"] or task["task_id"])
    if state in store.TERMINAL_STATES:
        return False
    if state == "running":
        raise RunInFlight(session_id, str(task["task_id"]), str(task.get("text") or ""))
    if state == "needs_human" and task["pause_reason"] == "permission":
        raise TaskAwaitingAnswer(session_id)
    if state == "needs_human":
        return True
    raise TaskStateError(str(task["task_id"]), state, f"{state}: it has not started")


def reopens(task: dict[str, str | None], allow_dirty: bool) -> bool:
    """Whether this ``task run`` reopens a task the dirty-tree checkpoint failed (ticket 269)."""
    return (
        allow_dirty and task["state"] == "failed" and task["stop_reason"] == store.DIRTY_TREE_STOP
    )


def _runnable(task: dict[str, str | None], message: str | None, allow_dirty: bool = False) -> None:
    """Refuse a ``task run`` the state does not allow."""
    state = str(task["state"])
    if state == "queued":
        if message is not None:
            raise ParameterError(
                "a task that has not started has nobody to answer: leave out the message."
            )
        return
    if reopens(task, allow_dirty):
        return
    if state == "failed" and task["stop_reason"] == store.DIRTY_TREE_STOP:
        raise TaskStateError(
            str(task["task_id"]),
            state,
            "failed: it never started, the working tree had uncommitted changes. "
            "Pass --allow-dirty to hand the tree over as it is, or commit or stash first.",
        )
    if state in store.TERMINAL_STATES:
        raise TaskStateError(str(task["task_id"]), state, f"{state}: it has already finished")
    route_message(task)


def _prepared(session: Session, task: dict[str, str | None], allow_dirty: bool = False) -> Session:
    """The session as this task runs it: its working directory on every agent it may reach, its
    ``entry`` as the only agent, and the permission hooks wrapped with the task's half."""
    agents = session.agents
    entry = task["entry"]
    if entry is not None:
        agents = {name: spec for name, spec in agents.items() if name == entry}
    agents = {name: dataclasses.replace(spec, cwd=task["cwd"]) for name, spec in agents.items()}
    assert session.door is not None, "a routed session carries its door"
    door = routed.task_door(
        session.door, session.conn, str(task["task_id"]), publish=session.bus.publish
    )
    allowed = allow_dirty or store.allows_dirty(session.conn, str(task["task_id"]))
    return dataclasses.replace(session, agents=agents, door=door, allow_dirty=allowed)


def open_task_session(
    task: dict[str, str | None],
    *,
    provider: str | None = None,
    opened: run_cmd.Opened | None = None,
    stopping: Callable[[], bool] | None = None,
    parked: Callable[[str, bool], None] | None = None,
    allow_dirty: bool = False,
) -> Session:
    """The task's session: a new run for a queued task, its own run for one that has a session."""
    session = run_cmd.open_session(
        provider=provider,
        session_id=task["session_id"],
        routed=True,
        answer_door=True,
        opened=opened,
        stopping=stopping,
        parked=parked,
    )
    return _prepared(session, task, allow_dirty)


def _spent(conn: sqlite3.Connection, run_id: str, message_seq: int) -> tuple[int, int]:
    """The message's dollars in nanodollars and its unpriced turns, from decision 64's handler rows
    for this message alone: the unrounded cost each was priced at, rounded once half-up, and the
    turns none could price."""
    unpriced = 0
    for row in messages.for_session(conn, run_id):
        if row.message_seq == message_seq:
            unpriced += sum(handler.not_metered_turns for handler in row.handlers)
    return messages.exact_nanos_for(conn, run_id, message_seq), unpriced


def _uncertain(verdict: route_module.Verdict | None) -> dict[str, str | None]:
    """``task.paused``'s router figures, as stored (decision 61: rounded once, at write)."""
    if verdict is None:
        return {}
    confidence, floor = verdict.confidence, verdict.floor
    return {
        "route": verdict.route,
        "confidence": None if confidence is None else decisions_store.round_four(confidence),
        "confidence_percent": (
            None if confidence is None else decisions_store.round_percent(confidence)
        ),
        "floor": None if floor is None else decisions_store.round_four(floor),
        "floor_percent": None if floor is None else decisions_store.round_percent(floor),
    }


@dataclasses.dataclass(slots=True)
class _Message:
    """What one message of the task is, and where its checkpoint reads from."""

    text: str
    author: str
    first: bool
    continuation: bool
    event: Event | None = None
    settled: bool = False


def _hooks(
    session: Session,
    task: dict[str, str | None],
    message: _Message,
    started: tuple[str, str | None] = ("hand", None),
) -> TaskHooks:
    conn, task_id = session.conn, str(task["task_id"])

    def on_announced(event: Event) -> None:
        message.event = event
        store.begin_message(
            conn,
            task_id,
            run_id=event.run_id,
            message_seq=event.seq,
            author=message.author,
            first=message.first,
            started_by=started[0],
            schedule_id=started[1],
            publish=session.bus.publish,
        )

    def on_settle(event: Event, settled: Settled) -> None:
        _checkpoint(session, task_id, event, settled.ended, settled.verdict)
        message.settled = True

    return TaskHooks(
        on_announced=on_announced,
        on_settle=on_settle,
        continuation=message.continuation,
        the_task=store.state_text(task),
    )


def _checkpoint(
    session: Session,
    task_id: str,
    event: Event,
    ended: str,
    verdict: route_module.Verdict | None = None,
) -> dict[str, str | None]:
    nanos, unpriced = _spent(session.conn, session.bus.run_id, event.seq)
    return store.checkpoint(
        session.conn,
        task_id,
        run_id=session.bus.run_id,
        message_seq=event.seq,
        started_at=event.ts,
        nanos=nanos,
        unpriced=unpriced,
        ended=ended,
        uncertain=_uncertain(verdict) if ended == "uncertain" else None,
        publish=session.bus.publish,
    )


def _next(
    task: dict[str, str | None], first_message: str | None, author: str, opening: bool
) -> _Message:
    """The next message: the task's text at the start, the caller's text on the first message of a
    resume, the continuation otherwise."""
    if task["session_id"] is None:
        return _Message(str(task["text"]), "task", first=True, continuation=False)
    if opening and first_message is not None:
        return _Message(first_message, author, first=False, continuation=False)
    text = store.continuation_for(str(task["text"]))
    return _Message(text, "task", first=False, continuation=True)


def _send(
    session: Session,
    task: dict[str, str | None],
    message: _Message,
    hooks: TaskHooks,
    *,
    announced: Event | None,
) -> None:
    if task["session_id"] is None:
        run_cmd.solve_with_memory(session, message.text, announced=announced, task=hooks)
    else:
        run_cmd.continue_session(
            session, session.bus.run_id, message.text, announced=announced, task=hooks
        )


def run_task(
    task_id: str,
    *,
    first_message: str | None = None,
    author: str = "task",
    provider: str | None = None,
    session: Session | None = None,
    announced: Event | None = None,
    reraise: bool = True,
    started_by: str = "hand",
    schedule_id: str | None = None,
    allow_dirty: bool = False,
    consolidate: bool = True,
) -> dict[str, str | None]:
    """Run ``task_id`` until it is done, fails, needs the operator or is cancelled; its row then.

    A ``queued`` task starts with its text. A task paused ``uncertain`` resumes on its own session
    with the next continuation, or with ``first_message`` as the operator's own text
    (``author="operator"``, ``human_messages`` + 1). ``session`` and ``announced`` are the control
    worker's: the session it opened on its thread and the ``run.started`` it already published.

    A message that raises is settled as the task's own end (``failed`` / ``raised``, before
    ``run.finished``) and then re-raised for ``main`` to render, unless ``reraise`` is false (the
    control worker, whose ``_carry`` would otherwise settle a second time). ``started_by`` is
    ``schedule`` with that schedule's ``schedule_id`` when ``task run --due`` starts it.

    After each message's checkpoint - the message finished, its lease released - consolidation's
    trigger is called (ticket 247, FR-148). The control worker passes ``consolidate=False``: it
    runs the trigger as its own executor job after the run's future completes.
    """
    if author not in AUTHORS:
        raise ParameterError(f"a message's author is one of {AUTHORS}, not {author!r}")
    own = session is None
    if session is None:
        conn = db.connect()
        task = store.get(conn, task_id)
        conn.close()
    else:
        task = store.get(session.conn, task_id)
    if task is None:
        raise SurfaceNotFound(f"no task {task_id!r}: the tasks ledger holds none.")
    _runnable(task, first_message, allow_dirty)
    if session is None:
        session = open_task_session(task, provider=provider, allow_dirty=allow_dirty)
    try:
        if reopens(task, allow_dirty):
            store.reopen_dirty(session.conn, task_id)
        return _drive(
            session,
            task_id,
            first_message,
            author,
            announced,
            reraise,
            (started_by, schedule_id),
            consolidate,
        )
    finally:
        if own:
            session.conn.close()


def _drive(
    session: Session,
    task_id: str,
    first_message: str | None,
    author: str,
    announced: Event | None,
    reraise: bool,
    started: tuple[str, str | None] = ("hand", None),
    consolidate: bool = True,
) -> dict[str, str | None]:
    opening = True
    while True:
        task = store.get(session.conn, task_id)
        assert task is not None
        message = _next(task, first_message, author, opening)
        hooks = _hooks(session, task, message, started)
        try:
            _send(session, task, message, hooks, announced=announced if opening else None)
        except Exception as exc:
            if message.event is None or message.settled:
                raise  # nothing of the task was begun, or its end is already kept
            run_cmd.settle_raised(
                session.bus,
                exc,
                before_finish=functools.partial(
                    _checkpoint,
                    session,
                    task_id,
                    message.event,
                    "dirty_tree" if isinstance(exc, DirtyTreeRefused) else "raised",
                ),
            )
            if reraise:
                raise
            return store.payload(_require(session.conn, task_id))
        opening = False
        if consolidate:
            run_cmd.consolidate_after_message(session)
        task = _require(session.conn, task_id)
        if task["state"] != "running":
            return store.payload(task)


def _require(conn: sqlite3.Connection, task_id: str) -> dict[str, str | None]:
    task = store.get(conn, task_id)
    if task is None:
        raise SurfaceNotFound(f"no task {task_id!r}: the tasks ledger holds none.")
    return task


@dataclasses.dataclass(frozen=True, slots=True)
class Due:
    """What one ``task run --due`` did: the schedules it found due and the tasks it ran."""

    schedules: tuple[str, ...]
    ran: tuple[str, ...]
    in_flight: bool = False
    #: The task holding the lease and the sentence that says so, when the holder is a task.
    running: RunInFlight | None = None


def sweep(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Close what a dead process left behind (decision 78): each ``running`` task, and each
    ``needs_human`` one with a pending permission, whose recorded process is not alive is
    ``failed`` / ``orphaned``, its pending request expired (rule ``ceiling``) and ``task.finished``
    published. The ids closed."""
    closed: list[str] = []
    for task in store.orphan_candidates(conn):
        pid = task["pid"]
        if pid is not None and proc.holds(int(pid), task["pid_started_at"]):
            continue
        if task["pending_permission_id"] is not None:
            pending.expire(conn, str(task["pending_permission_id"]))
        if store.orphan(conn, str(task["task_id"])):
            closed.append(str(task["task_id"]))
    return tuple(closed)


def _refusal(conn: sqlite3.Connection, held: lease.Lease) -> RunInFlight | None:
    """The refusal naming the task that holds the lease, or ``None`` when no task does."""
    found = lease.refusal(conn, held.session_id)
    return found if found.task_id else None


def run_due(
    *,
    run: Callable[..., dict[str, str | None]] = run_task,
    provider: str | None = None,
) -> Due:
    """The scheduler's command: sweep, run each due schedule's ``queued`` tasks, then advance it.

    The sweep comes first (:func:`sweep`), then the lease: a live holder means "in flight" and the
    tick runs nothing, leaving the schedules due for the next one. Tasks go one at a time in queue
    order (``created_at``, then ``task_id``), each through :func:`run_task` with
    ``started_by="schedule"`` and the schedule's id; the lease is looked at again between tasks,
    and a task another tick already took is skipped. A schedule that is due but finds nothing
    queued still advances.
    """
    conn = db.connect()
    try:
        sweep(conn)
        found = schedule.due(conn)
        names = tuple(str(r["schedule_id"]) for r in found)
        ran: list[str] = []
        for row in found:
            schedule_id = str(row["schedule_id"])
            for task_id in store.queued_ids(conn):
                held = lease.holder(conn)
                if held is not None:
                    return Due(names, tuple(ran), True, _refusal(conn, held))
                current = store.get(conn, task_id)
                if current is None or current["state"] != "queued":
                    continue  # another tick took it
                try:
                    run(
                        task_id,
                        provider=provider,
                        reraise=False,
                        started_by="schedule",
                        schedule_id=schedule_id,
                    )
                except RunInFlight as busy:
                    return Due(names, tuple(ran), True, busy if busy.task_id else None)
                except TaskStateError:
                    continue  # it moved between the check and the run
                ran.append(task_id)
            schedule.advance(conn, schedule_id)
        return Due(names, tuple(ran))
    finally:
        conn.close()
