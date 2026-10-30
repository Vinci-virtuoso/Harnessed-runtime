"""One session id's events as a resumable SSE stream (FR-39).

The gateway never runs the loop. It tails the ``events`` table over WAL through the request's
own read-only connection, because ``EventBus`` lives in the CLI process and an ``Observer`` is a
synchronous callable there (decision 15). The table is the queue, which is why events published
while nobody was listening are delivered on reconnect.

The frame is hand-rolled and four lines long: ``id:`` is the ``seq``, ``event:`` is the name
verbatim from ``events/names.py``, ``data:`` is ``serialize.canonical(payload)`` on one line,
then a blank line (decision 20). New rows are detected by SQLite's ``data_version`` counter at
:data:`POLL_INTERVAL_S`, which on a read-only connection moves only when the CLI writer commits,
and an unchanged counter is a tick with no query (decision 16). The stream closes on the finish
``read.terminal_for`` pairs with the session's seq 1 start, read once per request, or when
:data:`IDLE_CEILING_S` elapses (decision 17). Never on a name alone: an improve session holds
its inner evals' ``eval.run.finished`` events and they do not end it (MUST rule 20).

This module formats frames and holds the high-water mark inside :func:`tail`; every row read is
``events/read.py``'s and the counter read is ``memory/db.py``'s (MUST rule 14). Nothing lives
at module level but the two constants (MUST rule 17). With ``acp/``, it is the one place under
``src/`` allowed to define a coroutine.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from typing import Final

from fastapi.responses import StreamingResponse

from harnessed.errors import CursorError
from harnessed.events import read
from harnessed.memory import db
from harnessed.serialize import canonical

__all__ = ["IDLE_CEILING_S", "POLL_INTERVAL_S", "frame", "parse_cursor", "stream", "tail"]

#: Seconds between two looks at the change counter (decision 16). Publish-to-delivery latency is
#: one tick plus one indexed read, well under the NFR's one second.
POLL_INTERVAL_S: Final = 0.1

#: Seconds a stream with no terminal event stays open before closing cleanly (decision 17), so a
#: crashed CLI cannot hold a generator forever. Read at call time, so a test can lower it.
IDLE_CEILING_S: float = 900.0


def frame(event: read.ReplayedEvent) -> bytes:
    """The four lines of one SSE frame, UTF-8: ``id``, ``event``, ``data``, blank."""
    return (
        f"id: {event.seq}\nevent: {event.name}\ndata: {canonical(dict(event.payload))}\n\n"
    ).encode()


def parse_cursor(raw: str | None) -> int:
    """``Last-Event-ID`` as the ``seq`` the client last saw; absent means the start.

    Only a run of ASCII digits is a cursor. Anything else, a sign, whitespace, a separator, a
    non-ASCII digit or an empty header, is a :class:`CursorError`, which the gateway maps to 400
    rather than quietly replaying from the start (decision 20).
    """
    if raw is None:
        return 0
    if not (raw.isascii() and raw.isdigit()):
        raise CursorError(
            f"Last-Event-ID {raw!r} is not a non-negative integer. Send the seq of the last "
            f"frame received, or no header at all to stream from the start."
        )
    return int(raw)


async def tail(
    conn: sqlite3.Connection,
    session_id: str,
    cursor: int,
    first: tuple[read.ReplayedEvent, ...],
    version: int,
    *,
    follow: bool,
    terminal: str | None,
) -> AsyncIterator[bytes]:
    """Yield ``first``, then every later row as it lands, until ``terminal`` or the ceiling.

    ``terminal`` is the session's own finish name from ``read.terminal_for``, or ``None`` for a
    session whose start pairs with nothing, which only the ceiling closes.

    ``cursor`` is the high-water mark and it lives here, in the generator, for the life of one
    response (MUST rule 17). ``version`` is the change counter as it stood *before* ``first``
    was read, so a commit that lands between that read and the first tick is one redundant
    read, never a lost frame. ``follow=False`` yields ``first`` and closes: the caller has
    already seen the session end at or before the cursor.
    """
    batch = first
    elapsed = 0.0
    while True:
        for event in batch:
            elapsed = 0.0
            cursor = event.seq
            yield frame(event)
            if event.name == terminal:
                return
        if not follow or elapsed >= IDLE_CEILING_S:
            return
        await asyncio.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S
        seen = db.data_version(conn)
        if seen == version:
            batch = ()
            continue
        version = seen
        batch = read.after(conn, session_id, cursor)


def stream(
    conn: sqlite3.Connection, session_id: str, last_event_id: str | None
) -> StreamingResponse:
    """The response for one subscription: headers fixed, body from :func:`tail`.

    Everything that can refuse happens here, before a header is sent: a malformed cursor is a
    :class:`CursorError` (400) and an unknown session id, or an eval run id or case run key in
    its place, is a ``SurfaceNotFound`` (404) from the read module. A cursor above the highest
    recorded ``seq``, or at the ``seq`` of a terminal event, is zero frames and a clean close.
    """
    cursor = parse_cursor(last_event_id)
    # The counter is read before any row: a commit that lands after this line is seen either by
    # the two reads below or as a change on the first tick, never by neither.
    version = db.data_version(conn)
    newest = read.latest(conn, session_id)
    terminal = read.terminal_for(conn, session_id)
    first = read.after(conn, session_id, cursor) if cursor < newest.seq else ()
    # Follow when there is something to follow: rows behind the cursor still to send, or a
    # client resuming exactly at the head of a session that has not ended. A cursor past the
    # head claims frames that do not exist and gets a clean close, not a wait for them.
    at_live_head = cursor == newest.seq and newest.name != terminal
    follow = cursor < newest.seq or at_live_head
    return StreamingResponse(
        tail(conn, session_id, cursor, first, version, follow=follow, terminal=terminal),
        media_type="text/event-stream",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
