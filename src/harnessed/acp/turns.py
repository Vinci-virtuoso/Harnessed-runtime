"""The ``agent_turns`` table: what an agent did, turn by turn, beside the loop's ``turns`` (FR-112).

**A sibling, never rows in ``turns``** (decision 63). ``turns`` is keyed on the loop's iteration and
replayed by ``journal.recorded()``; an agent turn written there would replay as a loop turn, move
``last_iteration`` and be rate-priced by spend's ``prices`` join. This module owns the table, as
``acp/ledger.py`` owns ``delegations``.

**A turn is one prompt's worth of an ACP transcript**: its message chunks, its tool calls and its
``usage_update`` frames. A ``usage_update`` carrying ``cost`` closes a turn - an agent reports it
once the prompt is answered - and whatever follows the last one is a turn of its own when it holds
anything. An agent that sends no ``usage_update`` at all (Grok, 2 of 2 recorded sessions) yields one
turn for the whole prompt.

**Billing decides how a turn is metered, and nothing here computes a figure.** A subscription
entry's (``config.BILLING_SUBSCRIPTION``) ``usage_update.cost`` is stored as the agent reported
it, as an ``api_equivalent`` cost: what the same tokens would have cost over the API, not what the
subscription charged. Any other entry's
transcript turns are not metered - a null cost and a null ``cost_kind``, never ``0``. The model is
read from ``usage_update._meta`` where the agent sends one, and is null otherwise; nothing guesses.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

from harnessed import clock
from harnessed.acp.config import BILLING_SUBSCRIPTION
from harnessed.memory import db, redact

__all__ = ["COST_KINDS", "SOURCES", "AgentTurn", "from_transcript", "record", "settle_cost"]

#: Where an agent turn came from: an ACP transcript, the metering proxy (FR-113), or a ``cli`` run.
SOURCES: Final[frozenset[str]] = frozenset({"acp", "proxy", "cli"})
#: ``reported`` is a bill (``openrouter``); ``api_equivalent`` is a subscription agent's own figure.
COST_KINDS: Final[frozenset[str]] = frozenset({"reported", "api_equivalent"})

#: The kind a subscription entry's transcript cost is kept as (decision 63).
_API_EQUIVALENT: Final = "api_equivalent"

_USAGE: Final = "usage_update"
_MESSAGE: Final = "agent_message_chunk"
_TOOL_CALL: Final = "tool_call"
_TOOL_UPDATE: Final = "tool_call_update"

#: Nanodollars per dollar - the unit a sum over ``cost_nanos`` is in. Restated rather than imported
#: from ``router/decisions.py``: ``acp/`` may not import ``router/`` (the import table).
_NANOS_PER_DOLLAR: Final = Decimal("1000000000")
_WHOLE: Final = Decimal("1")

_INSERT: Final = (
    "INSERT INTO agent_turns ("
    "run_id, idx, message_seq, hop, task_hash, entry, billing, source, model, request, response, "
    "tool_calls, input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, cost, "
    "cost_nanos, currency, cost_kind, generation_id, complete, created_at"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SETTLE: Final = (
    "UPDATE agent_turns SET cost = ?, cost_nanos = ?, currency = ?, cost_kind = 'reported' "
    "WHERE run_id = ? AND generation_id = ? AND cost IS NULL"
)
_NEXT_IDX: Final = "SELECT COALESCE(MAX(idx), -1) + 1 FROM agent_turns WHERE run_id = ?"


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """One agent turn, ready to write. Token counts are null where the source never sent them."""

    request: str | None
    response: str
    tool_calls: tuple[dict[str, str], ...]
    model: str | None
    cost: Decimal | None
    currency: str | None
    cost_kind: str | None
    complete: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    generation_id: str | None = None


def from_transcript(
    transcript: Sequence[Mapping[str, Any]],
    *,
    billing: str | None,
    request: str | None,
    complete: bool,
) -> tuple[AgentTurn, ...]:
    """Every turn in one delegation's ACP transcript, oldest first.

    ``request`` is the task the delegation was handed; it belongs to the first turn only.
    ``complete`` is whether the delegation finished: a timed-out one's last turn is partial.
    """
    segments = list(_segments(transcript))
    return tuple(
        _turn(
            segment,
            billing=billing,
            request=request if position == 0 else None,
            complete=complete or position < len(segments) - 1,
        )
        for position, segment in enumerate(segments)
    )


def record(
    conn: sqlite3.Connection,
    run_id: str,
    turns: Sequence[AgentTurn],
    *,
    entry: str,
    billing: str | None,
    source: str,
    message_seq: int | None,
    hop: int,
    task_hash: str | None,
) -> None:
    """Append ``turns`` to ``run_id``'s agent turns in one transaction, after any it holds."""
    if source not in SOURCES:
        raise ValueError(f"an agent turn's source is one of {sorted(SOURCES)}, not {source!r}")
    if not turns:
        return
    first = int(conn.execute(_NEXT_IDX, (run_id,)).fetchone()[0])
    at = clock.isoformat(clock.now())
    db.write(
        conn,
        tuple(
            (
                _INSERT,
                (
                    run_id,
                    idx,
                    message_seq,
                    hop,
                    task_hash,
                    entry,
                    billing,
                    source,
                    turn.model,
                    None if turn.request is None else redact.scrub(turn.request),
                    redact.scrub(turn.response),
                    _tool_calls(turn.tool_calls),
                    turn.input_tokens,
                    turn.output_tokens,
                    turn.cached_input_tokens,
                    turn.reasoning_tokens,
                    None if turn.cost is None else str(turn.cost),
                    None if turn.cost is None else _to_nanos(turn.cost),
                    turn.currency,
                    turn.cost_kind,
                    turn.generation_id,
                    int(turn.complete),
                    at,
                ),
            )
            for idx, turn in enumerate(turns, start=first)
        ),
    )


def settle_cost(
    conn: sqlite3.Connection, run_id: str, generation_id: str, cost: Decimal, currency: str
) -> None:
    """Set a ``reported`` cost on ``run_id``'s turn for ``generation_id``, only where none is yet.

    The generation lookup's write (FR-114): a turn some other path already priced keeps its figure.
    """
    db.write(conn, ((_SETTLE, (str(cost), _to_nanos(cost), currency, run_id, generation_id)),))


# ---- reading a transcript ---------------------------------------------------------------------


def _segments(transcript: Sequence[Mapping[str, Any]]) -> Iterator[list[Mapping[str, Any]]]:
    """The transcript cut after each cost-bearing ``usage_update``; empty tails are dropped."""
    current: list[Mapping[str, Any]] = []
    for frame in transcript:
        update = frame.get("sessionUpdate")
        if update not in {_USAGE, _MESSAGE, _TOOL_CALL, _TOOL_UPDATE}:
            continue
        current.append(frame)
        if update == _USAGE and isinstance(frame.get("cost"), Mapping):
            yield current
            current = []
    if current:
        yield current


def _turn(
    frames: list[Mapping[str, Any]], *, billing: str | None, request: str | None, complete: bool
) -> AgentTurn:
    usages = [frame for frame in frames if frame.get("sessionUpdate") == _USAGE]
    cost, currency = _reported_cost(usages) if billing == BILLING_SUBSCRIPTION else (None, None)
    return AgentTurn(
        request=request,
        response="".join(_text(frame) for frame in frames),
        tool_calls=_calls(frames),
        model=_model(usages),
        cost=cost,
        currency=currency,
        cost_kind=None if cost is None else _API_EQUIVALENT,
        complete=complete,
    )


def _text(frame: Mapping[str, Any]) -> str:
    if frame.get("sessionUpdate") != _MESSAGE:
        return ""
    content = frame.get("content")
    if isinstance(content, Mapping) and content.get("type") == "text":
        return str(content.get("text", ""))
    return ""


def _calls(frames: list[Mapping[str, Any]]) -> tuple[dict[str, str], ...]:
    """Each tool call the turn started, carrying the last status any update gave it."""
    calls: dict[str, dict[str, str]] = {}
    for frame in frames:
        update = frame.get("sessionUpdate")
        call_id = str(frame.get("toolCallId") or "")
        if update == _TOOL_CALL:
            calls[call_id] = {
                "tool_call_id": call_id,
                "kind": str(frame.get("kind") or ""),
                "title": str(frame.get("title") or ""),
                "status": str(frame.get("status") or ""),
            }
        elif update == _TOOL_UPDATE and call_id in calls:
            if frame.get("status"):
                calls[call_id]["status"] = str(frame["status"])
            if frame.get("title"):
                calls[call_id]["title"] = str(frame["title"])
    return tuple(calls.values())


def _model(usages: list[Mapping[str, Any]]) -> str | None:
    """The last model any ``usage_update._meta`` named, or ``None`` when none was sent."""
    for frame in reversed(usages):
        meta = frame.get("_meta")
        if isinstance(meta, Mapping) and isinstance(meta.get("model"), str) and meta["model"]:
            return str(meta["model"])
    return None


def _reported_cost(usages: list[Mapping[str, Any]]) -> tuple[Decimal | None, str | None]:
    """The last ``usage_update.cost`` exactly as reported; ``(None, None)`` when none was sent."""
    for frame in reversed(usages):
        cost = frame.get("cost")
        if not isinstance(cost, Mapping):
            continue
        amount = cost.get("amount")
        currency = cost.get("currency")
        if isinstance(amount, bool) or not isinstance(amount, int | float | str):
            continue
        try:
            value = Decimal(str(amount))
        except InvalidOperation:
            continue
        if value.is_finite() and isinstance(currency, str) and currency:
            return value, currency
    return None, None


def _to_nanos(cost: Decimal) -> int:
    """``cost`` as integer nanodollars, half-up, for a sum that must never be a float."""
    return int((cost * _NANOS_PER_DOLLAR).quantize(_WHOLE, rounding=ROUND_HALF_UP))


def _tool_calls(calls: tuple[dict[str, str], ...]) -> str | None:
    """The calls as JSON text, scrubbed, or ``None`` on a turn that made none."""
    if not calls:
        return None
    return redact.scrub(json.dumps(list(calls), sort_keys=True, separators=(",", ":")))
