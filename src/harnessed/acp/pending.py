"""The pending decision as its own table (FR-130, decision 74): record it, answer it once, read it.

A permission request is one row of ``permission_decisions`` (migration 019). The row is written
when the request arrives, with ``ceiling_at`` when a person is being asked, and is **answered
exactly once**: :func:`answer` is a single compare-and-set (``answer IS NULL``) inside ``BEGIN
IMMEDIATE``, so the console, the CLI and the waiting thread's own ceiling can all race for the same
row and exactly one wins. A loser gets :class:`~harnessed.errors.PermissionAnswered` carrying what
the winner recorded.

The state a reader sees is computed here and never stored: ``pending`` (no answer), ``approved`` or
``denied`` (a person answered), ``expired`` (the ceiling answered), ``allowed`` or ``refused`` (the
configuration answered at once). Every column is served as a string or ``null`` with its reason, so
the dashboard never guesses why a cell is empty (MUST rule 14).

Nothing here publishes: the calling thread of the delegation does, from ``acp/delegate.py``.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from harnessed import clock, ids, proc, serialize
from harnessed.acp.permissions import Prompter
from harnessed.errors import ParameterError, PermissionAnswered, StorageError, SurfaceNotFound
from harnessed.events import read as events_read
from harnessed.memory import db, redact

__all__ = [
    "ANSWERED_VIA",
    "ANSWERS",
    "DETAIL_MAX",
    "PARKED_MAX",
    "RULES",
    "STATES",
    "Door",
    "Row",
    "answer",
    "get",
    "listing",
    "listing_payload",
    "operator_answer",
    "record",
    "state_of",
]

#: How many runs the control app may hold parked on a pending request beside the one active run.
PARKED_MAX: Final = 3

#: How much of the agent's raw input a row keeps (decision 100).
DETAIL_MAX: Final = 2000

ANSWERS: Final = ("approve", "deny")
RULES: Final = ("operator", "ceiling", "allowlist", "deny_list")
ANSWERED_VIA: Final = ("console", "cli")
STATES: Final = ("pending", "approved", "denied", "expired", "allowed", "refused")


@dataclass(frozen=True, slots=True)
class Door:
    """What a caller hands a delegation so a request can reach a person (decisions 74, 75, 78).

    ``answer_door`` says someone can answer. ``on_pending`` runs on the calling thread when a
    request opens (the run lease is released there); ``on_answered`` runs once it is answered and
    returns ``True`` when the run holds its lease again, ``False`` to be asked again next tick.
    ``stopping`` is ``True`` once the process is going down, which denies every open request.
    """

    answer_door: bool = False
    ceiling_s: float = 600.0
    on_pending: Callable[[str], None] | None = None
    on_answered: Callable[[str], bool] | None = None
    stopping: Callable[[], bool] | None = None
    #: With no answer door, v5's path: a terminal that asks (rule ``operator``, via ``cli``).
    interactive: bool = False
    prompter: Prompter | None = None


@dataclass(frozen=True, slots=True)
class Row:
    """One ``permission_decisions`` row, as stored."""

    permission_id: str
    session_id: str
    agent: str
    tool_kind: str
    description: str | None
    requested_at: str
    ceiling_at: str | None
    answer: str | None
    rule: str | None
    answered_via: str | None
    answered_at: str | None
    message_seq: str | None = None
    hop: str | None = None
    #: What the agent sent and who waits (ticket 288, decision 100); all null before migration 026.
    task_hash: str | None = None
    detail: str | None = None
    warning: str | None = None
    waiter_pid: str | None = None
    waiter_started_at: str | None = None
    #: Read, never stored: the task whose ``session_id`` is this row's (listing only).
    task_id: str | None = None

    @property
    def state(self) -> str:
        return state_of(self)


_COLUMNS: Final = (
    "permission_id, session_id, agent, tool_kind, description, requested_at, ceiling_at, "
    "answer, rule, answered_via, answered_at, message_seq, hop, task_hash, detail, warning, "
    "waiter_pid, waiter_started_at"
)


def state_of(row: Row) -> str:
    """The word for where ``row`` stands, computed from its columns and nothing else."""
    if row.answer is None:
        return "pending"
    if row.rule == "ceiling":
        return "expired"
    if row.rule == "operator":
        return "approved" if row.answer == "approve" else "denied"
    return "allowed" if row.answer == "approve" else "refused"


def _row(values: tuple[object, ...]) -> Row:
    text = [None if value is None else str(value) for value in values]
    return Row(*text)  # type: ignore[arg-type]


def record(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    agent: str,
    tool_kind: str,
    description: str | None,
    ceiling_at: str | None = None,
    answer: str | None = None,
    rule: str | None = None,
    answered_via: str | None = None,
    message_seq: int | None = None,
    hop: int | None = None,
    task_hash: str | None = None,
    raw_input: object = None,
    warning: str | None = None,
    waiter: proc.Identity | None = None,
) -> Row:
    """Write one request. Open when ``answer`` is ``None``; otherwise decided on the spot.

    ``raw_input`` is the agent's tool-call input, stored as canonical JSON, scrubbed and cut at
    :data:`DETAIL_MAX`. The waiter is this process unless a recorder plants another.
    """
    waiter = waiter or proc.identity(os.getpid())
    permission_id = ids.new_id("perm")
    now = clock.isoformat(clock.now())
    stored = (
        permission_id,
        session_id,
        agent,
        redact.scrub(tool_kind),
        None if description is None else redact.scrub(description),
        now,
        ceiling_at,
        answer,
        rule,
        answered_via,
        None if answer is None else now,
        message_seq,
        hop,
        task_hash,
        _detail(raw_input),
        None if not warning else redact.scrub(warning),
        None if waiter is None else waiter.pid,
        None if waiter is None else waiter.started_at,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"INSERT INTO permission_decisions ({_COLUMNS}) VALUES ({','.join('?' * len(stored))})",
            stored,
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"could not record the permission request: {exc}") from exc
    return _row(stored)


def _detail(raw_input: object) -> str | None:
    if raw_input is None:
        return None
    return redact.scrub(serialize.canonical(raw_input))[:DETAIL_MAX]


def get(conn: sqlite3.Connection, permission_id: str) -> Row | None:
    """The row under ``permission_id``, or ``None``."""
    found = conn.execute(
        f"SELECT {_COLUMNS} FROM permission_decisions WHERE permission_id = ?", (permission_id,)
    ).fetchone()
    return None if found is None else _row(tuple(found))


def answer(
    conn: sqlite3.Connection,
    permission_id: str,
    verdict: str,
    *,
    rule: str,
    via: str | None,
) -> Row:
    """Answer an open request once; the row as it now stands.

    Raises :class:`~harnessed.errors.SurfaceNotFound` for an unknown id and
    :class:`~harnessed.errors.PermissionAnswered` when the row was already answered or expired.
    """
    if verdict not in ANSWERS:
        raise StorageError(f"a permission answer is one of {ANSWERS}, not {verdict!r}")
    now = clock.isoformat(clock.now())
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE permission_decisions SET answer = ?, rule = ?, answered_via = ?, "
            "answered_at = ? WHERE permission_id = ? AND answer IS NULL",
            (verdict, rule, via, now, permission_id),
        ).rowcount
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"could not answer the permission request: {exc}") from exc
    stored = get(conn, permission_id)
    if stored is None:
        raise SurfaceNotFound(f"no permission request {permission_id!r}: the ledgers hold none.")
    if changed == 0:
        assert stored.answer is not None and stored.rule is not None
        assert stored.answered_at is not None
        raise PermissionAnswered(
            stored.answer, stored.rule, stored.answered_via, stored.answered_at
        )
    return stored


def expire(conn: sqlite3.Connection, permission_id: str) -> Row | None:
    """Expire an open request whose waiter died (decision 75): denied by the ceiling rule.

    A request someone answered first is left as it is and reads back as ``None``.
    """
    try:
        return answer(conn, permission_id, "deny", rule="ceiling", via=None)
    except PermissionAnswered:
        return None


def operator_answer(permission_id: str, verdict: str, *, via: str) -> Row:
    """What both doors call: open the one database, answer as the operator, close it again.

    ``harnessed permissions answer`` (``via`` ``cli``) and ``POST /control/v1/permissions/{id}``
    (``via`` ``console``) are this one call, so they cannot answer one request two ways.
    """
    conn = db.connect()
    try:
        return answer(conn, permission_id, verdict, rule="operator", via=via)
    finally:
        conn.close()


def listing(
    conn: sqlite3.Connection, *, state: str | None = None, session: str | None = None
) -> tuple[Row, ...]:
    """Decisions in ``requested_at`` order: one session's in every state, or every pending one.

    A session no ledger holds is :class:`~harnessed.errors.SurfaceNotFound`; one that never asked
    for anything reads back as ``()``. Asking for neither a state nor a session, or for a state
    that is not one of :data:`STATES`, is :class:`~harnessed.errors.ParameterError`.
    """
    if state is None and session is None:
        raise ParameterError("give a state (?state=pending) or a session (?session=<id>).")
    if state is not None and state not in STATES:
        raise ParameterError(f"state {state!r} is not one of {', '.join(STATES)}.")
    if session is not None and not events_read.held(conn, session):
        held = conn.execute(
            "SELECT 1 FROM permission_decisions WHERE session_id = ? LIMIT 1", (session,)
        ).fetchone()
        if held is None:
            raise SurfaceNotFound(
                f"no session under {session!r}: the events table holds nothing under that id."
            )
    rows = [
        _row(tuple(found))
        for found in conn.execute(
            f"SELECT {_COLUMNS}, (SELECT task_id FROM tasks WHERE session_id = p.session_id) "
            "FROM permission_decisions p "
            "WHERE (? IS NULL OR session_id = ?) ORDER BY requested_at, permission_id",
            (session, session),
        )
    ]
    if state is not None:
        rows = [row for row in rows if row.state == state]
    return tuple(rows)


_UNANSWERED: Final = "nobody has answered yet"

_REASONS: Final = {
    "description": "the agent described nothing",
    "ceiling_at": "the configuration answered at once, so no person was asked and no ceiling set",
    "answer": "nobody has answered yet",
    "rule": "nobody has answered yet",
    "answered_via": "no person answered: the configuration, the ceiling or a shutdown did",
    "answered_at": "nobody has answered yet",
}

_OLD: Final = "recorded before Harnessed kept this"
_KEPT_REASONS: Final = {
    "task_hash": _OLD,
    "detail": "the agent sent no details",
    "warning": "no warning applied",
    "waiter_pid": _OLD,
    "waiter_started_at": _OLD,
}

_EXCHANGE: Final = "asked outside a routed message, or recorded before the exchange was kept"
_EXCHANGE_REASONS: Final = {
    "message_seq": _EXCHANGE,
    "hop": _EXCHANGE,
    "task_id": "the session is not a task's",
}


def listing_payload(rows: tuple[Row, ...]) -> dict[str, object]:
    """The one wire shape: ``{"permissions": [...]}``, each column a string or null, with why."""
    served = []
    for row in rows:
        item: dict[str, object] = {"state": row.state}
        for name in ("permission_id", "session_id"):
            item[name] = getattr(row, name)
        for name, reason in _EXCHANGE_REASONS.items():
            value = getattr(row, name)
            item[name] = value
            item[f"{name}_reason"] = reason if value is None else None
        for name in ("agent", "tool_kind", "requested_at"):
            item[name] = getattr(row, name)
        for name, reason in _REASONS.items():
            value = getattr(row, name)
            item[name] = value
            why = _UNANSWERED if row.answer is None and name != "ceiling_at" else reason
            item[f"{name}_reason"] = why if value is None else None
        for name, reason in _KEPT_REASONS.items():
            value = getattr(row, name)
            item[name] = value
            item[f"{name}_reason"] = reason if value is None else None
        served.append(item)
    return {"permissions": served}
