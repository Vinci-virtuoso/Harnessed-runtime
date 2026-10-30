"""``GET /v1/sessions/{session_id}/messages`` and ``GET /v1/messages``: the two v5 reads
``loop/messages.py`` owns (FR-128, decision 66).

Renders ``loop/messages.py::for_session``/``recent`` through their own emitters and nothing else:
the store reads ``decisions``, ``agent_turns``, ``turns`` and ``outputs`` in SQL the way
``loop/runs.py::spend`` reads ``prices`` (MUST rule 14), and raises ``SurfaceNotFound`` itself for
a session id the events table never held. The bodies are ``harnessed sessions messages
<session_id> --json`` and ``harnessed sessions recent --json``, byte for byte.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed.gateway import deps
from harnessed.gateway.routes import parse_limit
from harnessed.loop import messages

__all__ = ["register"]

#: What ``?limit=`` means when absent: the store's own default.
DEFAULT_LIMIT = messages.DEFAULT_MESSAGE_LIMIT


def register(app: FastAPI, prefix: str) -> None:
    """Add the session messages read and the recent-messages listing to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/sessions/{{session_id}}/messages")
    def session_messages(
        session_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Every message recorded under ``session_id``, in order; 404 from the store."""
        return messages.session_payload(conn, session_id)

    @app.get(f"{prefix}/messages")
    def recent_messages(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        limit: str | None = None,
    ) -> dict[str, object]:
        """At most ``limit`` messages, newest first over hop-0 decisions alone."""
        return messages.recent_payload(messages.recent(conn, parse_limit(limit, DEFAULT_LIMIT)))
