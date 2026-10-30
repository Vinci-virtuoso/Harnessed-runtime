"""Procedural memory: every procedure version ever written (FR-10, FR-20).

A procedure is content-addressed, so the same text is always the same ``proc_id`` and writing
it twice is a no-op rather than a second row. Nothing here deletes: a retired version stays
forever so a promotion can be rolled back and diffed (``state-and-idempotency.md``). The active
set lives in ``active_procedures`` and is the promoter's to write.

**The two tables are not symmetrical, and that asymmetry is the point.** :func:`activate` clears
``active_procedures`` and writes one row; ``procedures`` is append-only and no statement in this
module removes a row from it. So after a promotion the retired version is still there, still
addressable by its ``proc_id``, which is what makes a rollback and a diff possible at all.

:func:`record_promotion` is the store side of FR-20's decision. It takes the numbers the promoter
already computed rather than any part of the comparison - the arithmetic belongs to
``evals/scoring.py`` and reaches here as a result - and it writes the decision row, the procedure
and the active-set swap in **one** transaction, so a crash can never leave a promotion recorded
against an active set that never moved.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from harnessed import clock, ids
from harnessed.errors import StorageError
from harnessed.memory import db, redact

__all__ = [
    "Procedure",
    "Promotion",
    "active",
    "activate",
    "all",
    "get",
    "proc_id_for",
    "promotion",
    "promotions",
    "put",
    "record_promotion",
]

_COLUMNS: Final = "proc_id, text, source, created_at"

# The conflict clause is what makes a repeated write a no-op: same text, same id, first write
# stands. It is not an upsert - a procedure is never overwritten.
_INSERT: Final = (
    f"INSERT INTO procedures ({_COLUMNS}) VALUES (?, ?, ?, ?) ON CONFLICT (proc_id) DO NOTHING"
)
_SELECT_ONE: Final = f"SELECT {_COLUMNS} FROM procedures WHERE proc_id = ?"
_SELECT_ALL: Final = f"SELECT {_COLUMNS} FROM procedures ORDER BY rowid"
_SELECT_ACTIVE: Final = (
    "SELECT p.proc_id, p.text, p.source, p.created_at "
    "FROM active_procedures a JOIN procedures p ON p.proc_id = a.proc_id "
    "ORDER BY a.rowid"
)

_CLEAR_ACTIVE: Final = "DELETE FROM active_procedures"
_ACTIVATE: Final = "INSERT INTO active_procedures (proc_id, activated_at, cand_id) VALUES (?, ?, ?)"

_PROMOTION_COLUMNS: Final = (
    "cand_id, decided_at, promoted, score_before, score_after, passes_before, passes_after, "
    "regressions, soft_drops, reason"
)
_RECORD_PROMOTION: Final = (
    f"INSERT INTO promotions ({_PROMOTION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
# Oldest first, so "the recorded outcome" is the first decision this candidate ever got. The
# primary key is (cand_id, decided_at), which permits a second row: idempotency is the promoter
# refusing to write one, never the schema forbidding it.
_SELECT_PROMOTION: Final = (
    f"SELECT {_PROMOTION_COLUMNS} FROM promotions WHERE cand_id = ? ORDER BY decided_at, rowid "
    f"LIMIT 1"
)
# The history the gateway lists (FR-41): every decision across every candidate, in the order they
# were decided. Same tie-break as the single lookup, so the two readers agree on what "first"
# means for a candidate decided twice.
_SELECT_PROMOTIONS: Final = (
    f"SELECT {_PROMOTION_COLUMNS} FROM promotions ORDER BY decided_at, rowid LIMIT ?"
)


@dataclass(frozen=True, slots=True)
class Procedure:
    """One procedure version. ``created_at`` is ISO-8601 UTC, from :mod:`harnessed.clock`."""

    proc_id: str
    text: str
    source: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Promotion:
    """One recorded decision, exactly as the ``promotions`` row holds it.

    The numbers are stored rather than recomputed, so a past promotion can be audited without
    re-running either suite. ``reason`` carries the deciding rule from
    ``evals/scoring.py::RULES``: the word is the reason, and the sentence a human reads is
    rendered from it rather than stored, so a stored decision can never disagree with the
    vocabulary the rule engine actually uses.
    """

    cand_id: str
    decided_at: str
    promoted: bool
    score_before: float
    score_after: float
    passes_before: int
    passes_after: int
    regressions: int
    soft_drops: int
    reason: str


def proc_id_for(text: str) -> str:
    """The ``proc_id`` ``text`` would be stored under, redaction included.

    Exposed so a caller can ask whether a procedure is already known without writing one, and so
    a test can assert that a rejected candidate left no row behind.
    """
    return ids.content_hash("proc", redact.scrub(text))


def put(conn: sqlite3.Connection, text: str, source: str) -> Procedure:
    """Write one procedure, or return the one already stored under the same content hash.

    Redacted first (FR-13), and the hash is taken over the redacted text rather than the raw
    text: hashing the raw text would file two rows under different ids for what reads back as
    one identical procedure.
    """
    text = redact.scrub(text)
    source = redact.scrub(source)
    proc_id = ids.content_hash("proc", text)
    existing = get(conn, proc_id)
    if existing is not None:
        return existing
    procedure = Procedure(
        proc_id=proc_id, text=text, source=source, created_at=clock.isoformat(clock.now())
    )
    db.write(
        conn,
        (
            (
                _INSERT,
                (procedure.proc_id, procedure.text, procedure.source, procedure.created_at),
            ),
        ),
    )
    return procedure


def get(conn: sqlite3.Connection, proc_id: str) -> Procedure | None:
    """One procedure by id, or ``None``. A miss is an answer, not an error."""
    row = conn.execute(_SELECT_ONE, (proc_id,)).fetchone()
    return None if row is None else Procedure(*row)


def all(conn: sqlite3.Connection) -> tuple[Procedure, ...]:
    """Every procedure version ever written, oldest first. Nothing is ever removed."""
    return tuple(Procedure(*row) for row in conn.execute(_SELECT_ALL))


def active(conn: sqlite3.Connection) -> tuple[Procedure, ...]:
    """The procedures currently in the active set, in the order they were activated."""
    return tuple(Procedure(*row) for row in conn.execute(_SELECT_ACTIVE))


def activate(conn: sqlite3.Connection, proc_id: str, *, cand_id: str | None) -> None:
    """Make ``proc_id`` the whole active set: the old rows leave, the procedures stay.

    Idempotent by construction - clearing before inserting means activating the same procedure
    twice leaves one row, so a retried commit cannot double the set. Raises
    :class:`~harnessed.errors.StorageError` when ``proc_id`` names a procedure that was never
    written, because an active set pointing at a row that is not in procedural memory is a
    dangling promotion rather than a recoverable state.
    """
    if get(conn, proc_id) is None:
        raise StorageError(
            f"cannot activate {proc_id!r}: no procedure is stored under that id. A procedure is "
            f"written before it is activated, never the other way around."
        )
    db.write(conn, _activation(proc_id, cand_id))


def promotion(conn: sqlite3.Connection, cand_id: str) -> Promotion | None:
    """The first recorded decision for ``cand_id``, or ``None``. A miss is an answer."""
    row = conn.execute(_SELECT_PROMOTION, (cand_id,)).fetchone()
    return None if row is None else _promotion(row)


def promotions(conn: sqlite3.Connection, limit: int = 20) -> tuple[Promotion, ...]:
    """At most ``limit`` recorded decisions across every candidate, in decided order.

    The history the gateway lists (FR-41). Additive to :func:`promotion`, which keeps answering
    the one question the promoter's idempotency rests on - the first decision for one candidate
    - and is not widened by this. Nothing is re-derived: each row is the decision exactly as
    ``improve/promoter.py`` recorded it (MUST rule 1). An empty store lists nothing.
    """
    return tuple(_promotion(row) for row in conn.execute(_SELECT_PROMOTIONS, (limit,)))


def _promotion(row: tuple[Any, ...]) -> Promotion:
    """One ``promotions`` row as the typed decision, columns in ``_PROMOTION_COLUMNS`` order."""
    stored, decided_at, promoted, before, after, was, now, broke, dropped, reason = row
    return Promotion(
        cand_id=str(stored),
        decided_at=str(decided_at),
        promoted=bool(promoted),
        score_before=float(before),
        score_after=float(after),
        passes_before=int(was),
        passes_after=int(now),
        regressions=int(broke),
        soft_drops=int(dropped),
        reason=str(reason),
    )


def record_promotion(
    conn: sqlite3.Connection,
    *,
    cand_id: str,
    text: str,
    promoted: bool,
    reason: str,
    score_before: float,
    score_after: float,
    passes_before: int,
    passes_after: int,
    regressions: int,
    soft_drops: int,
) -> Promotion:
    """Record one decision, and on an accept write the procedure and swap the active set.

    One :func:`~harnessed.memory.db.write`, so all of it commits or none of it does. A rejection
    writes the decision row and nothing else: the candidate text is not stored as a procedure,
    because a procedure that was never active is not a version of anything.
    """
    text = redact.scrub(text)
    decided_at = clock.isoformat(clock.now())
    record = Promotion(
        cand_id=cand_id,
        decided_at=decided_at,
        promoted=promoted,
        score_before=score_before,
        score_after=score_after,
        passes_before=passes_before,
        passes_after=passes_after,
        regressions=regressions,
        soft_drops=soft_drops,
        reason=reason,
    )
    statements: tuple[tuple[str, tuple[object, ...]], ...] = (
        (
            _RECORD_PROMOTION,
            (
                record.cand_id,
                record.decided_at,
                int(record.promoted),
                record.score_before,
                record.score_after,
                record.passes_before,
                record.passes_after,
                record.regressions,
                record.soft_drops,
                record.reason,
            ),
        ),
    )
    if promoted:
        proc_id = ids.content_hash("proc", text)
        statements = (
            *statements,
            (_INSERT, (proc_id, text, f"promotion of {cand_id}", decided_at)),
            *_activation(proc_id, cand_id, activated_at=decided_at),
        )
    db.write(conn, statements)
    return record


def _activation(
    proc_id: str, cand_id: str | None, activated_at: str | None = None
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    """Clear the active set, then name one procedure. The two statements are never separated."""
    stamp = clock.isoformat(clock.now()) if activated_at is None else activated_at
    return (
        (_CLEAR_ACTIVE, ()),
        (_ACTIVATE, (proc_id, stamp, cand_id)),
    )
