"""The run journal: the first of the four ledgers, and what makes a run resumable (FR-7).

One row in ``turns`` per model turn, one row in ``tool_results`` per tool outcome, keyed
``(run_id, iteration)`` and ``(run_id, iteration, call_id)``. Those keys are the done-state keys,
and the pattern around them is the one every ledger in this project uses, copied rather than
reinvented (`state-and-idempotency.md`)::

    read the done-state row for this step
      present  -> return its recorded payload, do no work
      absent   -> do the work, then persist the row BEFORE moving to the next step

Read from the loop's side that is: ask :meth:`Journal.recorded` before the model is called, and
call :meth:`Journal.record` before the next pass begins. A run killed between two turns therefore
re-reads what it already did instead of re-doing it, which for a tool with a side effect is the
difference between "the file was written once" and "the file was written twice".

**Contiguity is the resume rule.** A turn is done-state only when every turn before it is on disk
too, so a journal truncated at an arbitrary point resumes at the first hole rather than skipping
over it into stale rows. That is one ``COUNT(*)`` per read and it is what makes "tolerate a
half-finished ledger" a property of the query instead of a promise in a docstring.

**Two things this module deliberately cannot do.** It cannot decode JSON - the decoder is
quarantined to ``providers/translate.py`` and ``loop/`` may not import it - so a replayed turn is
rebuilt from typed columns, never from the serialized transcript: the words the model said, the
identity of each call it made, and each call's recorded outcome. What the columns do not hold, a
replay does not invent: the provider's own response id, the model name, and the arguments of an
already-executed call stay on the row for the dashboard and the audit trail. And it cannot spell
the malformed-arguments marker, whose two names belong to ``providers/translate.py`` and
``tools/registry.py`` alone (architecture open question 8). Serializing a call is therefore an
injected function - ``translate.tool_call_payload`` is the one that owns the glossary spelling -
and the marker reaches its own column read positionally off the frozen shape, never by name.

**Written for the reader, never read back - with one sanctioned exception** (FR-80, decision
50). ``turns.request`` stays a scrubbed human transcript nothing reads. A session resume reads
``run_messages`` instead, through :func:`messages_for` and nothing else: the run's message list,
one row per message, written by :meth:`Journal.record` in the same transaction as the turn that
added it. Every ``[redacted:<kind>]`` marker in that list travels into the resumed context **as
written** - the safe direction, since the secret it replaced was never stored. Decoding a row is
an injected function for the same reason serializing a call is: ``translate.journal_message``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import astuple, dataclass
from datetime import datetime
from typing import Final, Protocol

from harnessed import clock
from harnessed.memory import db, redact
from harnessed.providers.types import (
    STOP_REASONS,
    ChatRequest,
    ChatResponse,
    Message,
    StopReason,
    ToolCall,
    ToolResult,
    Usage,
)

__all__ = [
    "NO_JOURNAL",
    "CallSerializer",
    "Journal",
    "Ledger",
    "NullJournal",
    "MessageDecoder",
    "RecordedTurn",
    "last_iteration",
    "messages_for",
    "record_exchange",
    "turn_usage",
]

_TURN_COLUMNS: Final = (
    "run_id, iteration, request, response, stop_reason, created_at, "
    "input_tokens, output_tokens, cached_input_tokens, requested_at, provider, model, message_seq"
)
_RESULT_COLUMNS: Final = (
    "run_id, iteration, call_id, tool_name, arguments, output, ok, parse_error, created_at"
)

_INSERT_TURN: Final = (
    f"INSERT INTO turns ({_TURN_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_RESULT: Final = (
    f"INSERT INTO tool_results ({_RESULT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SELECT_TURN: Final = (
    "SELECT response, stop_reason, input_tokens, output_tokens, cached_input_tokens "
    "FROM turns WHERE run_id = ? AND iteration = ?"
)
_SELECT_USAGE: Final = (
    "SELECT iteration, input_tokens, output_tokens, cached_input_tokens "
    "FROM turns WHERE run_id = ? ORDER BY iteration"
)
_SELECT_RESULTS: Final = (
    "SELECT call_id, tool_name, output, ok FROM tool_results "
    "WHERE run_id = ? AND iteration = ? ORDER BY rowid"
)
_COUNT_THROUGH: Final = "SELECT COUNT(*) FROM turns WHERE run_id = ? AND iteration <= ?"
_LAST_ITERATION: Final = "SELECT COALESCE(MAX(iteration), 0) FROM turns WHERE run_id = ?"
_INSERT_MESSAGE: Final = (
    "INSERT INTO run_messages "
    "(run_id, idx, role, content, tool_calls, tool_results, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_COUNT_MESSAGES: Final = "SELECT COUNT(*) FROM run_messages WHERE run_id = ?"
_SELECT_MESSAGES: Final = (
    "SELECT role, content, tool_calls, tool_results FROM run_messages "
    "WHERE run_id = ? ORDER BY idx"
)

#: The recorded word back to the vocabulary it came from. A row written by an older build that
#: spells something this version does not know lands on ``other`` rather than leaking upward.
_STOP_BY_NAME: Final[dict[str, StopReason]] = {reason: reason for reason in STOP_REASONS}


class CallSerializer(Protocol):
    """Turns one tool call into a JSON-shaped payload for the transcript the journal stores.

    Injected rather than imported: ``loop/`` may reach ``providers.types`` and nothing else under
    ``providers/``, and the one function that knows how the malformed-arguments marker is spelled
    on the wire lives in ``providers/translate.py``. The composition root hands it in, exactly as
    ticket 010 hands the sandbox root to ``default_registry``.
    """

    def __call__(self, call: ToolCall) -> dict[str, object]: ...


class MessageDecoder(Protocol):
    """Turns one ``run_messages`` row back into a :class:`Message`.

    Injected for the reason :class:`CallSerializer` is: the two JSON columns hold serialized
    calls and results, ``loop/`` may not decode JSON, and the one function that knows the parse
    marker's spelling is ``translate.journal_message``.
    """

    def __call__(
        self, role: str, content: str, tool_calls: str | None, tool_results: str | None
    ) -> Message: ...


@dataclass(frozen=True, slots=True)
class RecordedTurn:
    """A turn already on disk: the done-state row, rebuilt into the shapes the loop speaks."""

    iteration: int
    response: ChatResponse
    results: tuple[ToolResult, ...]


class Ledger(Protocol):
    """What the loop asks of a journal: has this turn happened, and remember that it has."""

    def recorded(self, iteration: int) -> RecordedTurn | None: ...

    def record(
        self,
        iteration: int,
        request: ChatRequest,
        response: ChatResponse,
        results: Sequence[ToolResult],
        *,
        requested_at: datetime | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class NullJournal:
    """The seam with nothing behind it: every turn is a miss and every write is dropped.

    This is the default so ``run_loop`` has no ``if journal is not None`` in it. A run without a
    database is a run whose journal never recognises anything, which is precisely a run that
    starts from the beginning every time.
    """

    def recorded(self, iteration: int) -> RecordedTurn | None:
        return None

    def record(
        self,
        iteration: int,
        request: ChatRequest,
        response: ChatResponse,
        results: Sequence[ToolResult],
        *,
        requested_at: datetime | None = None,
    ) -> None:
        return None


#: The default journal ``run_loop`` uses when the caller opened no database.
NO_JOURNAL: Final = NullJournal()


@dataclass(frozen=True, slots=True)
class Journal:
    """The run journal for one run, against one connection.

    ``serialize_call`` is the injected :class:`CallSerializer`; ``run_id`` is the ledger key and
    is stored as it was handed over, because scrubbing a key would make the row unfindable by it.

    ``provider`` is the configured provider's name, written on every turn beside the request's
    model id (FR-72, migration 010). It has no default on purpose: a turn recorded from 010 on
    with a ``NULL`` provider would read as a pre-migration row, and a default name would put a
    false one on it. The loop never sees it; the composition root that built the provider does.

    ``message_seq`` is the message's own ``run.started`` ``seq`` (FR-108, migration 014): ``None``
    for a run that never routed - an eval or an improve run - which is also what a turn recorded
    before this migration reads back as.
    """

    conn: sqlite3.Connection
    run_id: str
    serialize_call: CallSerializer
    provider: str
    message_seq: int | None = None

    def recorded(self, iteration: int) -> RecordedTurn | None:
        """The recorded turn, or ``None`` when there is work to do. A miss is an answer.

        ``None`` also comes back for a row that sits after a hole: done-state means every turn
        up to this one is on disk, so a truncated journal resumes at the gap.
        """
        row = self.conn.execute(_SELECT_TURN, (self.run_id, iteration)).fetchone()
        if row is None or not self._contiguous_through(iteration):
            return None
        calls, results = self._recorded_calls(iteration)
        response = ChatResponse(
            response_id="",
            model="",
            content=str(row[0]),
            tool_calls=calls,
            stop_reason=_STOP_BY_NAME.get(str(row[1]), "other"),
            usage=_usage(row[2:]) or Usage(),
        )
        return RecordedTurn(iteration=iteration, response=response, results=results)

    def record(
        self,
        iteration: int,
        request: ChatRequest,
        response: ChatResponse,
        results: Sequence[ToolResult],
        *,
        requested_at: datetime | None = None,
    ) -> None:
        """Persist one whole turn - the row and every tool row - before the next step begins.

        Idempotent by the pattern rather than by the database: a turn already recorded is read
        first and does no second write, so a resumed run replays its ledger instead of appending
        to it - and keeps the ``requested_at`` it first wrote. Every stored string is scrubbed on
        the way in (FR-13), and the whole turn is one short transaction so the dashboard can read
        while the loop writes. ``requested_at`` is when the request was sent; a caller that did
        not say leaves it ``NULL``, which readers treat as a pre-007 row. The model recorded is
        the one the request named, not the one the reply echoed: it is the id a price is keyed on.

        The messages this turn added to the run's list join the same write (FR-80): the request's
        messages not yet stored, the assistant's reply, and the tool batch when it asked for
        tools. A crash therefore leaves a list that matches the journal exactly.
        """
        if self.recorded(iteration) is not None:
            return
        at = clock.isoformat(clock.now())
        turn = (
            _INSERT_TURN,
            (
                self.run_id,
                iteration,
                redact.scrub(self._transcript(request)),
                redact.scrub(response.content),
                response.stop_reason,
                at,
                response.usage.input_tokens,
                response.usage.output_tokens,
                response.usage.cached_input_tokens,
                None if requested_at is None else clock.isoformat(requested_at),
                self.provider,
                request.model,
                self.message_seq,
            ),
        )
        outcomes = {result.call_id: result for result in results}
        rows = tuple(
            self._result_row(iteration, call, outcomes[call.id], at)
            for call in response.tool_calls
            if call.id in outcomes
        )
        db.write(self.conn, (turn, *rows, *self._message_rows(request, response, results, at)))

    def turn_usage(self) -> dict[int, Usage | None]:
        """Each recorded turn's token usage, read from the ledger rather than recomputed.

        ``None`` marks a turn recorded before migration 006: its cost is unknown, not zero. One
        row per iteration is the primary key, so a sum over this is a resume-safe total.
        """
        return turn_usage(self.conn, self.run_id)

    # ---- internals -------------------------------------------------------------------------

    def _contiguous_through(self, iteration: int) -> bool:
        """Whether turns 1..``iteration`` are all on disk. Anything less is a truncated journal."""
        row = self.conn.execute(_COUNT_THROUGH, (self.run_id, iteration)).fetchone()
        return bool(row is not None and int(row[0]) == iteration)

    def _recorded_calls(
        self, iteration: int
    ) -> tuple[tuple[ToolCall, ...], tuple[ToolResult, ...]]:
        """The calls and outcomes of one recorded turn, rebuilt from the typed columns."""
        calls: list[ToolCall] = []
        results: list[ToolResult] = []
        for call_id, tool_name, output, ok in self.conn.execute(
            _SELECT_RESULTS, (self.run_id, iteration)
        ):
            calls.append(ToolCall(id=str(call_id), name=str(tool_name)))
            results.append(
                ToolResult(call_id=str(call_id), content=str(output), is_error=not bool(ok))
            )
        return tuple(calls), tuple(results)

    def _result_row(
        self, iteration: int, call: ToolCall, outcome: ToolResult, at: str
    ) -> tuple[str, tuple[object, ...]]:
        """One ``tool_results`` row: what was asked for, and what came back.

        The four fields of the frozen call are read positionally, in declaration order, because
        this package may not spell the last of them (`loop-readability.md`). ``providers/types.py``
        fixes that order and being frozen is what keeps it fixed.
        """
        identity, tool_name, arguments, marker = astuple(call)
        return (
            _INSERT_RESULT,
            (
                self.run_id,
                iteration,
                str(identity),
                redact.scrub(str(tool_name)),
                redact.scrub(_dumps(arguments)),
                redact.scrub(outcome.content),
                int(not outcome.is_error),
                None if marker is None else redact.scrub(str(marker)),
                at,
            ),
        )

    def _message_rows(
        self,
        request: ChatRequest,
        response: ChatResponse,
        results: Sequence[ToolResult],
        at: str,
    ) -> tuple[tuple[str, tuple[object, ...]], ...]:
        """The ``run_messages`` rows this turn adds: the list as the loop now holds it, past what
        is already stored. The loop appends the tool batch exactly when the reply asked for tools.
        """
        carried = [
            *request.messages,
            Message(role="assistant", content=response.content, tool_calls=response.tool_calls),
        ]
        if response.tool_calls:
            carried.append(Message(role="tool", tool_results=tuple(results)))
        (stored,) = self.conn.execute(_COUNT_MESSAGES, (self.run_id,)).fetchone()
        return tuple(
            (_INSERT_MESSAGE, (self.run_id, idx, *self._message_columns(message), at))
            for idx, message in enumerate(carried)
            if idx >= int(stored)
        )

    def _message_columns(self, message: Message) -> tuple[str, str, str | None, str | None]:
        """One message as ``role, content, tool_calls, tool_results``, scrubbed; none is NULL."""
        payload = self._message(message)
        calls, outcomes = payload["tool_calls"], payload["tool_results"]
        return (
            message.role,
            redact.scrub(message.content),
            redact.scrub(_dumps(calls)) if calls else None,
            redact.scrub(_dumps(outcomes)) if outcomes else None,
        )

    def _transcript(self, request: ChatRequest) -> str:
        """The request as sent, as one JSON string. Written for the reader, never read back."""
        return _dumps(
            {
                "model": request.model,
                "system": request.system,
                "messages": [self._message(message) for message in request.messages],
                "tools": [tool.name for tool in request.tools],
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "metadata": dict(request.metadata),
            }
        )

    def _message(self, message: Message) -> dict[str, object]:
        """One message of the transcript. Tool calls go through the injected serializer."""
        return {
            "role": message.role,
            "content": message.content,
            "tool_calls": [dict(self.serialize_call(call)) for call in message.tool_calls],
            "tool_results": [
                {"call_id": result.call_id, "content": result.content, "is_error": result.is_error}
                for result in message.tool_results
            ],
        }


def messages_for(
    conn: sqlite3.Connection, run_key: str, *, decode: MessageDecoder
) -> tuple[Message, ...]:
    """The run's stored message list, in order: the journal's one sanctioned reader (FR-80).

    Read by session resume and by nothing else. What comes back is what was written, scrubbed:
    a ``[redacted:<kind>]`` marker is carried into the resumed context as written, and the key it
    replaced is not on disk to be carried. An unknown key reads an empty list.
    """
    return tuple(
        decode(str(role), str(content), tool_calls, tool_results)
        for role, content, tool_calls, tool_results in conn.execute(_SELECT_MESSAGES, (run_key,))
    )


def record_exchange(conn: sqlite3.Connection, run_id: str, prompt: str, reply: str) -> None:
    """Append one ``user`` row and one ``assistant`` row to ``run_messages`` for a message an
    agent route answered directly (FR-106, ticket 167): no turn ran, so there is no
    :meth:`Journal.record` call to carry them, and this is the one other writer of that table.

    Keyed after whatever the run already holds, the same way :meth:`Journal.record`'s own
    ``_message_rows`` picks up after ``stored`` messages - so a follow-up on the same run resumes
    with this exchange through :func:`messages_for`, exactly as it would a loop turn's.
    """
    (stored,) = conn.execute(_COUNT_MESSAGES, (run_id,)).fetchone()
    at = clock.isoformat(clock.now())
    rows = tuple(
        (_INSERT_MESSAGE, (run_id, idx, role, redact.scrub(content), None, None, at))
        for idx, (role, content) in enumerate(
            (("user", prompt), ("assistant", reply)), start=int(stored)
        )
    )
    db.write(conn, rows)


def last_iteration(conn: sqlite3.Connection, run_key: str) -> int:
    """The highest iteration journaled under ``run_key``; ``0`` when there is none (decision 52).

    A follow-up's loop starts here, so its turns are keyed after the ones already on disk and
    can never replay them.
    """
    return int(conn.execute(_LAST_ITERATION, (run_key,)).fetchone()[0])


def turn_usage(conn: sqlite3.Connection, run_id: str) -> dict[int, Usage | None]:
    """:meth:`Journal.turn_usage` for a reader holding a key but no serializer (``runs.py``)."""
    return {int(row[0]): _usage(row[1:]) for row in conn.execute(_SELECT_USAGE, (run_id,))}


def _usage(counts: Sequence[object]) -> Usage | None:
    """Three stored counts back to a :class:`Usage`; ``None`` when a pre-006 row left any NULL."""
    if any(count is None for count in counts):
        return None
    inputs, outputs, cached = (int(str(count)) for count in counts)
    return Usage(input_tokens=inputs, output_tokens=outputs, cached_input_tokens=cached)


def _dumps(payload: object) -> str:
    """Canonical JSON, the format rule's spelling. Serializing is all this module does with it."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
