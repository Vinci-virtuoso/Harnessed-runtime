"""``GET /v1/tasks``, ``GET /v1/tasks/totals`` and ``GET /v1/tasks/{task_id}``: the tasks ledger
(FR-143, decisions 76 and 86).

Renders ``tasks/store.py``'s own readers through its own emitters and nothing else: the store owns
the table, the newest-first order (longest waiting first for ``?state=needs_human``), the
``unattended`` expression and the 400s and 404s (MUST rule 14). The bodies are ``harnessed task
list --json``, ``task totals --json`` and ``task show <id> --json``, byte for byte.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI, Response

from harnessed.gateway import deps
from harnessed.gateway.routes import found, parse_limit, presorted
from harnessed.tasks import store

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the three tasks reads to ``app`` under ``prefix``. ``totals`` goes before the id."""

    # The one list a thousand rows long: its dict is already wire-shaped (strings and nulls), so it
    # goes to the canonical response whole rather than through FastAPI's response-model encoder,
    # which walked every value again and was most of the read's time (ticket 265).
    @app.get(f"{prefix}/tasks", response_model=None)
    def tasks(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        state: str | None = None,
        session: str | None = None,
        limit: str | None = None,
    ) -> Response:
        """Tasks matching ``state`` and ``session``, at most ``limit``;
        400 or 404 from the store."""
        return presorted(
            store.listing_wire(
                conn, state=state, session=session, limit=parse_limit(limit, store.DEFAULT_LIMIT)
            )
        )

    @app.get(f"{prefix}/tasks/totals")
    def task_totals(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, str]:
        """Every task counted all time, by state, with ``completed_unattended``."""
        return store.totals_payload(store.totals(conn))

    @app.get(f"{prefix}/tasks/{{task_id}}")
    def task(
        task_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, str | None]:
        """One task as the list serves it; 404 for an id the ledger does not hold."""
        return store.json_payload(found(store.get(conn, task_id), f"task {task_id!r}"))
