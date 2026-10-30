"""The five routes over the events table: the session listing, one session's events
collection, its stream, the retrieval gate's split, and the table's totals.

``GET /v1/sessions?limit=`` renders ``events/read.py::sessions`` through
``read.sessions_payload`` (FR-48, decision 36): one row per session id, newest first, so an
eval or improve session can be found by someone who never saw the terminal that minted it.
A malformed or zero ``?limit=`` is a ``LimitError`` and a 400 by the same door as every other
listing (decision 19). An empty table is 200 with an empty list: no session yet is an answer.

``GET .../events`` renders ``events/read.py::json_payload`` and nothing else (FR-38).
``GET .../stream`` renders ``gateway/sse.py::stream``, which parses the ``Last-Event-ID``
cursor, reads the tail through the same package, and answers as a resumable SSE stream that
closes on the terminal event (FR-39). Each handler is one call. The ordered read, the refusal of
a foreign name, and the 404 for a session id the table does not hold are all the ``events``
package's: an unknown id raises ``SurfaceNotFound`` there and leaves through
``gateway/errors.py`` in the one body of decision 24, never as an empty 200 and never as an open
stream. A malformed cursor is a ``CursorError`` and a 400 by the same door.

``GET /v1/events/gate`` renders ``events/read.py::gate_split`` through ``read.gate_split_payload``
(FR-69, decisions 41 and 48): the gate's decisions over every run in three buckets, with
``failed_open`` its own and ``decided`` served as their sum, every figure a string. The bucketing
reads the payload in the events package, which owns the table; this module carries the dict and
spells no query (MUST rule 14).

``GET /v1/events/totals`` renders ``events/read.py::totals`` through ``read.totals_payload``
(FR-70, decisions 41 and 47): every event and every session, all time, with ``counted_at``. A
session is a ``seq`` 1 row, the listing's definition, counted in the events package.

The path parameter is the glossary term it carries, a **session id** (MUST rule 15). An eval run
id or a case run key in that position is simply an id the table does not hold, and gets the same
404 (decision 14). There is no stream across session ids: ``seq`` carries no meaning across them.

Routes are registered on the app by :func:`register` rather than collected on a module-level
``APIRouter``: a router is module state (MUST rule 17), and an included router lands on
``app.routes`` as a nested entry with no ``path``, which would blind the route-table scans that
FR-33 and FR-42 read. Registered this way every entry of ``app.routes`` is one flat ``GET``.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI, Header
from fastapi.responses import StreamingResponse

from harnessed.events import read
from harnessed.gateway import deps, sse
from harnessed.gateway.routes import parse_limit

__all__ = ["register"]

#: What ``?limit=`` means when absent: the store's own default.
DEFAULT_LIMIT = read.DEFAULT_SESSION_LIMIT


def register(app: FastAPI, prefix: str) -> None:
    """Add the session listing, the events collection, the stream, the gate split and the
    totals to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/events/totals")
    def totals(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Every event and every session, all time, every count a string."""
        return read.totals_payload(read.totals(conn))

    @app.get(f"{prefix}/events/gate")
    def gate_split(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """The retrieval gate's three buckets over every run, every figure a string."""
        return read.gate_split_payload(read.gate_split(conn))

    @app.get(f"{prefix}/sessions")
    def sessions(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        limit: str | None = None,
    ) -> dict[str, object]:
        """At most ``limit`` sessions, newest first, the events package's own projection."""
        rows = read.sessions(conn, parse_limit(limit, DEFAULT_LIMIT))
        return read.sessions_payload(rows)

    @app.get(f"{prefix}/sessions/{{session_id}}/events")
    def session_events(
        session_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Every event under ``session_id``, ascending ``seq``, the events package's projection."""
        return read.json_payload(conn, session_id)

    @app.get(f"{prefix}/sessions/{{session_id}}/stream")
    def session_stream(
        session_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        """Every event under ``session_id`` after the cursor, as SSE, until the terminal one."""
        return sse.stream(conn, session_id, last_event_id)
