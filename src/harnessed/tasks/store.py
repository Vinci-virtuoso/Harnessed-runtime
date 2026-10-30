"""The tasks ledger: queue a task, read it back, cancel it (FR-140, decision 76).

A task is one row of ``tasks``: scrubbed text, an absolute working directory, a budget stored on
the row whether it was given or defaulted, and a state. This module owns the rows and the verbs
that run nothing; running a task is the task composition's (ticket 240).

``queue`` is the one call ``harnessed task add`` (``via`` ``cli``) and ``POST /control/v1/tasks``
(``via`` ``console``) both make, so the two doors cannot queue a task two ways. ``cancel`` takes the
denial of a pending permission as a callable, because ``tasks`` imports no ``acp``: the door that
owns the answer passes it in.

Running one is the task composition's (``cli/task_run.py``, ticket 240), and this module holds the
three writes it makes: :func:`begin_message` as a message starts (who wrote it, ``running``,
``task.started``), :func:`checkpoint` as it ends (the kept figures and what happens next, decision
77) and the two a pending permission makes, :func:`pause_for_permission` and
:func:`resume_after_answer`. Each one commits before it publishes, so an event never names a state
the ledger does not hold.

Every figure leaves as a string (decision 19): :func:`payload` is the row as stored and
:func:`json_payload` the row as the reads serve it (ticket 241, decision 76): the used figures, the
pause fields, ``unattended`` and a ``*_reason`` beside each null. ``UNATTENDED_SQL`` is the one
expression that says whether a task was completed unattended, used by the row read and by the total
alike.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import repeat
from typing import Final

from harnessed import clock, config, ids, proc
from harnessed.errors import (
    ParameterError,
    PromptError,
    StorageError,
    SurfaceNotFound,
    TaskStateError,
)
from harnessed.events import read as events_read
from harnessed.events.bus import EventBus
from harnessed.memory import db, redact

__all__ = [
    "BUDGET_SOURCES",
    "COMPARED_SQL",
    "CONTINUATION",
    "ENDS",
    "FINISHED",
    "PAUSED",
    "STARTED",
    "Publish",
    "QUEUED_VIA",
    "STATES",
    "STOP_REASONS",
    "TERMINAL_STATES",
    "UNATTENDED_SQL",
    "allows_dirty",
    "begin_message",
    "cancel",
    "checkpoint",
    "continuation_for",
    "for_session",
    "for_session_text",
    "get",
    "listing",
    "listing_payload",
    "payload",
    "pause_for_permission",
    "queue",
    "queue_text",
    "queued_ids",
    "resume_after_answer",
    "json_payload",
    "state_text",
    "totals",
    "totals_payload",
]

#: ``tasks.state`` (decision 76).
STATES: Final = ("queued", "running", "needs_human", "done", "failed", "cancelled")

#: The states a task never leaves.
TERMINAL_STATES: Final = ("done", "failed", "cancelled")

#: The ``stop_reason`` the checkpoint gives a task whose delegation a dirty tree refused.
DIRTY_TREE_STOP: Final = "dirty_tree"

#: ``tasks.stop_reason`` (decision 76).
STOP_REASONS: Final = (
    "finished",
    "budget",
    "ceiling",
    "dirty_tree",
    "orphaned",
    "raised",
    "cancelled",
)

BUDGET_SOURCES: Final = ("default", "given")
QUEUED_VIA: Final = ("cli", "console")

#: The event a task's end publishes, as the first and only event of its own session when nothing
#: was running to publish it inside a message (decision 17, MUST rule 38).
FINISHED: Final = "task.finished"

#: The two events a task's messages publish inside the message (decision 12, FR-141).
STARTED: Final = "task.started"
PAUSED: Final = "task.paused"

#: The message the harness sends after the first: the task's own text, stored verbatim, written in
#: code with no generative call (decision 77).
CONTINUATION: Final = (
    "Continue the task: {text}. Your work so far is above; say when it is finished."
)


def continuation_for(text: str) -> str:
    """:data:`CONTINUATION` filled with ``text``, with no period added after a task that already
    ends in ``.``, ``?`` or ``!`` (ticket 268)."""
    head, tail = CONTINUATION.split("{text}.", 1)
    return f"{head}{text}{'' if text[-1:] in '.?!' else '.'}{tail}"


#: How a message ended, as the composition tells the checkpoint (decision 77's order is the
#: checkpoint's): ``ok`` is a message that ran and has no reason to stop the task.
ENDS: Final = ("ok", "task_done", "uncertain", "raised", "dirty_tree")

#: A callable taking an event name and its payload: ``EventBus.publish``'s shape.
Publish = Callable[[str, Mapping[str, object]], object]

DEFAULT_LIMIT: Final = 50
_NANOS_PER_DOLLAR: Final = 1_000_000_000
_TENTH: Final = Decimal("0.1")
_MILLIONTH: Final = Decimal("0.000001")

_INSERT: Final = (
    "INSERT INTO tasks (task_id, text, cwd, entry, allow_dirty, budget_iterations, budget_seconds, "
    "budget_dollars, budget_nanos, budget_source, state, queued_via, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)"
)


def _now() -> str:
    return clock.isoformat(clock.now())


def _count(name: str, value: int) -> int:
    if value < 1:
        raise ParameterError(f"{name} must be 1 or more, not {value}.")
    return value


def _dollars(value: str) -> tuple[str, int]:
    """``value`` as stored (two places at least, no exponent) and as nanodollars."""
    try:
        amount = Decimal(value.strip())
    except InvalidOperation:
        raise ParameterError(f"the dollar budget must be a number, not {value!r}.") from None
    if not amount.is_finite() or amount <= 0:
        raise ParameterError(f"the dollar budget must be more than 0, not {value!r}.")
    return format(amount, "f"), int(amount * _NANOS_PER_DOLLAR)


def queue(
    conn: sqlite3.Connection,
    text: str,
    *,
    cwd: str | None = None,
    entry: str | None = None,
    budget_iterations: int | None = None,
    budget_seconds: int | None = None,
    budget_dollars: str | None = None,
    allow_dirty: bool = False,
    via: str,
) -> dict[str, str | None]:
    """Write one ``queued`` task and answer it as :func:`payload` serves it.

    An absent budget part is the Settings default, stored on the row like a given one; the row's
    ``budget_source`` is ``given`` when any part was stated and ``default`` when none was. The text
    passes ``redact.scrub`` and the working directory is made absolute here.
    """
    if via not in QUEUED_VIA:
        raise StorageError(f"a task is queued via one of {QUEUED_VIA}, not {via!r}")
    if not isinstance(text, str) or not text.strip():
        raise PromptError("a task needs text: say what it should do.")
    settings = config.load()
    given = any(part is not None for part in (budget_iterations, budget_seconds, budget_dollars))
    iterations = _count(
        "the iteration budget",
        settings.task_budget_iterations if budget_iterations is None else budget_iterations,
    )
    seconds = _count(
        "the seconds budget",
        settings.task_budget_seconds if budget_seconds is None else budget_seconds,
    )
    dollars, nanos = _dollars(
        settings.task_budget_dollars if budget_dollars is None else budget_dollars
    )
    task_id = ids.new_id("task")
    db.write(
        conn,
        (
            (
                _INSERT,
                (
                    task_id,
                    redact.scrub(text.strip()),
                    os.path.abspath(cwd if cwd is not None else os.getcwd()),
                    entry,
                    1 if allow_dirty else 0,
                    iterations,
                    seconds,
                    dollars,
                    nanos,
                    "given" if given else "default",
                    via,
                    _now(),
                ),
            ),
        ),
    )
    return payload(_require(conn, task_id))


def queue_text(text: str, *, via: str) -> dict[str, str | None]:
    """What the control door calls: open the one database, queue with the defaults, close it.

    No budget is a parameter: the browser sets none (FR-140), so a console task always carries the
    Settings defaults and ``budget_source`` ``default``.
    """
    conn = db.connect()
    try:
        return queue(conn, text, via=via)
    finally:
        conn.close()


def _served(
    cursor: sqlite3.Cursor, found: sqlite3.Row | tuple[object, ...]
) -> dict[str, str | None]:
    names = [column[0] for column in cursor.description]
    return {
        name: None if value is None else str(value)
        for name, value in zip(names, found, strict=True)
    }


_TEXT_OR_NULL: Final = {str, type(None)}


def _texts(values: tuple[object, ...]) -> tuple[str | None, ...]:
    """``values`` as text, a ``NULL`` kept: ``map(str)`` in C when none is, a loop when one is."""
    if None in values:
        return tuple(None if value is None else str(value) for value in values)
    return tuple(map(str, values))


def _served_columns(cursor: sqlite3.Cursor) -> tuple[list[str], list[tuple[str | None, ...]]]:
    """The cursor's names and, per name, its column of values as text or ``None``.

    A thousand-row list is a hot path (FR-75's 200 ms), so the work is by column, in C: a column
    whose values are all text or ``NULL`` (most of them) is kept as it came, and only one that
    holds a number is made text, value by value (ticket 265).
    """
    names = [column[0] for column in cursor.description]
    columns = list(zip(*cursor.fetchall(), strict=True)) or [()] * len(names)
    for at, values in enumerate(columns):
        if not set(map(type, values)) <= _TEXT_OR_NULL:
            columns[at] = _texts(values)
    return names, columns


def _served_rows(cursor: sqlite3.Cursor) -> tuple[dict[str, str | None], ...]:
    """Every row the cursor holds as :func:`_served` would give it, the names read once."""
    names, columns = _served_columns(cursor)
    return tuple(map(dict, map(zip, repeat(names), zip(*columns, strict=True))))


#: Whether a task was completed unattended: ``1`` or ``0`` once ``done``, ``NULL`` before. The one
#: expression the row read and ``completed_unattended`` both use (decision 76, G-63).
UNATTENDED_SQL: Final = "CASE WHEN state = 'done' THEN (human_messages = 0 AND pauses = 0) END"

#: ``router/decisions.py::COMPARED_SQL`` word for word (decision 101, FR-171): ``tasks/`` may not
#: import ``router/``, so the copy is held equal by ``tests/test_gateway_tasks.py``. Over the
#: ``decisions`` row aliased ``d`` that :data:`_SELECT` joins in.
COMPARED_SQL: Final = (
    "CASE WHEN d.outcome = 'uncertain' THEN "
    "CASE WHEN d.hop = 0 AND d.route = 'done' "
    "AND EXISTS (SELECT 1 FROM task_messages tm WHERE tm.run_id = d.run_id "
    "AND tm.message_seq = d.message_seq AND tm.kind = 'continuation') "
    "AND d.confidence = (SELECT o.probability FROM decision_options o WHERE o.run_id = d.run_id "
    "AND o.idx = d.idx AND o.question = 'route' AND o.option = 'done') "
    "THEN 'done_probability' ELSE 'confidence' END END"
)

#: A task with the two things a pause points at, joined in so a row carries them (G-39): the
#: request a ``permission`` pause waits on, and the decision row an ``uncertain`` pause stopped at
#: (the last decision of ``paused_message_seq``). Both joins are on keys the ledgers index.
_JOINED: Final = frozenset(
    {
        "unattended",
        "pause_agent",
        "pause_tool_kind",
        "pause_description",
        "pause_route",
        "pause_confidence",
        "pause_confidence_percent",
        "pause_floor",
        "pause_floor_percent",
        "pause_compared",
        "handler",
        "outcome",
    }
)
_SELECT: Final = (
    f"SELECT t.*, {UNATTENDED_SQL} AS unattended, "
    "p.agent AS pause_agent, p.tool_kind AS pause_tool_kind, p.description AS pause_description, "
    "d.route AS pause_route, d.confidence AS pause_confidence, "
    "d.confidence_percent AS pause_confidence_percent, d.floor AS pause_floor, "
    "d.floor_percent AS pause_floor_percent, "
    f"{COMPARED_SQL} AS pause_compared, "
    "m.handler AS handler, "
    # `failed` is `loop/messages.py::OUTCOME_FAILED`: any failed hop of the message makes it so.
    "CASE WHEN m.run_id IS NULL THEN NULL WHEN EXISTS (SELECT 1 FROM decisions f "
    "  WHERE f.run_id = m.run_id AND f.message_seq = m.message_seq AND f.result = 'failed') "
    "THEN 'failed' ELSE m.outcome END AS outcome "
    "FROM tasks t "
    "LEFT JOIN permission_decisions p ON p.permission_id = t.pending_permission_id "
    "LEFT JOIN decisions d ON t.pause_reason = 'uncertain' AND d.run_id = t.session_id "
    "  AND d.idx = (SELECT MAX(idx) FROM decisions "
    "    WHERE run_id = t.session_id AND message_seq = t.paused_message_seq) "
    # the last decision of the task's last message: its handler and outcome (V6-6).
    "LEFT JOIN decisions m ON m.run_id = t.session_id AND m.message_seq = t.last_message_seq "
    "  AND m.idx = (SELECT MAX(idx) FROM decisions "
    "    WHERE run_id = t.session_id AND message_seq = t.last_message_seq)"
)


def queued_ids(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Every ``queued`` task's id in queue order: ``created_at``, then ``task_id`` (one read)."""
    found = conn.execute(
        "SELECT task_id FROM tasks WHERE state = 'queued' ORDER BY created_at, task_id"
    ).fetchall()
    return tuple(str(row[0]) for row in found)


def orphan_candidates(conn: sqlite3.Connection) -> tuple[dict[str, str | None], ...]:
    """The tasks a process could have left behind: every ``running`` one and every
    ``needs_human`` one with a pending permission, with the process each recorded."""
    cursor = conn.execute(
        "SELECT task_id, session_id, state, pid, pid_started_at, pending_permission_id FROM tasks "
        "WHERE state = 'running' OR (state = 'needs_human' AND pending_permission_id IS NOT NULL) "
        "ORDER BY created_at, task_id"
    )
    names = [column[0] for column in cursor.description]
    return tuple(
        {
            name: None if value is None else str(value)
            for name, value in zip(names, found, strict=False)
        }
        for found in cursor.fetchall()
    )


def orphan(conn: sqlite3.Connection, task_id: str) -> bool:
    """Close a task whose process is gone: ``failed`` / ``orphaned`` and ``task.finished``.

    ``task.finished`` is the first and only event of its own new session (MUST rule 38), as a
    cancel at rest does. Returns whether this call closed it (a task that moved meanwhile is left
    alone).
    """
    session_id = ids.new_run_id()
    changed = _tick(
        conn,
        task_id,
        "'running', 'needs_human'",
        "state = 'failed', stop_reason = 'orphaned', finished_at = ?, "
        "session_id = COALESCE(session_id, ?), pause_reason = NULL, paused_at = NULL, "
        "pending_permission_id = NULL",
        (_now(), session_id),
    )
    if changed:
        EventBus(run_id=session_id, conn=conn).publish(
            FINISHED, {"task_id": task_id, "state": "failed", "stop_reason": "orphaned"}
        )
    return bool(changed)


def get(conn: sqlite3.Connection, task_id: str) -> dict[str, str | None] | None:
    """The task as stored, every column a string or ``None``; ``None`` if there is no such task."""
    cursor = conn.execute(f"{_SELECT} WHERE t.task_id = ?", (task_id,))
    found = cursor.fetchone()
    return None if found is None else _served(cursor, found)


def allows_dirty(conn: sqlite3.Connection, task_id: str) -> bool:
    """Whether ``task add --allow-dirty`` stored the task's leave to delegate into a dirty tree."""
    found = conn.execute("SELECT allow_dirty FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return found is not None and bool(found[0])


def _require(conn: sqlite3.Connection, task_id: str) -> dict[str, str | None]:
    found = get(conn, task_id)
    if found is None:
        raise SurfaceNotFound(f"no task {task_id!r}: the tasks ledger holds none.")
    return found


def payload(row: dict[str, str | None]) -> dict[str, str | None]:
    """The row as stored, for the verbs that act on a task; :func:`json_payload` is the read's
    shape, with the joined pause fields and ``unattended`` this drops."""
    return {name: value for name, value in row.items() if name not in _JOINED}


def _listing_cursor(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    session: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> sqlite3.Cursor:
    """The read of tasks newest first, optionally of one state or one session.

    ``state`` ``needs_human`` is the one listing served longest waiting first (``paused_at``
    ascending, G-59). A ``session`` that is a task's answers that task, one the events table holds
    that is not a task's answers ``()``, and one nothing holds is :class:`SurfaceNotFound`.
    """
    if state is not None and state not in STATES:
        raise ParameterError(f"a task state is one of {', '.join(STATES)}, not {state!r}.")
    _count("the limit", limit)
    if session is not None and not _held(conn, session):
        raise SurfaceNotFound(
            f"no session under {session!r}: the events table holds nothing under that id."
        )
    clauses, params = [], []
    if state is not None:
        clauses.append("t.state = ?")
        params.append(state)
    if session is not None:
        clauses.append("t.session_id = ?")
        params.append(session)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    order = (
        "t.paused_at, t.task_id" if state == "needs_human" else "t.created_at DESC, t.task_id DESC"
    )
    return conn.execute(f"{_LIST_SELECT} {where}ORDER BY {order} LIMIT ?", (*params, limit))


def listing(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    session: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[dict[str, str | None], ...]:
    """Tasks newest first, as :func:`_listing_cursor` reads them, one dict per task."""
    return _served_rows(_listing_cursor(conn, state=state, session=session, limit=limit))


def listing_wire(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    session: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, object]:
    """:func:`listing` then :func:`listing_payload`, without the row dicts in between: the gateway's
    read, which a thousand tasks must answer within 200 ms (FR-75, ticket 265)."""
    names, columns = _served_columns(
        _listing_cursor(conn, state=state, session=session, limit=limit)
    )
    return _wire(dict(zip(names, columns, strict=True)), len(columns[0]))


def _held(conn: sqlite3.Connection, session_id: str) -> bool:
    return events_read.held(conn, session_id) or (
        conn.execute("SELECT 1 FROM tasks WHERE session_id = ?", (session_id,)).fetchone()
        is not None
    )


#: The columns a read serves, in order (decision 86); ``allow_dirty``, the nanodollar twins, the
#: process identity and the checkpoint's sequence numbers stay in the ledger.
_SERVED: Final = (
    "task_id",
    "text",
    "cwd",
    "entry",
    "state",
    "stop_reason",
    "pause_reason",
    "paused_at",
    "pending_permission_id",
    "pause_agent",
    "pause_tool_kind",
    "pause_description",
    "pause_route",
    "pause_confidence",
    "pause_confidence_percent",
    "pause_floor",
    "pause_floor_percent",
    "pause_compared",
    "session_id",
    "handler",
    "outcome",
    "queued_via",
    "started_by",
    "schedule_id",
    "created_at",
    "started_at",
    "finished_at",
    "budget_iterations",
    "budget_seconds",
    "budget_dollars",
    "budget_source",
    "used_iterations",
    "used_seconds",
    "used_dollars",
    "unpriced_turns",
    "human_messages",
    "pauses",
    "unattended",
)

#: The list's read: :data:`_SELECT` with only the ledger columns a read serves in place of ``t.*``,
#: since the process identity, the nanodollar twins and the checkpoint's numbers are fetched and
#: dropped otherwise, a third of a thousand rows' values (ticket 265).
_LIST_SELECT: Final = _SELECT.replace(
    "t.*", ", ".join(f"t.{name}" for name in _SERVED if name not in _JOINED), 1
)


_NOT_WAITING: Final = "it is not waiting on an agent's request"
_NOT_UNCERTAIN: Final = "it is not paused on an uncertain route"
_NOT_STARTED: Final = "it has not started yet"

#: Why a served column is ``null``, said beside it as ``<column>_reason`` (decision 86).
_REASONS: Final = {
    "entry": "no entry was named: the router chooses",
    "stop_reason": "it has not stopped",
    "pause_reason": "it is not waiting on you",
    "paused_at": "it is not waiting on you",
    "pending_permission_id": _NOT_WAITING,
    "pause_agent": _NOT_WAITING,
    "pause_tool_kind": _NOT_WAITING,
    "pause_description": "it is not waiting on an agent's request, or the agent described nothing",
    "pause_route": _NOT_UNCERTAIN,
    "pause_confidence": _NOT_UNCERTAIN,
    "pause_confidence_percent": _NOT_UNCERTAIN,
    "pause_floor": _NOT_UNCERTAIN,
    "pause_floor_percent": _NOT_UNCERTAIN,
    "pause_compared": _NOT_UNCERTAIN,
    "session_id": "no message has run yet",
    "handler": "no message has run yet, or its last message named no handler",
    "outcome": "no message has run yet",
    "started_by": _NOT_STARTED,
    "schedule_id": "no schedule started it",
    "started_at": _NOT_STARTED,
    "finished_at": "it has not finished yet",
    "unattended": "not done yet",
}


#: ``json_payload``'s plan, worked out once: each served name with the reason it carries, if any.
_PLAN: Final = tuple((name, _REASONS.get(name)) for name in _SERVED)


def json_payload(row: Mapping[str, str | None]) -> dict[str, str | None]:
    """One task as the reads serve it: every column a string or ``null`` with its reason.

    ``unattended`` is ``"true"`` or ``"false"`` once the task is ``done`` and ``null`` with its
    reason before, from the same expression the total counts by.
    """
    get = row.get
    out: dict[str, str | None] = {}
    for name, reason in _PLAN:
        value = get(name)
        out[name] = value
        if reason is not None:
            out[f"{name}_reason"] = reason if value is None else None
    unattended = get("unattended")
    if unattended is not None:
        out["unattended"] = "true" if str(unattended) == "1" else "false"
    return out


def listing_payload(rows: tuple[dict[str, str | None], ...]) -> dict[str, object]:
    """The one wire shape of the tasks list: ``{"tasks": [...]}``, each row as
    :func:`json_payload`."""
    by_name = {name: tuple(map(dict.get, rows, repeat(name))) for name, _ in _PLAN}
    return _wire(by_name, len(rows))


def _when_null(reason: str) -> Callable[[str | None], str | None]:
    """``reason`` for a ``None``, ``None`` for the rest: a ``dict.get``, so ``map`` stays in C."""
    return {None: reason}.get  # type: ignore[return-value]


def _wire(by_name: Mapping[str, tuple[str | None, ...]], count: int) -> dict[str, object]:
    """``{"tasks": [...]}`` from the served columns, by column like :func:`_served_columns`, each
    row's keys ascending.

    A thousand rows of sixty keys is the read's longest Python loop, so each served column is one
    ``map`` and its reason one more over that column, then the rows are zipped back (ticket 265).
    A name the columns lack reads ``null``, as :func:`json_payload`'s ``get`` does.
    """
    if not count:
        return {"tasks": []}
    columns: dict[str, tuple[str | None, ...]] = {}
    for name, reason in _PLAN:
        values = by_name.get(name) or (None,) * count
        columns[name] = values
        if reason is not None:
            nulls = values.count(None)
            if nulls == 0 or nulls == count:  # most columns are all one or the other
                columns[f"{name}_reason"] = (reason if nulls else None,) * count
            else:
                columns[f"{name}_reason"] = tuple(map(_when_null(reason), values))
    # `1` is true, any other stored value false; a not-done task's `null` stays `null`.
    columns["unattended"] = tuple(map(_UNATTENDED_WORD.get, columns["unattended"], repeat("false")))
    keys = tuple(sorted(columns))  # ascending, so the gateway can skip the sort (ticket 265)
    ordered = zip(*map(columns.__getitem__, keys), strict=True)
    return {"tasks": list(map(dict, map(zip, repeat(keys), ordered)))}


_UNATTENDED_WORD: Final = {None: None, "1": "true"}


_TOTALS: Final = f"SELECT state, COUNT(*), SUM({UNATTENDED_SQL} = 1) FROM tasks GROUP BY state"


def totals(conn: sqlite3.Connection) -> dict[str, str]:
    """Every task counted all time, by state, with ``completed_unattended`` (decision 47's shape).

    Every count is a string (MUST rule 29) and ``counted_at`` says when the count was taken.
    """
    counts = dict.fromkeys(STATES, 0)
    unattended = 0
    for state, held, unattended_held in conn.execute(_TOTALS):
        counts[str(state)] = int(held)
        unattended += int(unattended_held or 0)
    return {
        "completed_unattended": str(unattended),
        **{name: str(counts[name]) for name in ("needs_human", "running", "queued", "done")},
        "failed": str(counts["failed"]),
        "cancelled": str(counts["cancelled"]),
        "counted_at": _now(),
    }


def totals_payload(found: Mapping[str, str]) -> dict[str, str]:
    """The one wire shape of the totals, printed by the CLI and served by the gateway."""
    return dict(found)


def cancel(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    deny: Callable[[str], None] | None = None,
) -> dict[str, str | None]:
    """Cancel a ``queued`` or ``needs_human`` task; the row as it now stands.

    A ``needs_human`` task's pending permission is denied through ``deny`` first (the parked
    process then ends its own message and publishes ``task.finished`` inside it). A task with
    nothing parked, a queued one or an at-rest uncertain pause, publishes ``task.finished`` here as
    the one event of its own session. A ``running`` task is refused, and so is a finished one.
    """
    task = _require(conn, task_id)
    state = str(task["state"])
    if state == "running":
        raise TaskStateError(task_id, state, "running: it stops at its budget or when it finishes")
    if state in TERMINAL_STATES:
        raise TaskStateError(task_id, state, f"{state}: it has already finished")
    permission_id = task["pending_permission_id"]
    parked = state == "needs_human" and permission_id is not None
    if parked and deny is not None:
        deny(str(permission_id))
    session_id = None if parked else ids.new_run_id()
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE tasks SET state = 'cancelled', stop_reason = 'cancelled', finished_at = ?, "
            "session_id = COALESCE(session_id, ?), pause_reason = NULL, paused_at = NULL, "
            "pending_permission_id = NULL WHERE task_id = ? AND state = ?",
            (_now(), session_id, task_id, state),
        ).rowcount
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"could not cancel the task: {exc}") from exc
    if changed == 0:
        now = _require(conn, task_id)
        raise TaskStateError(task_id, str(now["state"]), f"{now['state']}: it moved under you")
    if session_id is not None:
        EventBus(run_id=session_id, conn=conn).publish(
            FINISHED, {"task_id": task_id, "state": "cancelled", "stop_reason": "cancelled"}
        )
    return payload(_require(conn, task_id))


def for_session(conn: sqlite3.Connection, session_id: str) -> dict[str, str | None] | None:
    """The task whose session is ``session_id``, or ``None`` when the session is not a task's."""
    cursor = conn.execute(f"{_SELECT} WHERE t.session_id = ?", (session_id,))
    found = cursor.fetchone()
    return None if found is None else _served(cursor, found)


def for_session_text(session_id: str) -> dict[str, str | None] | None:
    """What the control door calls: open the one database, read the task, close it."""
    conn = db.connect()
    try:
        return for_session(conn, session_id)
    finally:
        conn.close()


def state_text(row: Mapping[str, str | None]) -> str:
    """The router state's ``The task:`` section: the text, then the used figures beside the budget
    exactly as the row holds them (decision 58)."""
    return (
        f"{row['text']}\n"
        f"Used so far: {row['used_iterations']} of {row['budget_iterations']} steps, "
        f"{row['used_seconds']} of {row['budget_seconds']} seconds, "
        f"${row['used_dollars']} of ${row['budget_dollars']}."
    )


def _tick(
    conn: sqlite3.Connection, task_id: str, states: str, change: str, params: tuple[object, ...]
) -> int:
    """One guarded ``UPDATE`` inside its own transaction; the rows it changed."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            f"UPDATE tasks SET {change} WHERE task_id = ? AND state IN ({states})",
            (*params, task_id),
        ).rowcount
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"could not update the task: {exc}") from exc
    return changed


def begin_message(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: str,
    message_seq: int,
    author: str,
    first: bool = False,
    started_by: str = "hand",
    schedule_id: str | None = None,
    publish: Publish | None = None,
) -> None:
    """A message of the task starts: who wrote it, and the task is ``running`` (decision 77).

    ``author`` is written here, by the composition, as the message starts and never inferred
    afterwards (MUST rule 40): a ``task`` message gets its ``task_messages`` row (kind ``first`` or
    ``continuation``), an ``operator`` message gets none and ``human_messages`` goes up by one. The
    first message also sets ``started_at``, ``started_by`` and the process identity, and publishes
    ``task.started``; every message holds the ``session_id``. ``started_by`` is ``schedule`` (with
    that schedule's id) when ``task run --due`` began it, ``hand`` otherwise.
    """
    if started_by not in ("hand", "schedule"):
        raise StorageError(f"a task is started by hand or by a schedule, not {started_by!r}")
    if author not in ("task", "operator"):
        raise StorageError(f"a message's author is task or operator, not {author!r}")
    task = _require(conn, task_id)
    me = proc.identity(os.getpid())
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE tasks SET state = 'running', session_id = COALESCE(session_id, ?), "
            "started_at = COALESCE(started_at, ?), started_by = COALESCE(started_by, ?), "
            "schedule_id = COALESCE(schedule_id, ?), pid = ?, pid_started_at = ?, "
            "last_message_seq = ?, pause_reason = NULL, "
            "paused_at = NULL, paused_message_seq = NULL, pending_permission_id = NULL, "
            "human_messages = human_messages + ? "
            "WHERE task_id = ? AND state IN ('queued', 'running', 'needs_human')",
            (
                run_id,
                now,
                started_by,
                schedule_id,
                os.getpid(),
                None if me is None else me.started_at,
                message_seq,
                1 if author == "operator" else 0,
                task_id,
            ),
        ).rowcount
        if changed and author == "task":
            conn.execute(
                "INSERT INTO task_messages (run_id, message_seq, task_id, kind) "
                "VALUES (?, ?, ?, ?)",
                (run_id, message_seq, task_id, "first" if first else "continuation"),
            )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"could not start the task's message: {exc}") from exc
    if changed == 0:
        now_row = _require(conn, task_id)
        raise TaskStateError(
            task_id, str(now_row["state"]), f"{now_row['state']}: it cannot take a message"
        )
    if first and publish is not None:
        publish(
            STARTED,
            {
                "task_id": task_id,
                "started_by": started_by,
                "schedule_id": schedule_id if schedule_id is not None else task["schedule_id"],
            },
        )


def reopen_dirty(conn: sqlite3.Connection, task_id: str) -> None:
    """Reopen a task failed ``dirty_tree`` as ``running``, for ``task run --allow-dirty``.

    The one terminal state decision 76 lets back in (ticket 269): the refusal came before any
    work, so the task keeps its session and its figures, which the next checkpoint adds to. One
    guarded transaction; a task that moved meanwhile is refused.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE tasks SET state = 'running', stop_reason = NULL, finished_at = NULL "
            "WHERE task_id = ? AND state = 'failed' AND stop_reason = ?",
            (task_id, DIRTY_TREE_STOP),
        ).rowcount
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"could not reopen the task: {exc}") from exc
    if changed == 0:
        now = _require(conn, task_id)
        raise TaskStateError(task_id, str(now["state"]), f"{now['state']}: it moved under you")


def paused_payload(
    task_id: str,
    pause_reason: str,
    *,
    permission_id: str | None = None,
    uncertain: Mapping[str, str | None] | None = None,
) -> dict[str, object]:
    """``task.paused``'s payload: the reason, and either the request waited on or the router's
    route, confidence and floor with their percents (decision 59's stored strings)."""
    said = {} if uncertain is None else uncertain
    return {
        "task_id": task_id,
        "pause_reason": pause_reason,
        "permission_id": permission_id,
        "route": said.get("route"),
        "confidence": said.get("confidence"),
        "confidence_percent": said.get("confidence_percent"),
        "floor": said.get("floor"),
        "floor_percent": said.get("floor_percent"),
    }


def pause_for_permission(
    conn: sqlite3.Connection, task_id: str, permission_id: str, *, publish: Publish | None = None
) -> None:
    """The agent asked the operator for something: ``needs_human`` / ``permission`` (decision 75).

    ``pending_permission_id`` names the request and ``pauses`` goes up by one. The caller releases
    the run lease.
    """
    changed = _tick(
        conn,
        task_id,
        "'running'",
        "state = 'needs_human', pause_reason = 'permission', paused_at = ?, "
        "pending_permission_id = ?, paused_message_seq = last_message_seq, pauses = pauses + 1",
        (_now(), permission_id),
    )
    if changed and publish is not None:
        publish(PAUSED, paused_payload(task_id, "permission", permission_id=permission_id))


def resume_after_answer(conn: sqlite3.Connection, task_id: str) -> None:
    """The operator answered and the run holds its lease again: ``running`` (decision 75).

    A task cancelled while it waited is left as it is.
    """
    _tick(
        conn,
        task_id,
        "'needs_human'",
        "state = 'running', pause_reason = NULL, paused_at = NULL, pending_permission_id = NULL, "
        "paused_message_seq = NULL",
        (),
    )


_COUNT_TURNS: Final = "SELECT COUNT(*) FROM turns WHERE run_id = ? AND message_seq = ?"
_COUNT_HOPS: Final = (
    "SELECT COUNT(*) FROM decisions WHERE run_id = ? AND message_seq = ? "
    "AND handler = 'agent' AND outcome = 'routed'"
)
_PENDING_SPANS: Final = (
    "SELECT requested_at, answered_at FROM permission_decisions "
    "WHERE session_id = ? AND requested_at >= ? AND ceiling_at IS NOT NULL"
)
_CEILING_DENIALS: Final = (
    "SELECT COUNT(*) FROM permission_decisions "
    "WHERE session_id = ? AND requested_at >= ? AND rule = 'ceiling'"
)


def _seconds(conn: sqlite3.Connection, run_id: str, started_at: str) -> Decimal:
    """The message's span less the time it spent waiting on a person, never below zero."""
    now = clock.now()
    span = Decimal(str((now - clock.parse(started_at)).total_seconds()))
    waited = Decimal(0)
    for requested, answered in conn.execute(_PENDING_SPANS, (run_id, started_at)).fetchall():
        end = now if answered is None else min(now, clock.parse(str(answered)))
        waited += Decimal(str((end - clock.parse(str(requested))).total_seconds()))
    return max(Decimal(0), span - waited)


def checkpoint(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: str,
    message_seq: int,
    started_at: str,
    nanos: int,
    unpriced: int,
    ended: str,
    uncertain: Mapping[str, str | None] | None = None,
    publish: Publish | None = None,
) -> dict[str, str | None]:
    """A message ended: keep its figures and decide what the task does next (decision 77).

    In one transaction, before ``run.finished``: the used columns take this message's own rows
    (iterations are loop turns plus routed agent hops; seconds the message's span less pending
    time; dollars the ``nanos`` the caller priced from decision 64's arithmetic, ``unpriced`` turns
    counted), then the state follows decision 77's order: a ceiling denial on this message is
    ``failed`` / ``ceiling``; ``dirty_tree``; ``raised``; ``task_done`` is ``done`` / ``finished``;
    an uncertain stop is ``needs_human`` / ``uncertain``; a used figure at or over its budget is
    ``failed`` / ``budget``; otherwise the task stays ``running`` and the caller sends the next
    continuation. A task cancelled meanwhile stays ``cancelled``. ``task.paused`` and
    ``task.finished`` publish after the commit, still inside the message.
    """
    if ended not in ENDS:
        raise StorageError(f"a message ends one of {ENDS}, not {ended!r}")
    iterations = int(conn.execute(_COUNT_TURNS, (run_id, message_seq)).fetchone()[0]) + int(
        conn.execute(_COUNT_HOPS, (run_id, message_seq)).fetchone()[0]
    )
    seconds = _seconds(conn, run_id, started_at)
    ceiling = int(conn.execute(_CEILING_DENIALS, (run_id, started_at)).fetchone()[0]) > 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = _require(conn, task_id)
        used_iterations = int(str(task["used_iterations"])) + iterations
        used_seconds = (Decimal(str(task["used_seconds"])) + seconds).quantize(
            _TENTH, ROUND_HALF_UP
        )
        used_nanos = int(str(task["used_nanos"])) + nanos
        used_dollars = format(
            (Decimal(used_nanos) / _NANOS_PER_DOLLAR).quantize(_MILLIONTH, ROUND_HALF_UP), "f"
        )
        state, stop_reason, pause = str(task["state"]), None, None
        if state == "cancelled":
            pass
        elif ceiling:
            state, stop_reason = "failed", "ceiling"
        elif ended == "dirty_tree":
            state, stop_reason = "failed", "dirty_tree"
        elif ended == "raised":
            state, stop_reason = "failed", "raised"
        elif ended == "task_done":
            state, stop_reason = "done", "finished"
        elif ended == "uncertain":
            state, pause = "needs_human", "uncertain"
        elif (
            used_iterations >= int(str(task["budget_iterations"]))
            or used_seconds >= Decimal(str(task["budget_seconds"]))
            or used_nanos >= int(str(task["budget_nanos"]))
        ):
            state, stop_reason = "failed", "budget"
        else:
            state = "running"
        finishing = state in ("done", "failed")
        now = _now()
        conn.execute(
            "UPDATE tasks SET used_iterations = ?, used_seconds = ?, used_dollars = ?, "
            "used_nanos = ?, unpriced_turns = unpriced_turns + ?, last_message_seq = ?, "
            "state = ?, stop_reason = COALESCE(?, stop_reason), "
            "finished_at = CASE WHEN ? THEN ? ELSE finished_at END, "
            "pause_reason = ?, paused_at = ?, paused_message_seq = ?, pauses = pauses + ?, "
            "pending_permission_id = NULL WHERE task_id = ?",
            (
                used_iterations,
                format(used_seconds, "f"),
                used_dollars,
                used_nanos,
                unpriced,
                message_seq,
                state,
                stop_reason,
                1 if finishing else 0,
                now,
                pause,
                None if pause is None else now,
                None if pause is None else message_seq,
                0 if pause is None else 1,
                task_id,
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"could not keep the task's checkpoint: {exc}") from exc
    if publish is not None:
        if pause is not None:
            publish(PAUSED, paused_payload(task_id, pause, uncertain=uncertain))
        elif finishing or state == "cancelled":
            publish(
                FINISHED,
                {
                    "task_id": task_id,
                    "state": state,
                    "stop_reason": "cancelled" if state == "cancelled" else stop_reason,
                },
            )
    return payload(_require(conn, task_id))
