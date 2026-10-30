"""The six routes over the memory stores: facts, one fact, episodes, procedures, promotions,
and the totals.

Each renders one callable of the store that owns the table and wraps its rows under the
collection noun the CLI's ``--json`` already uses (``facts``, ``episodes``, ``procedures``),
every element ``dataclasses.asdict`` of the store's own row (ticket 067). ``GET
/memory/facts?q=&limit=`` renders ``memory/semantic.py::search``; ``GET /memory/facts/{mem_id}``
renders ``memory/semantic.py::get``, whose ``None`` is the 404 here; ``GET
/memory/episodes/{session_id}`` renders ``memory/episodic.py::for_session``; ``GET
/memory/procedures`` renders ``memory/procedural.py::active``; ``GET /promotions?limit=``
renders ``memory/procedural.py::promotions``, the decision history (FR-41).

``q`` is text an operator typed, not an FTS5 expression, so it is rendered by
``semantic.searched`` and never by ``semantic.search`` (ticket 121). Handing the raw string to
``MATCH`` made an ordinary question - ``who is Vinci?`` - a 500, and it was the search box on
``/memory/facts`` that found it. Every typed word still counts: ticket 048's rule holds, and
nothing here drops a stopword. An absent or empty ``q``, like any text holding no searchable
term, is the empty collection plus the note that says why, so a scan that requests every
registered path bare (FR-33, FR-42) reads an answer rather than a fault.

Episodes are keyed by the session id the bus carried when ``harnessed run`` or a delegation
wrote them, so the parameter is spelled ``{session_id}`` (MUST rule 15). A session id the
events table never held is a miss, the 404 of decision 24, because a client must not have to
guess whether an empty list means "recorded no episodes" or "never existed" (ticket 076). A
session that exists and wrote no episode is the empty collection. ``for_session`` tells the two
apart in one call, ``None`` against an empty tuple, so the handler still makes one call.

``GET /memory/totals`` renders ``memory/totals.py::totals`` through ``totals.totals_payload``
(FR-70, decision 41): active and archived facts, episodes and the active procedure set, four
strings from one package, so the tile holds them under one source.

Every value served here was scrubbed on write (FR-13); nothing here redacts again or less.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed import config
from harnessed.gateway import deps
from harnessed.gateway.routes import found, parse_limit
from harnessed.memory import consolidation, episodic, procedural, semantic, totals

__all__ = ["register"]

#: ``harnessed memory search`` shows ten by default; the history shows twenty.
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_HISTORY_LIMIT = 20


def register(app: FastAPI, prefix: str) -> None:
    """Add the memory routes and the promotion history to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/memory/facts")
    def facts(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        q: str = "",
        limit: str | None = None,
    ) -> dict[str, object]:
        """Active facts matching the text ``q``, best match first; no term in ``q``, no facts."""
        wanted = parse_limit(limit, DEFAULT_SEARCH_LIMIT)
        return semantic.json_payload(semantic.searched(conn, q, wanted))

    @app.get(f"{prefix}/memory/totals")
    def memory_totals(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Facts active and archived, episodes and active procedures, every figure a string."""
        return totals.totals_payload(totals.totals(conn))

    @app.get(f"{prefix}/memory/consolidation")
    def memory_consolidation(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """The trigger's threshold, the episodes waiting, and every batch newest first."""
        found = consolidation.listing(conn, config.load().consolidate_after)
        return consolidation.listing_payload(found)

    @app.get(f"{prefix}/memory/facts/{{mem_id}}")
    def fact(
        mem_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """One fact by id, archived or not; a miss is the 404."""
        return asdict(found(semantic.get(conn, mem_id), f"fact {mem_id!r}"))

    @app.get(f"{prefix}/memory/episodes/{{session_id}}")
    def episodes(
        session_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Every episode written under ``session_id``, in the order it was written; a session
        id the events table never held is the 404."""
        rows = found(episodic.for_session(conn, session_id), f"session id {session_id!r}")
        return {"episodes": [asdict(row) for row in rows]}

    @app.get(f"{prefix}/memory/procedures")
    def procedures(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """The active procedure set, in the order it was activated."""
        rows = procedural.active(conn)
        return {"procedures": [asdict(row) for row in rows]}

    @app.get(f"{prefix}/promotions")
    def promotions(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        limit: str | None = None,
    ) -> dict[str, object]:
        """At most ``limit`` recorded promotion decisions, in decided order."""
        rows = procedural.promotions(conn, parse_limit(limit, DEFAULT_HISTORY_LIMIT))
        return {"promotions": [asdict(row) for row in rows]}
