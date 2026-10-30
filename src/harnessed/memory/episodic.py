"""Episodic memory: what happened, per run (FR-10).

One episode is one row in ``mem_episodic``. ``kind`` separates a loop transcript from a
delegation transcript, and ``for_run`` replays a run in insertion order, which is the order the
loop wrote it in.

``for_run`` answers "the episodes under this id" and an empty tuple is a complete answer to
that. It cannot say whether the id names a session at all, because ``mem_episodic`` is not
where a session id is minted: ``events`` is. ``for_session`` closes that gap for the surface
that needs it (ticket 076): it asks ``events/read.py::held`` first and answers ``None`` for an
id the events table does not hold, so "never existed" and "recorded nothing" are two values
rather than one. It is one callable so a gateway handler can render it in one call (FR-40),
and each query stays in the package that owns its table (MUST rule 14).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from harnessed import clock, ids
from harnessed.events import read as events_read
from harnessed.memory import db, redact

__all__ = [
    "Episode",
    "count_outside",
    "count_unconsolidated",
    "for_run",
    "for_session",
    "get",
    "put",
    "unconsolidated",
]

_COLUMNS: Final = "episode_id, run_id, kind, text, created_at"

_INSERT: Final = f"INSERT INTO mem_episodic ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)"
_SELECT_ONE: Final = f"SELECT {_COLUMNS} FROM mem_episodic WHERE episode_id = ?"
_SELECT_RUN: Final = f"SELECT {_COLUMNS} FROM mem_episodic WHERE run_id = ? ORDER BY rowid"
_UNCONSOLIDATED: Final = (
    f"SELECT {_COLUMNS} FROM mem_episodic "
    "WHERE episode_id NOT IN (SELECT episode_id FROM mem_consolidated) ORDER BY rowid"
)
_COUNT_UNCONSOLIDATED: Final = (
    "SELECT COUNT(*) FROM mem_episodic "
    "WHERE episode_id NOT IN (SELECT episode_id FROM mem_consolidated)"
)


@dataclass(frozen=True, slots=True)
class Episode:
    """One remembered episode. ``created_at`` is ISO-8601 UTC, from :mod:`harnessed.clock`."""

    episode_id: str
    run_id: str
    kind: str
    text: str
    created_at: str


def count_outside(conn: sqlite3.Connection, prefix: str) -> int:
    """How many episodes carry a ``run_id`` that does not start with ``prefix``."""
    row = conn.execute(
        "SELECT COUNT(*) FROM mem_episodic WHERE substr(run_id, 1, ?) != ?", (len(prefix), prefix)
    ).fetchone()
    return int(row[0])


def put(conn: sqlite3.Connection, run_id: str, kind: str, text: str) -> Episode:
    """Write one episode, redacted first (FR-13).

    Every caller-supplied string is scrubbed, ``run_id`` and ``kind`` included. Neither can
    plausibly carry a credential, which is exactly the argument rule 7 forbids making.
    """
    episode = Episode(
        episode_id=ids.new_id("epi"),
        run_id=redact.scrub(run_id),
        kind=redact.scrub(kind),
        text=redact.scrub(text),
        created_at=clock.isoformat(clock.now()),
    )
    db.write(
        conn,
        (
            (
                _INSERT,
                (
                    episode.episode_id,
                    episode.run_id,
                    episode.kind,
                    episode.text,
                    episode.created_at,
                ),
            ),
        ),
    )
    return episode


def get(conn: sqlite3.Connection, episode_id: str) -> Episode | None:
    """One episode by id, or ``None``. A miss is an answer, not an error."""
    row = conn.execute(_SELECT_ONE, (episode_id,)).fetchone()
    return None if row is None else Episode(*row)


def for_run(conn: sqlite3.Connection, run_id: str) -> tuple[Episode, ...]:
    """Every episode of one run, in the order it was written."""
    return tuple(Episode(*row) for row in conn.execute(_SELECT_RUN, (run_id,)))


def for_session(conn: sqlite3.Connection, session_id: str) -> tuple[Episode, ...] | None:
    """Every episode of one session, or ``None`` when no session was ever recorded under the id.

    ``None`` means the events table holds nothing under ``session_id``: there is no such
    session, and a surface built on this answers 404. An empty tuple means the session exists
    and wrote no episode, which is a legitimate empty collection. The rows, when there are
    rows, are exactly :func:`for_run`'s.
    """
    if not events_read.held(conn, session_id):
        return None
    return for_run(conn, session_id)


def unconsolidated(conn: sqlite3.Connection, limit: int) -> tuple[Episode, ...]:
    """The oldest ``limit`` episodes no batch has consumed (no ``mem_consolidated`` row), oldest
    first (ticket 247, FR-148)."""
    rows = conn.execute(f"{_UNCONSOLIDATED} LIMIT ?", (limit,))
    return tuple(Episode(*row) for row in rows)


def count_unconsolidated(conn: sqlite3.Connection) -> int:
    """How many episodes no batch has consumed."""
    return int(conn.execute(_COUNT_UNCONSOLIDATED).fetchone()[0])
