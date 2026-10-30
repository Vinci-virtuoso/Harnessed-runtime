"""``GET /v1/sessions/{session_id}/decisions``: a session's decisions in order (FR-120, decision
67).

Renders ``router/decisions.py::for_session`` through ``decisions.json_payload`` and nothing else:
the store reads ``decisions`` and ``decision_options``, the two tables ``router/`` owns, and raises
``SurfaceNotFound`` itself for a session id the events table never held (MUST rule 14). The body is
the ``--json`` line of ``harnessed decisions list <session_id>``, byte for byte.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed.gateway import deps
from harnessed.router import decisions

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the session decisions read to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/sessions/{{session_id}}/decisions")
    def session_decisions(
        session_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Every decision recorded under ``session_id``, in order, options nested; 404 from the
        store."""
        return decisions.json_payload(decisions.for_session(conn, session_id))
