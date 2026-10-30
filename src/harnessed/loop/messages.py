"""``GET /v1/sessions/{session_id}/messages`` and ``GET /v1/messages``: a session's messages, and
the newest-first activity list (FR-128, decision 66).

**A message is one ``run.started``/``run.finished`` pair** (decision 51), found by name alone
over the events table: :func:`for_session` walks the session's whole event stream
(``events/read.py::replay``, which owns the table) and pairs each ``run.started`` with the next
``run.finished`` - ``first_seq`` the start's ``seq``, ``last_seq`` the paired finish's, ``None``
while the pair has no finish yet (a live run). A chain's extra ``loop.route.decided`` events for
hop 1 and up sit inside the same pair, so a chain stays one exchange.

Everything else joins on ``(run_id, message_seq)``, read in SQL against ``decisions``,
``agent_turns``, ``turns`` and ``outputs`` - the tables ``router/``, ``acp/`` and ``loop/`` itself
own - the way ``loop/runs.py::spend`` reads ``prices`` (``fastapi.md``): ``text`` is the hop-0
decision's ``task``, ``null`` with :data:`TEXT_BEFORE_ROUTER` when no such row exists (a message
recorded before the router, or a router call that never wrote one); ``handler`` and ``outcome``
are the *last* decision's (a chain's final hop); ``reply`` is the last agent hop's own text result
when the ``outputs`` table holds one for this message, else the last ``turns.response``, else
``null`` with :data:`REPLY_NOTHING_RAN` (an uncertain stop, or ``needs_human``) or
:data:`REPLY_TASK_DONE` (routed ``done``); ``hops`` is every
hop the ``outputs`` table holds, grouped, each output's metadata only - never its body; ``steps``
is loop turns plus agent turns; ``seconds`` is the pair's two timestamps subtracted in Python
through ``clock.parse``, one decimal (decision 54); ``handlers`` is the message's own handler rows,
the same :class:`~harnessed.loop.runs.HandlerRow` shape decision 64 serves, scoped to this message
rather than the whole session.

:func:`recent` is ``GET /v1/messages?limit=``: newest first over hop-0 decisions alone, through
``decisions_recent_idx`` (migration 014) - a message that never routed never appears, which is
why the activity list reads messages and not sessions.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from harnessed import clock
from harnessed.events import read as events_read
from harnessed.loop.runs import (
    CURRENCY,
    HANDLER_AGENT,
    HANDLER_CHAT_MODEL,
    HANDLER_ROUTER,
    NO_AGENT_COST_REASON,
    NO_PRICES_REASON,
    NO_ROUTER_COST_REASON,
    NONE_PRICED_REASON,
    HandlerRow,
    handler_payload,
)

__all__ = [
    "DEFAULT_MESSAGE_LIMIT",
    "REPLY_NOTHING_RAN",
    "REPLY_TASK_DONE",
    "REPLY_TASK_FINISHED",
    "TEXT_BEFORE_ROUTER",
    "Hop",
    "MessageRow",
    "MessageSummary",
    "OutputMeta",
    "Reply",
    "for_session",
    "messages_payload",
    "recent",
    "recent_payload",
    "session_payload",
    "task_for",
]

#: Why ``text`` is ``null``: no hop-0 ``decisions`` row exists for this message's ``message_seq``
#: (decision 66). Never guessed from ``run_messages`` order.
TEXT_BEFORE_ROUTER: Final = "recorded before the router"

#: Why ``route`` is ``null`` for a message whose ``run.started`` says ``routing: "off"``
#: (ticket 316, decision 105): the router was never asked, so no decision and no route exist.
ROUTING_OFF: Final = "routing is off"

#: Why ``route`` is ``null`` when the router was asked and chose no route.
NO_ROUTE_CHOSEN: Final = "the router chose no route"

#: Why ``reply`` is ``null``: nothing ran for this message - an uncertain stop or ``needs_human``,
#: 0 turns and 0 delegations (decision 66).
REPLY_NOTHING_RAN: Final = "uncertain stop: nothing ran"

#: Why ``reply`` is ``null`` for a message the router routed ``done`` (a task judged finished at or
#: above ``TASK_DONE_FLOOR``): nothing ran because nothing was left to run, not an uncertain stop.
REPLY_TASK_DONE: Final = "the task was judged finished"

#: Why ``reply`` is ``null`` for a task's continuation once the task ended ``done`` / ``finished``
#: (``task_messages`` and ``tasks``, never inferred from the words, MUST rule 40; FR-168).
REPLY_TASK_FINISHED: Final = "the task is finished"

#: ``router/route.py``'s chain terminal (spelled here: ``loop/`` may not reach ``router/``).
_ROUTE_DONE: Final = "done"

#: A Groq reply's ``output_kind`` (``acp/outputs.py::KIND_TEXT``, spelled here rather than
#: imported: ``loop/`` may not reach ``acp/`` - IMPORT_TABLE - and no Groq turn ever passes
#: through that module either, decision 65).
_REPLY_KIND_TEXT: Final = "text"

#: A hop's outcome on the message when its handler failed (ticket 232, decision 73).
OUTCOME_FAILED: Final = "failed"

#: How many messages ``harnessed sessions recent`` and ``GET /v1/messages`` answer with when the
#: caller names no limit, matching ``events/read.py::DEFAULT_SESSION_LIMIT``.
DEFAULT_MESSAGE_LIMIT: Final = 20

#: Why a hop's ``result`` is ``null``: its ``decisions`` row predates migration 018 (ticket 232).
RESULT_BEFORE_018: Final = "recorded before Harnessed recorded a hop's result"

#: A message's ``author`` (ticket 241, decision 77).
AUTHOR_OPERATOR: Final = "operator"
AUTHOR_TASK: Final = "task"

_SELECT_HOP0_TASK: Final = (
    "SELECT task FROM decisions WHERE run_id = ? AND message_seq = ? AND hop = 0"
)
_SELECT_HOP_DECISIONS: Final = (
    "SELECT hop, handler, route, result, result_reason FROM decisions "
    "WHERE run_id = ? AND message_seq = ? ORDER BY hop"
)
_SELECT_LAST_DECISION: Final = (
    "SELECT handler, outcome FROM decisions WHERE run_id = ? AND message_seq = ? "
    "ORDER BY idx DESC LIMIT 1"
)
_SELECT_LAST_ROUTE: Final = (
    "SELECT route, outcome FROM decisions WHERE run_id = ? AND message_seq = ? "
    "ORDER BY idx DESC LIMIT 1"
)
_SELECT_LAST_TURN_REPLY: Final = (
    "SELECT response FROM turns WHERE run_id = ? AND message_seq = ? "
    "ORDER BY iteration DESC LIMIT 1"
)
_SELECT_LAST_HOP_TEXT_OUTPUT: Final = (
    "SELECT output_id, output_kind, body FROM outputs "
    "WHERE run_id = ? AND message_seq = ? AND position = 0 ORDER BY hop DESC LIMIT 1"
)
_SELECT_HOP_OUTPUTS: Final = (
    "SELECT hop, output_id, output_kind, media_type, name, caption, size_bytes, too_large "
    "FROM outputs WHERE run_id = ? AND message_seq = ? ORDER BY hop, position"
)
_SELECT_HOP_HASH: Final = (
    "SELECT hop, task_hash FROM outputs WHERE run_id = ? AND message_seq = ? "
    "AND task_hash IS NOT NULL ORDER BY position DESC"
)
_SELECT_REPO_RECORD: Final = (
    "SELECT attempt, head_before, head_after, head_before_short, head_after_short, branch, "
    "diff_stat, changed_count, started_dirty FROM repo_records WHERE task_hash = ? "
    "ORDER BY attempt DESC LIMIT 1"
)
_SELECT_REPO_PATHS: Final = (
    "SELECT path, kind FROM repo_paths WHERE task_hash = ? AND attempt = ? ORDER BY position"
)
#: Why a hop serves ``repository: null`` (ticket 244, FR-145).
REPOSITORY_NONE: Final = "not a git repository, or git was not available"
#: Why a hop with no delegation serves ``repository: null``.
REPOSITORY_NO_DELEGATION: Final = "no delegation ran in this hop"
_COUNT_TURNS: Final = "SELECT COUNT(*) FROM turns WHERE run_id = ? AND message_seq = ?"
_COUNT_AGENT_TURNS: Final = "SELECT COUNT(*) FROM agent_turns WHERE run_id = ? AND message_seq = ?"

#: The message-scoped twins of ``loop/runs.py``'s own spend queries, one ``AND message_seq = ?``
#: added to each ``WHERE`` (that module's own docstring: "the way spend reads prices").
_SELECT_SPEND_FOR_MESSAGE: Final = (
    "SELECT g.provider, g.model, g.held, g.no_usage, g.cached_turns, g.uncached, g.outputs, "
    "  g.cached, g.bare_inputs, g.bare_outputs, p.input, p.output, p.cached_input "
    "FROM (SELECT provider, model, COUNT(*) AS held, "
    "    SUM(input_tokens IS NULL OR output_tokens IS NULL OR cached_input_tokens IS NULL) "
    "      AS no_usage, "
    "    SUM(cached_input_tokens > 0) AS cached_turns, "
    "    SUM(input_tokens - cached_input_tokens) AS uncached, "
    "    SUM(output_tokens) AS outputs, SUM(cached_input_tokens) AS cached, "
    "    SUM(CASE WHEN cached_input_tokens = 0 THEN input_tokens END) AS bare_inputs, "
    "    SUM(CASE WHEN cached_input_tokens = 0 THEN output_tokens END) AS bare_outputs "
    "  FROM turns WHERE run_id = ? AND message_seq = ? GROUP BY provider, model) g "
    "LEFT JOIN prices p "
    f"  ON p.provider = g.provider AND p.model = g.model AND p.currency = '{CURRENCY}'"
)
_SELECT_ROUTER_FOR_MESSAGE: Final = (
    "SELECT COUNT(*), SUM(cost_nanos), SUM(cost_nanos IS NULL) FROM decisions "
    "WHERE run_id = ? AND message_seq = ?"
)
_SELECT_AGENT_ENTRIES_FOR_MESSAGE: Final = (
    "SELECT entry, billing, COUNT(*), "
    "  SUM(CASE WHEN cost_kind = 'reported' THEN cost_nanos END), "
    "  SUM(CASE WHEN cost_kind = 'api_equivalent' THEN cost_nanos END), "
    "  SUM(cost_kind IS NULL), MIN(idx) "
    "FROM agent_turns WHERE run_id = ? AND message_seq = ? "
    "GROUP BY entry, billing ORDER BY MIN(idx)"
)
_NEWEST_PRICE: Final = "SELECT MAX(fetched_at) FROM prices"

#: Six decimal places, half-up - the same rule ``loop/runs.py`` rounds a handler's cost by.
_SIX_PLACES: Final = Decimal("0.000001")
_PER_MILLION: Final = Decimal(1_000_000)
_PER_BILLION: Final = Decimal(1_000_000_000)

#: Who wrote a message and which of the task's it is: a ``LEFT JOIN`` on ``task_messages``, a row
#: the task composition writes and nobody infers (MUST rule 40). No row, or a database from before
#: migration 020, reads ``operator``.
_SELECT_TASK_MESSAGE: Final = (
    "SELECT tm.kind FROM (SELECT ? AS run_id, ? AS message_seq) m "
    "LEFT JOIN task_messages tm ON tm.run_id = m.run_id AND tm.message_seq = m.message_seq"
)

#: ``tasks/store.py::UNATTENDED_SQL`` word for word: ``loop/`` may not import ``tasks/``, so the
#: copy is held equal by ``tests/test_session_messages.py`` rather than by an import (ticket 241).
_UNATTENDED_SQL: Final = "CASE WHEN state = 'done' THEN (human_messages = 0 AND pauses = 0) END"
_SELECT_TASK: Final = (
    f"SELECT task_id, text, state, {_UNATTENDED_SQL} FROM tasks WHERE session_id = ?"
)

#: ``router/decisions.py::COMPARED_SQL`` word for word (decision 101): ``loop/`` may not import
#: that module's private names, so the copy is held equal by ``tests/test_session_messages.py``.
_COMPARED_SQL: Final = (
    "CASE WHEN d.outcome = 'uncertain' THEN "
    "CASE WHEN d.hop = 0 AND d.route = 'done' "
    "AND EXISTS (SELECT 1 FROM task_messages tm WHERE tm.run_id = d.run_id "
    "AND tm.message_seq = d.message_seq AND tm.kind = 'continuation') "
    "AND d.confidence = (SELECT o.probability FROM decision_options o WHERE o.run_id = d.run_id "
    "AND o.idx = d.idx AND o.question = 'route' AND o.option = 'done') "
    "THEN 'done_probability' ELSE 'confidence' END END"
)
_SELECT_HOP_COMPARED: Final = (
    f"SELECT d.hop, {_COMPARED_SQL} FROM decisions d WHERE d.run_id = ? AND d.message_seq = ? "
    "ORDER BY d.hop"
)
#: Why a hop's ``compared`` is ``null``: its decision acted, so no figure met a floor.
COMPARED_REASON: Final = "the router acted"

_SELECT_TASK_FINISHED: Final = (
    "SELECT 1 FROM task_messages tm JOIN tasks t ON t.task_id = tm.task_id "
    "WHERE tm.run_id = ? AND tm.message_seq = ? AND tm.kind = 'continuation' "
    "AND t.state = 'done' AND t.stop_reason = 'finished'"
)

_SELECT_RECENT: Final = (
    "SELECT d.run_id, d.message_seq, d.task, d.handler, "
    "CASE WHEN EXISTS (SELECT 1 FROM decisions f WHERE f.run_id = d.run_id "
    "  AND f.message_seq = d.message_seq AND f.result = 'failed') "
    f"THEN '{OUTCOME_FAILED}' ELSE d.outcome END, d.cost, d.created_at, "
    f"CASE WHEN tm.task_id IS NULL THEN '{AUTHOR_OPERATOR}' ELSE '{AUTHOR_TASK}' END, tm.task_id "
    "FROM decisions d LEFT JOIN task_messages tm "
    "  ON tm.run_id = d.run_id AND tm.message_seq = d.message_seq "
    "WHERE d.hop = 0 ORDER BY d.created_at DESC, d.run_id DESC LIMIT ?"
)


@dataclass(frozen=True, slots=True)
class OutputMeta:
    """One output's metadata, as a message's ``hops`` carry it. Never the body."""

    output_id: str
    output_kind: str
    media_type: str
    name: str | None
    caption: str | None
    size_bytes: int
    too_large: bool


@dataclass(frozen=True, slots=True)
class Hop:
    """One hop's outputs, in position order."""

    hop: int
    outputs: tuple[OutputMeta, ...]
    #: ``succeeded``, ``failed`` or ``None`` for a hop recorded before migration 018 (ticket 232).
    result: str | None = None
    #: Why a hop failed, or why ``result`` is ``None``; ``None`` for a success.
    reason: str | None = None
    #: What the repository became under this hop's delegation (ticket 244, FR-145), or ``None``.
    repository: dict[str, str | list[str]] | None = None
    #: Why ``repository`` is ``None``; ``None`` when it is served.
    repository_reason: str | None = None
    #: ``done_probability`` or ``confidence`` when this hop's decision was an uncertain stop, the
    #: figure it compared with its floor (decision 101, FR-171); else ``None``.
    compared: str | None = None
    #: Why ``compared`` is ``None``; ``None`` when it is served.
    compared_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """A message's reply: a Groq route's last text, or an agent route's last hop, position 0."""

    output_kind: str
    text: str | None
    output_id: str | None


@dataclass(frozen=True, slots=True)
class MessageRow:
    """One message, as ``GET /v1/sessions/{id}/messages`` and its CLI twin serve it (decision 66).

    ``message_seq`` and ``first_seq`` are the same ``run.started`` ``seq`` (decision 51 and 61);
    both are carried because the first joins the stored rows and the second names the pair's own
    start. ``last_seq`` is ``None`` while the pair has no finish yet.
    """

    message_seq: int
    first_seq: int
    last_seq: int | None
    text: str | None
    text_reason: str | None
    handler: str | None
    outcome: str | None
    reply: Reply | None
    reply_reason: str | None
    hops: tuple[Hop, ...]
    steps: int
    seconds: str | None
    handlers: tuple[HandlerRow, ...]
    created_at: str
    #: ``operator`` or ``task``, and for a task's message ``first`` or ``continuation``.
    author: str = AUTHOR_OPERATOR
    task_message: str | None = None
    #: The route the message's last decision chose, or ``None`` (ticket 316).
    route: str | None = None
    #: Why ``route`` is ``None``; ``None`` when it is served.
    route_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MessageSummary:
    """One row of the activity list: :func:`recent`'s own, lighter shape (decision 66)."""

    session_id: str
    message_seq: int
    text: str | None
    handler: str | None
    outcome: str
    cost: str | None
    created_at: str
    author: str = AUTHOR_OPERATOR
    task_id: str | None = None


def _task_message_for(conn: sqlite3.Connection, run_id: str, message_seq: int) -> str | None:
    (kind,) = conn.execute(_SELECT_TASK_MESSAGE, (run_id, message_seq)).fetchone()
    return None if kind is None else str(kind)


def _text_for(
    conn: sqlite3.Connection, run_id: str, message_seq: int
) -> tuple[str | None, str | None]:
    row = conn.execute(_SELECT_HOP0_TASK, (run_id, message_seq)).fetchone()
    if row is None:
        return None, TEXT_BEFORE_ROUTER
    return str(row[0]), None


def _last_decision(
    conn: sqlite3.Connection, run_id: str, message_seq: int
) -> tuple[str | None, str | None]:
    row = conn.execute(_SELECT_LAST_DECISION, (run_id, message_seq)).fetchone()
    if row is None:
        return None, None
    handler, outcome = row
    return (None if handler is None else str(handler)), str(outcome)


def _reason_from_hops(hops: tuple[Hop, ...], handlers: dict[int, str]) -> str:
    """Why a reply-less message has none, from its own hops (ticket 232): the last failed hop,
    else nothing ran."""
    for hop in reversed(hops):
        if hop.result == "failed":
            who = handlers.get(hop.hop, "the handler")
            return f"the delegation to {who} failed: {hop.reason}"
    return REPLY_NOTHING_RAN


def _reply_for(
    conn: sqlite3.Connection, run_id: str, message_seq: int, hops: tuple[Hop, ...]
) -> tuple[Reply | None, str | None]:
    """The turn's text first (ticket 198: every chain ends in one Groq synthesis pass), then the
    last hop output of a message recorded before that, then the reason from its events."""
    turn = conn.execute(_SELECT_LAST_TURN_REPLY, (run_id, message_seq)).fetchone()
    if turn is not None:
        return Reply(output_kind=_REPLY_KIND_TEXT, text=str(turn[0]), output_id=None), None
    row = conn.execute(_SELECT_LAST_HOP_TEXT_OUTPUT, (run_id, message_seq)).fetchone()
    if row is not None:
        output_id, output_kind, body = row
        text = None if body is None else bytes(body).decode("utf-8")
        return Reply(output_kind=str(output_kind), text=text, output_id=str(output_id)), None
    reason = _reason_from_hops(hops, _routes_by_hop(conn, run_id, message_seq))
    if reason == REPLY_NOTHING_RAN and _routed_done(conn, run_id, message_seq):
        reason = REPLY_TASK_DONE
    if reason in (REPLY_NOTHING_RAN, REPLY_TASK_DONE) and _task_finished(conn, run_id, message_seq):
        reason = REPLY_TASK_FINISHED
    return None, reason


def _route_for(
    conn: sqlite3.Connection, run_id: str, start: events_read.ReplayedEvent
) -> tuple[str | None, str | None]:
    """The last decision's route, else why there is none: routing off (the ``run.started`` says
    so), a decision that chose nothing, or a message that predates the router."""
    row = conn.execute(_SELECT_LAST_ROUTE, (run_id, start.seq)).fetchone()
    if row is not None:
        return (str(row[0]), None) if row[0] is not None else (None, NO_ROUTE_CHOSEN)
    if start.payload.get("routing") == "off":
        return None, ROUTING_OFF
    return None, TEXT_BEFORE_ROUTER


def _routed_done(conn: sqlite3.Connection, run_id: str, message_seq: int) -> bool:
    """Whether the message's last decision routed ``done`` (never an uncertain stop)."""
    row = conn.execute(_SELECT_LAST_ROUTE, (run_id, message_seq)).fetchone()
    return row is not None and row[0] == _ROUTE_DONE and row[1] == "routed"


def _task_finished(conn: sqlite3.Connection, run_id: str, message_seq: int) -> bool:
    """Whether this is a continuation of a task that ended ``done`` / ``finished``."""
    return conn.execute(_SELECT_TASK_FINISHED, (run_id, message_seq)).fetchone() is not None


def _routes_by_hop(conn: sqlite3.Connection, run_id: str, message_seq: int) -> dict[int, str]:
    """Who each hop's decision handed it to: an entry's own name, else the handler."""
    return {
        int(hop): str(route or handler or "the handler")
        for hop, handler, route, _result, _reason in conn.execute(
            _SELECT_HOP_DECISIONS, (run_id, message_seq)
        )
    }


def _hops_for(conn: sqlite3.Connection, run_id: str, message_seq: int) -> tuple[Hop, ...]:
    grouped: dict[int, list[OutputMeta]] = {}
    for (
        hop,
        output_id,
        output_kind,
        media_type,
        name,
        caption,
        size_bytes,
        too_large,
    ) in conn.execute(_SELECT_HOP_OUTPUTS, (run_id, message_seq)):
        grouped.setdefault(int(hop), []).append(
            OutputMeta(
                output_id=str(output_id),
                output_kind=str(output_kind),
                media_type=str(media_type),
                name=None if name is None else str(name),
                caption=None if caption is None else str(caption),
                size_bytes=int(size_bytes),
                too_large=bool(too_large),
            )
        )
    settled: dict[int, tuple[str | None, str | None]] = {}
    for hop, handler, _route, result, reason in conn.execute(
        _SELECT_HOP_DECISIONS, (run_id, message_seq)
    ):
        settled[int(hop)] = (
            None if result is None else str(result),
            RESULT_BEFORE_018 if result is None else (None if reason is None else str(reason)),
        )
        # An agent hop is always a hop; any other hop is one only when its handler failed.
        if handler == "agent" or result == "failed":
            grouped.setdefault(int(hop), [])
    compared = {
        int(hop): None if word is None else str(word)
        for hop, word in conn.execute(_SELECT_HOP_COMPARED, (run_id, message_seq))
    }
    # An uncertain stop runs nothing, but it is a hop that compared a figure with a floor.
    for hop, word in compared.items():
        if word is not None:
            grouped.setdefault(hop, [])
    hashes = {
        int(hop): str(task_hash)
        for hop, task_hash in conn.execute(_SELECT_HOP_HASH, (run_id, message_seq))
    }
    records = {hop: _repository_for(conn, task_hash) for hop, task_hash in hashes.items()}
    return tuple(
        Hop(
            hop=hop,
            outputs=tuple(outputs),
            result=settled.get(hop, (None, RESULT_BEFORE_018))[0],
            reason=settled.get(hop, (None, RESULT_BEFORE_018))[1],
            repository=records.get(hop),
            repository_reason=(
                None
                if records.get(hop)
                else (REPOSITORY_NONE if hop in hashes else REPOSITORY_NO_DELEGATION)
            ),
            compared=compared.get(hop),
            compared_reason=COMPARED_REASON if compared.get(hop) is None else None,
        )
        for hop, outputs in sorted(grouped.items())
    )


def _repository_for(conn: sqlite3.Connection, task_hash: str) -> dict[str, str | list[str]] | None:
    """The latest attempt's repository record for one delegation, every scalar a string."""
    row = conn.execute(_SELECT_REPO_RECORD, (task_hash,)).fetchone()
    if row is None:
        return None
    found = conn.execute(_SELECT_REPO_PATHS, (task_hash, int(row[0]))).fetchall()
    paths = [str(path) for path, kind in found if kind == "changed"]
    dirty = [str(path) for path, kind in found if kind == "dirty"]
    return {
        "head_before": str(row[1]),
        "head_after": str(row[2]),
        "head_before_short": str(row[3]),
        "head_after_short": str(row[4]),
        "branch": str(row[5]),
        "diff_stat": str(row[6]),
        "changed_count": str(row[7]),
        "committed": "true" if row[1] != row[2] else "false",
        "changed_paths": paths,
        "dirty_paths": dirty,
        "started_dirty": "true" if row[8] else "false",
    }


def _steps_for(conn: sqlite3.Connection, run_id: str, message_seq: int) -> int:
    (turns,) = conn.execute(_COUNT_TURNS, (run_id, message_seq)).fetchone()
    (agent_turns,) = conn.execute(_COUNT_AGENT_TURNS, (run_id, message_seq)).fetchone()
    return int(turns) + int(agent_turns)


def _elapsed(start_ts: str, finish_ts: str) -> float:
    return (clock.parse(finish_ts) - clock.parse(start_ts)).total_seconds()


def _one_decimal(seconds: float) -> str:
    return f"{round(seconds, 1):.1f}"


def _seconds_between(start_ts: str, finish_ts: str | None) -> str | None:
    if finish_ts is None:
        return None
    return _one_decimal(_elapsed(start_ts, finish_ts))


def _tokens(value: object) -> int:
    return 0 if value is None else int(str(value))


def _rounded(amount: Decimal | None) -> Decimal | None:
    return None if amount is None else amount.quantize(_SIX_PLACES, ROUND_HALF_UP)


def _dollars(nanos: object) -> Decimal | None:
    return None if nanos is None else Decimal(int(str(nanos))) / _PER_BILLION


def _handlers_for(
    conn: sqlite3.Connection, run_id: str, message_seq: int
) -> tuple[HandlerRow, ...]:
    """This message's handler rows, the same shape decision 64 serves, scoped to it alone."""
    return tuple(row for row, _exact in _handlers_exact(conn, run_id, message_seq))


def exact_nanos_for(conn: sqlite3.Connection, run_id: str, message_seq: int) -> int:
    """This message's dollars in whole nanodollars, summed from the handlers' unrounded costs and
    rounded once, half-up (MUST rule 29), so a task adds what the session total adds (ticket 282).
    """
    total = sum(
        (exact for _row, exact in _handlers_exact(conn, run_id, message_seq) if exact is not None),
        Decimal(0),
    )
    return int((total * _PER_BILLION).quantize(Decimal(1), ROUND_HALF_UP))


def _handlers_exact(
    conn: sqlite3.Connection, run_id: str, message_seq: int
) -> list[tuple[HandlerRow, Decimal | None]]:
    """Each handler row beside the exact dollars it was rounded from."""
    (newest,) = conn.execute(_NEWEST_PRICE).fetchone()
    handlers: list[tuple[HandlerRow, Decimal | None]] = []
    for row in conn.execute(_SELECT_SPEND_FOR_MESSAGE, (run_id, message_seq)):
        (
            provider,
            model,
            held,
            no_usage,
            cached_turns,
            uncached,
            outputs_sum,
            cached,
            bare_inputs,
            bare_outputs,
            input_rate,
            output_rate,
            cached_rate,
        ) = row
        held = int(held)
        known = held - int(no_usage)
        cost: Decimal | None = None
        exact: Decimal | None = None
        priced_turns = 0
        if known > 0 and provider is not None and model is not None and input_rate is not None:
            if cached_rate is None and int(cached_turns) > 0:
                priced_turns = known - int(cached_turns)
                amount = _tokens(bare_inputs) * Decimal(str(input_rate)) + _tokens(
                    bare_outputs
                ) * Decimal(str(output_rate))
            else:
                priced_turns = known
                amount = _tokens(uncached) * Decimal(str(input_rate)) + _tokens(
                    outputs_sum
                ) * Decimal(str(output_rate))
                if _tokens(cached) > 0:
                    amount += _tokens(cached) * Decimal(str(cached_rate))
            exact = amount / _PER_MILLION
            cost = _rounded(exact)
        handlers.append(
            (
                HandlerRow(
                    handler=str(provider),
                    kind=HANDLER_CHAT_MODEL,
                    billing=None,
                    turns=held,
                    cost=cost,
                    api_equivalent=None,
                    not_metered_turns=held - priced_turns,
                    reason=None
                    if cost is not None
                    else (NO_PRICES_REASON if newest is None else NONE_PRICED_REASON),
                ),
                exact,
            )
        )
    decisions_count, router_nanos, router_not_metered = conn.execute(
        _SELECT_ROUTER_FOR_MESSAGE, (run_id, message_seq)
    ).fetchone()
    if int(decisions_count) > 0:
        router_exact = _dollars(router_nanos)
        router_cost = _rounded(router_exact)
        handlers.append(
            (
                HandlerRow(
                    handler="router",
                    kind=HANDLER_ROUTER,
                    billing=None,
                    turns=int(decisions_count),
                    cost=router_cost,
                    api_equivalent=None,
                    not_metered_turns=int(router_not_metered),
                    reason=None if router_cost is not None else NO_ROUTER_COST_REASON,
                ),
                router_exact,
            )
        )
    for (
        entry,
        billing,
        held_turns,
        reported_nanos,
        api_nanos,
        entry_not_metered,
        _idx,
    ) in conn.execute(_SELECT_AGENT_ENTRIES_FOR_MESSAGE, (run_id, message_seq)):
        entry_exact = _dollars(reported_nanos)
        entry_cost = _rounded(entry_exact)
        entry_api_equivalent = _rounded(_dollars(api_nanos))
        handlers.append(
            (
                HandlerRow(
                    handler=str(entry),
                    kind=HANDLER_AGENT,
                    billing=None if billing is None else str(billing),
                    turns=int(held_turns),
                    cost=entry_cost,
                    api_equivalent=entry_api_equivalent,
                    not_metered_turns=int(entry_not_metered),
                    reason=(
                        None
                        if entry_cost is not None or entry_api_equivalent is not None
                        else NO_AGENT_COST_REASON
                    ),
                ),
                entry_exact,
            )
        )
    return handlers


def _message_row(
    conn: sqlite3.Connection,
    run_id: str,
    start: events_read.ReplayedEvent,
    finish: events_read.ReplayedEvent | None,
) -> MessageRow:
    message_seq = start.seq
    text, text_reason = _text_for(conn, run_id, message_seq)
    handler, outcome = _last_decision(conn, run_id, message_seq)
    hops = _hops_for(conn, run_id, message_seq)
    if any(hop.result == "failed" for hop in hops):
        outcome = OUTCOME_FAILED
    reply, reply_reason = _reply_for(conn, run_id, message_seq, hops)
    task_message = _task_message_for(conn, run_id, message_seq)
    route, route_reason = _route_for(conn, run_id, start)
    return MessageRow(
        message_seq=message_seq,
        first_seq=start.seq,
        last_seq=None if finish is None else finish.seq,
        text=text,
        text_reason=text_reason,
        handler=handler,
        outcome=outcome,
        reply=reply,
        reply_reason=reply_reason,
        hops=hops,
        steps=_steps_for(conn, run_id, message_seq),
        seconds=_seconds_between(start.ts, None if finish is None else finish.ts),
        handlers=_handlers_for(conn, run_id, message_seq),
        created_at=start.ts,
        author=AUTHOR_OPERATOR if task_message is None else AUTHOR_TASK,
        task_message=task_message,
        route=route,
        route_reason=route_reason,
    )


def for_session(conn: sqlite3.Connection, session_id: str) -> tuple[MessageRow, ...]:
    """Every message under ``session_id``, in order, each a ``run.started``/``run.finished`` pair.

    Raises :class:`~harnessed.errors.SurfaceNotFound` when the events table never held
    ``session_id`` (``events/read.py::replay``'s own rule). A session that exists but never
    routed a message - an eval or an improve session, whose stream never publishes ``run.started``
    - reads back as ``()``, never a 404: "nothing happened" and "no such session" are different
    claims (ticket 076's rule, carried here as it is in ``router/decisions.py::for_session``).
    """
    events = events_read.replay(conn, session_id)
    return tuple(
        _message_row(conn, session_id, start, finish)
        for start, finish in events_read.run_pairs(events)
    )


def task_for(conn: sqlite3.Connection, session_id: str) -> dict[str, str | None] | None:
    """The task a session belongs to as ``{task_id, text, state, unattended}``, or ``None``.

    ``unattended`` is ``"true"`` or ``"false"`` once the task is ``done`` and ``None`` before
    (G-37, G-63).
    """
    found = conn.execute(_SELECT_TASK, (session_id,)).fetchone()
    if found is None:
        return None
    task_id, text, state, unattended = found
    return {
        "task_id": str(task_id),
        "text": str(text),
        "state": str(state),
        "unattended": None if unattended is None else ("true" if int(unattended) else "false"),
    }


def session_payload(conn: sqlite3.Connection, session_id: str) -> dict[str, object]:
    """:func:`for_session` and :func:`task_for` as the one wire body ``GET
    /v1/sessions/{id}/messages`` and ``harnessed sessions messages --json`` both serve."""
    return messages_payload(for_session(conn, session_id), task_for(conn, session_id))


def messages_payload(
    rows: tuple[MessageRow, ...], task: Mapping[str, str | None] | None = None
) -> dict[str, object]:
    """The one wire shape for :func:`for_session`, rendered by the CLI and the gateway alike.

    ``task`` is :func:`task_for`'s answer, ``None`` when the session is not a task's, and it
    rides the payload once.

    Built by hand rather than a blanket ``dataclasses.asdict`` (MUST rule 16's shape, not its
    letter): a handler row's ``cost``/``api_equivalent`` are ``Decimal``, and only
    ``loop/runs.py::handler_payload`` knows how to cross one as the wire's money string.
    """
    return {
        "messages": [_message_payload(row) for row in rows],
        "task": None if task is None else dict(task),
    }


def _message_payload(message: MessageRow) -> dict[str, object]:
    return {
        "message_seq": message.message_seq,
        "first_seq": message.first_seq,
        "last_seq": message.last_seq,
        "text": message.text,
        "text_reason": message.text_reason,
        "handler": message.handler,
        "outcome": message.outcome,
        "reply": None if message.reply is None else dataclasses.asdict(message.reply),
        "reply_reason": message.reply_reason,
        "hops": [dataclasses.asdict(hop) for hop in message.hops],
        "steps": message.steps,
        "seconds": message.seconds,
        "handlers": [handler_payload(row) for row in message.handlers],
        "created_at": message.created_at,
        "author": message.author,
        "task_message": message.task_message,
        "route": message.route,
        "route_reason": message.route_reason,
    }


def recent(
    conn: sqlite3.Connection, limit: int = DEFAULT_MESSAGE_LIMIT
) -> tuple[MessageSummary, ...]:
    """At most ``limit`` messages, newest first over hop-0 decisions alone (decision 66).

    A message that never routed - recorded before the router, or a session with no decisions row
    at all - never appears: the activity list reads messages, not sessions.
    """
    return tuple(
        MessageSummary(
            session_id=str(run_id),
            message_seq=int(message_seq),
            text=None if task is None else str(task),
            handler=None if handler is None else str(handler),
            outcome=str(outcome),
            cost=None if cost is None else str(cost),
            created_at=str(created_at),
            author=str(author),
            task_id=None if task_id is None else str(task_id),
        )
        for (
            run_id,
            message_seq,
            task,
            handler,
            outcome,
            cost,
            created_at,
            author,
            task_id,
        ) in conn.execute(_SELECT_RECENT, (limit,))
    )


def recent_payload(rows: tuple[MessageSummary, ...]) -> dict[str, object]:
    """The one wire shape for :func:`recent`, printed by the CLI and served by the gateway."""
    return {"messages": [dataclasses.asdict(row) for row in rows]}
