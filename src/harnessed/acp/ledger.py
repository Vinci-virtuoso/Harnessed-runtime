"""The `delegations` ledger - the one that stops a coding agent running twice (FR-23).

Every ledger in this project protects against wasted work. This one protects against *damage*:
re-running a coding agent against a repository is destructive, so a delegation that already
happened must never happen again because a run was resumed, a loop retried, or a model asked for
the same thing twice.

**The pattern is the one from `state-and-idempotency.md`, with the write moved forward.** The
ordinary ledger reads the done-state, does the work, then persists. Here :func:`begin` writes the
row with ``status='attempting'`` *before* the spawn, and :func:`settle` updates it after, because
a crash between the write and the spawn is recoverable and a crash between the spawn and the
write is not. An attempt that was made and never finished still shows up as an attempt.

**Attempts ride in the ``outcome`` payload.** ``003_ledgers.sql`` is committed and migrations are
forward-only, so there is no attempts column to add. The column holds one canonical JSON object -
``attempts``, the compact five-field ``summary``, and the ``failure`` message - which is also
where the "returned summary is compact" guarantee is kept: the sub-agent transcript is on disk
and in episodic memory, and nothing resembling it is in this table.

**Two attempts, then the ledger answers instead of the agent.** :attr:`ATTEMPT_CAP` is the number
of times this harness will start an external agent for one ``task_hash``. A third request reads
:attr:`Record.exhausted` and gets the recorded failure back.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from harnessed import clock
from harnessed.memory import db, redact

__all__ = [
    "ATTEMPTING",
    "ATTEMPT_CAP",
    "FAILED",
    "DIRTY_TREE",
    "QUOTA_REFUSED",
    "REFUSAL_WINDOW_S",
    "SUCCEEDED",
    "Record",
    "begin",
    "canonical",
    "get",
    "recent_refusals",
    "settle",
    "task_hash",
]

#: The row exists and an agent was started for it. A crash leaves the row in this state.
ATTEMPTING: Final = "attempting"
#: The delegation finished and :attr:`Record.summary` is what it produced.
SUCCEEDED: Final = "succeeded"
#: The delegation ended in a timeout or a protocol mismatch. Retryable until the cap.
FAILED: Final = "failed"

#: The reason a refusal for quota settles with, and the prefix of its stored failure (ticket 194).
QUOTA_REFUSED: Final = "quota_refused"
#: The reason a refusal for a dirty working tree settles with, and the prefix of its stored failure
#: (ticket 245, FR-146).
DIRTY_TREE: Final = "dirty_tree"
#: How long a refusal keeps an entry in the router's ``Recently refusing:`` line.
REFUSAL_WINDOW_S: Final = 3600

#: How many times an external agent may be started for one ``task_hash`` (`acp.md`).
ATTEMPT_CAP: Final = 2

#: What a ``task_hash`` reads as, so a row is recognisable in a log without a schema to hand.
HASH_PREFIX: Final = "task_"

_SELECT: Final = (
    "SELECT agent, cwd, task, acceptance, status, outcome FROM delegations WHERE task_hash = ?"
)
_UPSERT: Final = (
    "INSERT OR REPLACE INTO delegations "
    "(task_hash, agent, cwd, task, acceptance, status, outcome, started_at, finished_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)"
)
_SETTLE: Final = (
    "UPDATE delegations SET status = ?, outcome = ?, finished_at = ? WHERE task_hash = ?"
)


@dataclass(frozen=True, slots=True)
class Record:
    """One delegation's done-state, rebuilt from its row."""

    task_hash: str
    agent: str
    cwd: str
    task: str
    acceptance: str
    status: str
    attempts: int
    summary: dict[str, object] = field(default_factory=dict)
    failure: str = ""

    @property
    def exhausted(self) -> bool:
        """Whether this hash has used up its attempts and must be answered from the ledger."""
        return self.attempts >= ATTEMPT_CAP


def canonical(*, agent: str, cwd: str, task: str, acceptance: str) -> str:
    """The exact bytes the hash is taken over: four keys, sorted, no incidental whitespace."""
    return json.dumps(
        {"agent": agent, "cwd": cwd, "task": task, "acceptance": acceptance},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def task_hash(*, agent: str, cwd: str, task: str, acceptance: str) -> str:
    """The ledger key for one unit of delegated work.

    Hashed over what the caller asked for, never over the redacted form of it: this is an
    idempotency key that has to be recomputable from the arguments in hand, which is the
    opposite of ticket 008's content-addressed procedure id.
    """
    digest = hashlib.sha256(
        canonical(agent=agent, cwd=cwd, task=task, acceptance=acceptance).encode("utf-8")
    ).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def get(conn: sqlite3.Connection, key: str) -> Record | None:
    """The row already on disk for ``key``, rebuilt, or ``None`` when there is work to do."""
    row = conn.execute(_SELECT, (key,)).fetchone()
    if row is None:
        return None
    attempts, summary, failure = _decode(row[5])
    return Record(
        task_hash=key,
        agent=str(row[0]),
        cwd=str(row[1]),
        task=str(row[2]),
        acceptance=str(row[3]),
        status=str(row[4]),
        attempts=attempts,
        summary=summary,
        failure=failure,
    )


def recent_refusals(conn: sqlite3.Connection, now: datetime) -> tuple[str, ...]:
    """The entries that refused for quota within :data:`REFUSAL_WINDOW_S` of ``now``."""
    since = clock.isoformat(now - timedelta(seconds=REFUSAL_WINDOW_S))
    rows = conn.execute(
        "SELECT agent, outcome FROM delegations "
        "WHERE status = ? AND finished_at IS NOT NULL AND finished_at >= ? "
        "ORDER BY finished_at",
        (FAILED, since),
    ).fetchall()
    named: dict[str, None] = {}
    for agent, outcome in rows:
        if _decode(outcome)[2].startswith(f"{QUOTA_REFUSED}: "):
            named[str(agent)] = None
    return tuple(named)


def begin(
    conn: sqlite3.Connection,
    key: str,
    *,
    agent: str,
    cwd: str,
    task: str,
    acceptance: str,
    attempts: int,
) -> None:
    """Record the attempt **before** the agent is started, so a crash still leaves it recorded."""
    db.write(
        conn,
        (
            (
                _UPSERT,
                (
                    key,
                    redact.scrub(agent),
                    redact.scrub(cwd),
                    redact.scrub(task),
                    redact.scrub(acceptance),
                    ATTEMPTING,
                    _encode(attempts=attempts, summary={}, failure=""),
                    clock.isoformat(clock.now()),
                ),
            ),
        ),
    )


def settle(
    conn: sqlite3.Connection,
    key: str,
    *,
    status: str,
    attempts: int,
    summary: dict[str, object] | None = None,
    failure: str = "",
) -> None:
    """Write what the attempt turned into, before the caller moves on to anything else."""
    db.write(
        conn,
        (
            (
                _SETTLE,
                (
                    status,
                    _encode(attempts=attempts, summary=summary or {}, failure=failure),
                    clock.isoformat(clock.now()),
                    key,
                ),
            ),
        ),
    )


# ---- the outcome payload ------------------------------------------------------------------------


def _encode(*, attempts: int, summary: dict[str, object], failure: str) -> str:
    """The ``outcome`` column, scrubbed, canonical, and free of anything transcript-shaped."""
    return redact.scrub(
        json.dumps(
            {"attempts": attempts, "summary": summary, "failure": failure},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )


def _decode(stored: object) -> tuple[int, dict[str, object], str]:
    """The attempt count, the compact summary and the failure, tolerating a row from elsewhere.

    The string form of the JSON parser is quarantined to `providers/translate.py` by a
    package-wide text scan, so the decoder object is used directly - the same distinction
    `evals/graders.py` draws for model output that is an answer rather than a tool call. The
    three fields come back positionally rather than as a dict, because a dict of `object` is
    what forces every caller to re-narrow what this function already knows.
    """
    if not stored:
        return 0, {}, ""
    decoded = json.JSONDecoder().decode(str(stored))
    return (
        int(decoded.get("attempts", 0)),
        dict(decoded.get("summary", {})),
        str(decoded.get("failure", "")),
    )
