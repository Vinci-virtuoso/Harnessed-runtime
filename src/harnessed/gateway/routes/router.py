"""``GET /v1/router``: the newest recorded router probe and the confidence floors (FR-121,
decision 67, G-32).

Renders ``router/decisions.py::probe_state`` through ``router_json_payload`` and nothing else:
the store reads ``router_probes`` (the newest row, if any) and ``agents.toml`` for the per-entry
floors, exactly as ``/v1/agents`` reads the registry for its own fields. The body is the
``--json`` line of ``harnessed router status``, byte for byte.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed.gateway import deps
from harnessed.router import decisions

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the router probe state to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/router")
    def router(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """The newest recorded router probe and every route's confidence floor; ``probed: false``
        with nulls before any live rung."""
        return decisions.router_json_payload(decisions.probe_state(conn))
