"""The run lease: at most one message is being worked on at a time, across processes (decision 78).

Every door that starts a message (``harnessed run``, the control app) takes the lease inside
``run_cmd``'s composition. The lease is one row of ``run_lease`` naming the session that holds it
and the holder's process identity (``proc.identity``: pid plus start time), so a holder that died
without releasing is not a live holder and a reused pid is not mistaken for one. ``eval run`` and
``improve run`` never take it.

A run that is waiting on the operator releases the lease (decision 75) and takes it back when the
answer arrives; :func:`acquire` is that one call both times.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from harnessed import clock, proc
from harnessed.errors import RunInFlight, StorageError

__all__ = ["Lease", "acquire", "holder", "refusal", "release"]


@dataclass(frozen=True, slots=True)
class Lease:
    """The row as stored."""

    session_id: str
    pid: int
    started_at: str | None
    acquired_at: str

    @property
    def started_at_reason(self) -> str | None:
        """Why ``started_at`` is null, or ``None`` when the row holds one."""
        return None if self.started_at is not None else proc.NO_START_TIME


def holder(conn: sqlite3.Connection) -> Lease | None:
    """The live holder, or ``None`` when the lease is free or its holder is gone."""
    found = conn.execute(
        "SELECT session_id, pid, started_at, acquired_at FROM run_lease WHERE slot = 1"
    ).fetchone()
    if found is None:
        return None
    lease = Lease(
        str(found[0]), int(found[1]), None if found[2] is None else str(found[2]), str(found[3])
    )
    return lease if proc.holds(lease.pid, lease.started_at) else None


def refusal(conn: sqlite3.Connection, session_id: str) -> RunInFlight:
    """The refusal for a run in flight under ``session_id``: it names the task when the session is
    a task's, so the operator reads what is running and how to stop it (decision 103)."""
    row = conn.execute(
        "SELECT task_id, text FROM tasks WHERE session_id = ?", (session_id,)
    ).fetchone()
    return RunInFlight(session_id) if row is None else RunInFlight(session_id, row[0], row[1])


def acquire(conn: sqlite3.Connection, session_id: str) -> None:
    """Take the lease for ``session_id``, or raise :class:`~harnessed.errors.RunInFlight`.

    A live holder that is a different session refuses, naming its session. The same session, or a
    holder that died, is replaced. The check and the write are one ``BEGIN IMMEDIATE``.
    """
    me = proc.identity(os.getpid())
    started = None if me is None else me.started_at
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = holder(conn)
        if current is not None and current.session_id != session_id:
            conn.execute("ROLLBACK")
            raise refusal(conn, current.session_id)
        conn.execute("DELETE FROM run_lease")
        conn.execute(
            "INSERT INTO run_lease (slot, session_id, pid, started_at, acquired_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (session_id, os.getpid(), started, clock.isoformat(clock.now())),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"could not take the run lease: {exc}") from exc


def release(conn: sqlite3.Connection, session_id: str) -> None:
    """Give the lease up if ``session_id`` holds it. Releasing one never held is a no-op."""
    try:
        conn.execute("DELETE FROM run_lease WHERE slot = 1 AND session_id = ?", (session_id,))
    except sqlite3.Error as exc:
        raise StorageError(f"could not release the run lease: {exc}") from exc
