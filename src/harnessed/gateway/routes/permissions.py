"""``GET /v1/permissions?state=pending`` and ``?session=<id>``: an agent's permission requests
(FR-130, decision 74).

Renders ``acp/pending.py::listing`` through ``listing_payload`` and nothing else: the store owns
the table, the computed state (``pending``, ``approved``, ``denied``, ``expired``, ``allowed``,
``refused``), the ``requested_at`` order and the 404 for a session no ledger holds (MUST rule 14).
The body is the ``--json`` line of ``harnessed permissions list``, byte for byte.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed.acp import pending
from harnessed.gateway import deps

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the permission decisions read to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/permissions")
    def permissions(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        state: str | None = None,
        session: str | None = None,
    ) -> dict[str, object]:
        """Every decision matching ``state`` and ``session``, oldest request first; 400 or 404
        from the store."""
        return pending.listing_payload(pending.listing(conn, state=state, session=session))
