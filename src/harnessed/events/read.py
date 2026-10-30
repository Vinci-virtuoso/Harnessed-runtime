"""The ordered read of the ``events`` table, keyed by session id (FR-38).

``bus.py`` writes the table and, until this module, nothing in ``src/`` read it. ``001_init.sql``
declares the ``(run_id, seq)`` primary key so that a replay can walk it in order, and
:func:`replay` is that walk: every row under one **session id**, ascending ``seq``, decoded back
into the envelope the bus stamped. The read lives here rather than in ``gateway/`` because a
query belongs to the package that owns its table (MUST rule 14), and because the CLI must be able
to call it without an HTTP server in the way.

The glossary is enforced in the names. The table's column is spelled ``run_id`` and holds a
**session id** and nothing else: an eval run id keys ``eval_runs`` and a case run key keys
``turns``, and neither ever appears in an event envelope (decision 14, MUST rule 15). So the
Python surface says ``session_id`` throughout and the column name appears only inside the query.

The replay tells the truth or refuses. A row whose name is outside the frozen taxonomy is an
:class:`UnknownEventError` on the way out, exactly as ``publish()`` refuses it on the way in; a
hole in ``seq`` is an :class:`EventError` naming the gap rather than a renumbered sequence; and
a session id with no rows is a :class:`SurfaceNotFound`, never an empty tuple, because "nothing
happened" and "no such session" are different claims. The payload column is decoded here with the
standard decoder object; the string-form parser is quarantined to ``providers/translate.py``
because its subject is model output, and a stored envelope this package wrote is not that.

A stream ends by pairing, never by name alone (decision 17, MUST rule 20). :data:`TERMINAL_PAIRS`
maps each start event to the finish that closes it, and :func:`terminal_for` reads the event at
seq 1 of a session id and answers with its pair. An improve session runs its baseline, candidate
and holdout evals on its own bus, so it holds three ``eval.run.finished`` events that are not its
end; its stream closes on ``improve.run.finished``, which ``improve/steps.py::run_cycle``
publishes on every return path. A session whose first event pairs with nothing, or whose
publisher crashed before its finish, closes on ``gateway/sse.py``'s idle ceiling. Both
groupings are derived from the frozen set and checked against it at import, never an edit to
``names.py`` (MUST rule 4).

The listing reads the same table the other way round (FR-48, decision 36). :func:`sessions`
takes the ``seq`` 1 row of every session id, newest first, and probes each one's newest row for
the finish its start pairs with; :func:`sessions_payload` is the emitter both ``GET /v1/sessions``
and ``harnessed sessions list --json`` render, so a session id is reachable without the terminal
that printed it. The pairing is the same one the stream uses, which is why a cycle whose
baseline eval has finished still lists as open.

The gate split is the third read of this table (FR-69, decisions 41 and 48). ``memory/gate.py``
publishes ``retrieval.gate.decided`` once per decision with ``retrieve`` and ``failed_open`` as
payload booleans, so :func:`gate_split` buckets on ``json_extract`` over the payload: failed open
first, then retrieve, else skip, with ``decided`` served as the sum of the three. The JSON is
read here and nowhere in ``gateway/``: MUST rule 14 bans SQL in the gateway, not JSON in a store
read. ``failed_open`` is never folded into ``retrieve``: a gate that failed open did retrieve, for
a different reason, and a figure that merged them would confirm something that is not true. The
two key names are spelled in :data:`GATE_KEYS` and a test holds them against the payload the gate
publishes, so a rename fails rather than silently zeroing a bucket. Migration
``009_event_indexes`` gives the read ``events_gate_idx``, a partial index on the decision event.

The totals are the fourth (FR-70, decisions 41 and 47): every row, and every ``seq`` 1 row, which
is what a session is to the listing above, so the tile and the listing cannot count sessions two
ways. :func:`totals` answers all time and stamps ``counted_at`` from the clock, and
:func:`totals_payload` is the emitter ``GET /v1/events/totals`` and ``harnessed events totals
--json`` both render.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Final

from harnessed import clock
from harnessed.errors import EventError, SurfaceNotFound, UnknownEventError
from harnessed.events.names import NAMES

__all__ = [
    "GATE_DECIDED",
    "GATE_KEYS",
    "TERMINAL_NAMES",
    "TERMINAL_PAIRS",
    "EventTotals",
    "GateSplit",
    "ReplayedEvent",
    "SessionSummary",
    "after",
    "gate_split",
    "gate_split_payload",
    "held",
    "json_payload",
    "latest",
    "replay",
    "run_pairs",
    "sessions",
    "sessions_payload",
    "terminal_for",
    "totals",
    "totals_payload",
]

#: The start event at seq 1 of a session, and the finish that ends that session (decision 17).
#: Every half is checked against the frozen set at import, so a rename there is an ImportError
#: here and not a stream that never closes.
TERMINAL_PAIRS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "run.started": "run.finished",
        "eval.run.started": "eval.run.finished",
        "improve.run.started": "improve.run.finished",
        # A task cancelled at rest is one event long: it opens and ends its own session (MUST 38).
        "task.finished": "task.finished",
        # A consolidation batch is one event long, in a session of its own (MUST 38).
        "memory.consolidation.finished": "memory.consolidation.finished",
    }
)

#: The finish halves: every name that can end *some* session. A vocabulary, not a rule - an
#: improve session holds ``eval.run.finished`` events that do not end it, so nothing decides a
#: close with this set (MUST rule 20); the stream asks :func:`terminal_for`.
TERMINAL_NAMES: Final[frozenset[str]] = frozenset(TERMINAL_PAIRS.values())

if not (set(TERMINAL_PAIRS) | TERMINAL_NAMES) <= NAMES:
    raise ImportError(
        f"TERMINAL_PAIRS names {sorted((set(TERMINAL_PAIRS) | TERMINAL_NAMES) - NAMES)}, which "
        f"the frozen taxonomy in events/names.py does not hold."
    )

#: The primary key's order, and nothing else: SQLite walks ``(run_id, seq)`` as declared.
_SELECT_SESSION: Final = (
    "SELECT seq, name, payload, created_at FROM events WHERE run_id = ? ORDER BY run_id, seq"
)

#: The tail of that order from a cursor, the SSE resumption read (FR-39).
_SELECT_AFTER: Final = (
    "SELECT seq, name, payload, created_at FROM events WHERE run_id = ? AND seq > ? "
    "ORDER BY run_id, seq"
)

#: The newest row under a session id, which is what says whether its stream has already closed.
_SELECT_LATEST: Final = (
    "SELECT seq, name, payload, created_at FROM events WHERE run_id = ? ORDER BY run_id, seq DESC "
    "LIMIT 1"
)

#: The event that opened a session: seq 1, by primary key.
_SELECT_FIRST: Final = "SELECT name FROM events WHERE run_id = ? AND seq = 1"

#: Every session's opening row, newest first, ties by session id descending (decision 36). One
#: row per session because ``seq`` 1 happens once per session id: a case run journals turns and
#: publishes nothing, and an improve cycle's inner evals publish onto the cycle's own bus, so
#: neither is a session here.
#:
#: **Cost, measured then paid:** v3 accepted the scan this read implied (``seq = 1`` is not the
#: primary key), and decision 47 measured it at 163.1 ms over 500,000 events. Migration
#: ``009_event_indexes`` adds ``events_first_idx``, a partial index on exactly this ``WHERE`` and
#: ``ORDER BY``, which brings the read to 8.8 ms. The SQL is decision 36's, unchanged but for v5's
#: own ``LEFT JOIN`` (decision 66): ``title`` is the session's own first message's ``task``, off
#: ``decisions`` at ``message_seq = 1, hop = 0`` - the row a routed session's own ``seq`` 1 start
#: always carries as its ``message_seq`` - measured at 24.7 ms against decision 66's seeded
#: database, inside the 8.8 ms this join adds to.
_SELECT_SESSIONS: Final = (
    "SELECT e.run_id, e.name, e.created_at, d.task FROM events e "
    "LEFT JOIN decisions d ON d.run_id = e.run_id AND d.message_seq = 1 AND d.hop = 0 "
    "WHERE e.seq = 1 ORDER BY e.created_at DESC, e.run_id DESC LIMIT ?"
)

#: The session's newest hop-0 decision, as the History row reads it (ticket 232): the entry's own
#: name for an agent route, ``groq`` for a chat route, ``failed`` when any hop of that message's
#: handler settled failed (migration 018), the cost and the decision's own ``created_at``.
_SELECT_LAST_HOP0: Final = (
    "SELECT CASE d.handler WHEN 'agent' THEN d.route WHEN 'groq' THEN 'groq' END, "
    "CASE WHEN EXISTS (SELECT 1 FROM decisions f WHERE f.run_id = d.run_id "
    "  AND f.message_seq = d.message_seq AND f.result = 'failed') "
    "THEN 'failed' ELSE d.outcome END, d.cost, d.created_at "
    "FROM decisions d WHERE d.run_id = ? AND d.hop = 0 ORDER BY d.idx DESC LIMIT 1"
)

#: Whether the table holds any row under a session id: a primary-key probe, never a scan,
#: because the twelve concurrent reads of ticket 065 must not pay a walk per request.
_SELECT_HELD: Final = "SELECT 1 FROM events WHERE run_id = ? LIMIT 1"

#: How many sessions the listing answers with when the caller names no limit (decision 36).
DEFAULT_SESSION_LIMIT: Final = 20

#: The gate's decision event, and the two payload keys ``memory/gate.py`` publishes in it that
#: the split buckets on (decision 48). Spelled here rather than imported: ``events/`` reaches
#: nothing under ``memory/`` (the import table), and the guard is a test that asserts these
#: against ``gate.DECIDED`` and the published payload, so a rename there fails here.
GATE_DECIDED: Final = "retrieval.gate.decided"
GATE_KEYS: Final[tuple[str, str]] = ("retrieve", "failed_open")

if GATE_DECIDED not in NAMES:
    raise ImportError(
        f"GATE_DECIDED names {GATE_DECIDED!r}, which the frozen taxonomy in events/names.py "
        f"does not hold."
    )

_RETRIEVE: Final = f"json_extract(payload, '$.{GATE_KEYS[0]}')"
_FAILED_OPEN: Final = f"json_extract(payload, '$.{GATE_KEYS[1]}')"

#: The three buckets in one pass: failed open first, then retrieve, else skip, so every decision
#: row lands in exactly one and the sum of the three is the count of decisions. ``SUM`` over no
#: rows is ``NULL``, hence the ``COALESCE``: an empty table is three zeros, never three nulls.
#: The event name is a **literal**, not a parameter, because that is what lets SQLite prove
#: migration 009's partial index ``events_gate_idx`` applies; a bound ``?`` proves nothing and
#: the planner would scan.
_SELECT_GATE_SPLIT: Final = (
    "SELECT "
    f"COALESCE(SUM(CASE WHEN {_FAILED_OPEN} THEN 0 WHEN {_RETRIEVE} THEN 0 ELSE 1 END), 0), "
    f"COALESCE(SUM(CASE WHEN {_FAILED_OPEN} THEN 0 WHEN {_RETRIEVE} THEN 1 ELSE 0 END), 0), "
    f"COALESCE(SUM(CASE WHEN {_FAILED_OPEN} THEN 1 ELSE 0 END), 0) "
    f"FROM events WHERE name = '{GATE_DECIDED}'"
)

#: Every row, and every session by its ``seq`` 1 row (FR-48's definition), in one statement. The
#: ``1`` is a literal for the reason the gate's name is: it is what lets SQLite prove migration
#: 009's partial index ``events_first_idx`` covers the second count.
_COUNT_EVENTS: Final = "SELECT COUNT(*), (SELECT COUNT(*) FROM events WHERE seq = 1) FROM events"

_decoder: Final = json.JSONDecoder()


@dataclass(frozen=True, slots=True)
class ReplayedEvent:
    """One stored event, read back: the envelope the bus stamped plus the payload it scrubbed."""

    session_id: str
    seq: int
    name: str
    ts: str
    payload: Mapping[str, object]


def replay(conn: sqlite3.Connection, session_id: str) -> tuple[ReplayedEvent, ...]:
    """Every event under ``session_id``, ascending ``seq``, contiguous from 1.

    Takes a bare connection so that the CLI, a test and the gateway all read through the one
    query. Raises :class:`SurfaceNotFound` for a session id the table does not hold,
    :class:`UnknownEventError` for a stored name outside the frozen set, and :class:`EventError`
    for a ``seq`` that is not the previous one plus one.
    """
    events: list[ReplayedEvent] = []
    for row in conn.execute(_SELECT_SESSION, (session_id,)):
        seq, name, body, created_at = int(row[0]), str(row[1]), str(row[2]), str(row[3])
        expected = len(events) + 1
        if seq != expected:
            raise EventError(
                f"session id {session_id!r} has seq {seq} where {expected} was expected: the "
                f"events table holds a gap or a duplicate, and the replay will not renumber it."
            )
        if name not in NAMES:
            raise UnknownEventError(
                f"session id {session_id!r} holds an event named {name!r} at seq {seq}, which "
                f"is not in the frozen taxonomy in events/names.py. The bus could not have "
                f"written it, so the replay refuses to serve it."
            )
        events.append(ReplayedEvent(session_id, seq, name, created_at, _payload(body)))
    if not events:
        raise SurfaceNotFound(
            f"no session id {session_id!r}: the events table holds no event under it. An eval "
            f"run id or a case run key is not a session id and never keys this table."
        )
    return tuple(events)


def after(conn: sqlite3.Connection, session_id: str, cursor: int) -> tuple[ReplayedEvent, ...]:
    """Every event under ``session_id`` with ``seq > cursor``, ascending, contiguous from
    ``cursor + 1``.

    The one ``seq > cursor`` query, owned here so ``gateway/sse.py`` formats frames and spells no
    SQL (MUST rule 14). An empty tuple means "nothing after the cursor", which is a clean close on
    the stream; a session id with no rows at all is :class:`SurfaceNotFound`, because those are
    different claims. A hole between the cursor and the first row, or inside the tail, is an
    :class:`EventError`, and a foreign name is :class:`UnknownEventError`, as in :func:`replay`.
    """
    events: list[ReplayedEvent] = []
    for row in conn.execute(_SELECT_AFTER, (session_id, cursor)):
        seq, name, body, created_at = int(row[0]), str(row[1]), str(row[2]), str(row[3])
        expected = cursor + len(events) + 1
        if seq != expected:
            raise EventError(
                f"session id {session_id!r} has seq {seq} where {expected} was expected after "
                f"cursor {cursor}: the events table holds a gap or a duplicate, and the tail "
                f"will not renumber it."
            )
        if name not in NAMES:
            raise UnknownEventError(
                f"session id {session_id!r} holds an event named {name!r} at seq {seq}, which "
                f"is not in the frozen taxonomy in events/names.py. The bus could not have "
                f"written it, so the tail refuses to serve it."
            )
        events.append(ReplayedEvent(session_id, seq, name, created_at, _payload(body)))
    if not events:
        latest(conn, session_id)
    return tuple(events)


def latest(conn: sqlite3.Connection, session_id: str) -> ReplayedEvent:
    """The newest event under ``session_id``: the highest ``seq`` and whether it is terminal.

    Raises :class:`SurfaceNotFound` for a session id the table does not hold, the one clause
    FR-38 and FR-39 share for an unknown id, an eval run id or a case run key in its place.
    """
    row = conn.execute(_SELECT_LATEST, (session_id,)).fetchone()
    if row is None:
        raise SurfaceNotFound(
            f"no session id {session_id!r}: the events table holds no event under it. An eval "
            f"run id or a case run key is not a session id and never keys this table."
        )
    seq, name, body, created_at = int(row[0]), str(row[1]), str(row[2]), str(row[3])
    if name not in NAMES:
        raise UnknownEventError(
            f"session id {session_id!r} holds an event named {name!r} at seq {seq}, which is "
            f"not in the frozen taxonomy in events/names.py."
        )
    return ReplayedEvent(session_id, seq, name, created_at, _payload(body))


def run_pairs(
    events: Sequence[ReplayedEvent],
) -> tuple[tuple[ReplayedEvent, ReplayedEvent | None], ...]:
    """Every ``run.started``/``run.finished`` pair in ``events``, found by name alone (decision 51).

    A ``run.started`` with no ``run.finished`` before the stream ends is a live message: it pairs
    with ``None``. A chain's own route-decided events for hop 1 and up carry no pairing name and
    are skipped here, so they stay inside the one pair their message opened.

    It lives here rather than in ``loop/messages.py``, its one caller: pairing events by name is
    this module's reading of the events table, and ``loop/`` branches on no ``.name`` at all
    (``tests/test_loop_readability.py``).
    """
    pairs: list[tuple[ReplayedEvent, ReplayedEvent | None]] = []
    pending: ReplayedEvent | None = None
    for event in events:
        if event.name == "run.started":
            if pending is not None:
                pairs.append((pending, None))
            pending = event
        elif event.name == "run.finished" and pending is not None:
            pairs.append((pending, event))
            pending = None
    if pending is not None:
        pairs.append((pending, None))
    return tuple(pairs)


def terminal_for(conn: sqlite3.Connection, session_id: str) -> str | None:
    """The finish event that ends ``session_id``'s stream, paired with its seq 1 start.

    ``None`` when the first event pairs with nothing: that stream closes on the idle ceiling
    alone. Raises :class:`SurfaceNotFound` for a session id the table does not hold, the same
    clause :func:`latest` answers with.
    """
    row = conn.execute(_SELECT_FIRST, (session_id,)).fetchone()
    if row is None:
        raise SurfaceNotFound(
            f"no session id {session_id!r}: the events table holds no event under it. An eval "
            f"run id or a case run key is not a session id and never keys this table."
        )
    return TERMINAL_PAIRS.get(str(row[0]))


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One session as the listing carries it: how it opened, when, and whether it has closed.

    ``title`` is the session's first message's operator text (decision 66, FR-128): the hop-0
    ``decisions`` row at ``message_seq = 1``, or ``None`` for a session that never routed a
    message - the same "before the router" null every other v5 read carries, never guessed.
    """

    session_id: str
    first_event: str
    first_ts: str
    terminal_event: str | None
    title: str | None
    #: The newest hop-0 decision's own handler, outcome, cost and time, as strings (ticket 232);
    #: all four ``None`` for a session that never routed a message.
    last_handler: str | None = None
    last_outcome: str | None = None
    last_cost: str | None = None
    last_at: str | None = None


def sessions(
    conn: sqlite3.Connection, limit: int = DEFAULT_SESSION_LIMIT
) -> tuple[SessionSummary, ...]:
    """At most ``limit`` sessions, newest first by the ``created_at`` of their ``seq`` 1 row.

    FR-48. A session id is minted where its first event is published and printed nowhere else,
    which is why UJ-11 could not start without a terminal; this is the enumeration that answers
    "which sessions are there". Ties on the timestamp break by session id descending, so the
    order is total and a listing read twice is the same listing.

    Every field is read from a row. ``first_event`` and ``first_ts`` are the ``name`` and
    ``created_at`` columns of the opening row; ``terminal_event`` is the **newest** row's name,
    and only when that name is the finish :data:`TERMINAL_PAIRS` puts against the start (MUST
    rule 20). An improve cycle holds ``eval.run.finished`` events from its baseline, candidate
    and holdout evals, and none of them ends it: until ``improve.run.finished`` lands, the
    listing says ``None``. A session whose first event pairs with nothing says ``None`` too.

    Raises :class:`UnknownEventError` for a stored name outside the frozen taxonomy, as
    :func:`replay` and :func:`latest` do: a listing is still a read of this table, and a row the
    bus could not have written is not served.
    """
    rows: list[SessionSummary] = []
    for row in conn.execute(_SELECT_SESSIONS, (limit,)).fetchall():
        session_id, first_event, first_ts = str(row[0]), str(row[1]), str(row[2])
        title = None if row[3] is None else str(row[3])
        if first_event not in NAMES:
            raise UnknownEventError(
                f"session id {session_id!r} opens on an event named {first_event!r}, which is "
                f"not in the frozen taxonomy in events/names.py. The bus could not have written "
                f"it, so the listing refuses to serve it."
            )
        newest = latest(conn, session_id)
        finish = TERMINAL_PAIRS.get(first_event)
        last = conn.execute(_SELECT_LAST_HOP0, (session_id,)).fetchone()
        last_handler, last_outcome, last_cost, last_at = (
            (None, None, None, None)
            if last is None
            else tuple(None if v is None else str(v) for v in last)
        )
        rows.append(
            SessionSummary(
                session_id=session_id,
                first_event=first_event,
                first_ts=first_ts,
                terminal_event=newest.name if newest.name == finish else None,
                title=title,
                last_handler=last_handler,
                last_outcome=last_outcome,
                last_cost=last_cost,
                last_at=last_at,
            )
        )
    return tuple(rows)


def sessions_payload(rows: Sequence[SessionSummary]) -> dict[str, object]:
    """The listing as the wire carries it: the rows under ``sessions``, in the order read.

    The one callable ``GET /v1/sessions`` and ``harnessed sessions list --json`` both render, so
    the gateway and the CLI cannot describe one session two ways (MUST rule 16, decision 38).
    An empty table is ``{"sessions": []}``: "nothing has happened yet" is an answer.
    """
    return {"sessions": [asdict(row) for row in rows]}


def held(conn: sqlite3.Connection, session_id: str) -> bool:
    """Whether ``session_id`` keys at least one row: whether the session exists at all.

    A session id is minted where its first event is published, so this table is where "is
    there such a session" is answered. The one existence query over ``events``, owned here so
    a store that must tell "held nothing" from "never existed" (``memory/episodic.py``,
    ticket 076) asks rather than spelling this table's SQL (MUST rule 14). A bool, not a
    raise: the caller decides what an absence means on its own surface.
    """
    return conn.execute(_SELECT_HELD, (session_id,)).fetchone() is not None


def json_payload(conn: sqlite3.Connection, session_id: str) -> dict[str, object]:
    """The collection as the wire carries it: the session id and its events, in order.

    The one callable ``GET /v1/sessions/{session_id}/events`` renders, so the gateway and a
    CLI reader cannot describe one session two ways (MUST rule 16).
    """
    return {
        "session_id": session_id,
        "events": [
            {"seq": e.seq, "name": e.name, "ts": e.ts, "payload": dict(e.payload)}
            for e in replay(conn, session_id)
        ],
    }


@dataclass(frozen=True, slots=True)
class GateSplit:
    """The retrieval gate's decisions over every run, in the three buckets decision 48 names.

    ``failed_open`` counts decisions the gate made for itself because the model never gave a
    usable answer; ``retrieve`` counts the ones the model plainly said yes to. Each decision is
    in exactly one bucket, which is why :attr:`decided` is their sum and not a fourth count.
    """

    skip: int
    retrieve: int
    failed_open: int

    @property
    def decided(self) -> int:
        """Every decision the gate has published: the three buckets summed here, never by a
        reader (FR-69)."""
        return self.skip + self.retrieve + self.failed_open


def gate_split(conn: sqlite3.Connection) -> GateSplit:
    """The three buckets over every persisted ``retrieval.gate.decided`` event, all time.

    FR-69, decision 41: an aggregate over one package's table is that package's store read. One
    statement, selected through ``events_gate_idx`` (migration 009), bucketing in SQL so the
    handler and the CLI carry three integers and derive nothing. An empty table is
    ``GateSplit(0, 0, 0)``: "the gate has not been asked yet" is an answer, never an absence.
    """
    skip, retrieve, failed_open = conn.execute(_SELECT_GATE_SPLIT).fetchone()
    return GateSplit(skip=int(skip), retrieve=int(retrieve), failed_open=int(failed_open))


def gate_split_payload(found: GateSplit) -> dict[str, object]:
    """The one wire shape for :func:`gate_split`, printed by the CLI and served by the gateway.

    Every figure is a **string** (MUST rule 29) and ``decided`` is served as its own field, so
    the tile renders four figures as sent and computes nothing (decision 38, MUST rule 16).
    """
    return {
        "skip": str(found.skip),
        "retrieve": str(found.retrieve),
        "failed_open": str(found.failed_open),
        "decided": str(found.decided),
    }


@dataclass(frozen=True, slots=True)
class EventTotals:
    """What the events table holds, counted once, all time (FR-70).

    ``sessions`` is the ``seq`` 1 rows, one per session id, the count the listing would reach
    if it had no limit. ``counted_at`` is when the count was taken, because a figure that says
    "all time" has to say which "now" that was.
    """

    events: int
    sessions: int
    counted_at: str


def totals(conn: sqlite3.Connection) -> EventTotals:
    """Every event and every session over the whole table - the store read behind
    ``GET /v1/events/totals`` and ``harnessed events totals`` (decision 41).

    An empty table counts to zero, not to nothing. Nothing is windowed and nothing is cached
    (decision 47): at its measured ceiling of about 1.4 million events this read crosses the
    200 ms NFR, and a read over 150 ms is the trigger to reopen that decision.
    """
    events, first_rows = conn.execute(_COUNT_EVENTS).fetchone()
    return EventTotals(
        events=int(events), sessions=int(first_rows), counted_at=clock.isoformat(clock.now())
    )


def totals_payload(found: EventTotals) -> dict[str, object]:
    """The one wire shape for :func:`totals`, printed by the CLI and served by the gateway.

    Every count is a **string** (MUST rule 29): the tile renders the characters as sent.
    """
    return {
        "events": str(found.events),
        "sessions": str(found.sessions),
        "counted_at": found.counted_at,
    }


def _payload(body: str) -> Mapping[str, object]:
    """The stored payload column back into the mapping the bus scrubbed and serialised."""
    decoded: object = _decoder.decode(body)
    if not isinstance(decoded, dict):
        raise EventError(
            f"an events row holds a payload that is not a JSON object: {body[:80]!r}. The bus "
            f"only ever writes an object, so this row was not written by it."
        )
    return {str(key): value for key, value in decoded.items()}
