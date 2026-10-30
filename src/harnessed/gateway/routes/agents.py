"""``GET /v1/agents``: each declared agent's newest recorded handshake probe (FR-90, decision 55).

Renders ``acp/probes.py::states`` through ``probes.json_payload`` and nothing else. The route reads
the ``agent_probes`` ledger (migration 008, ticket 089) and ``agents.toml`` from the working
directory, where ``harnessed agents test`` and doctor read it; it spawns nothing and runs no
handshake, so proving an agent stays ``harnessed agents test <name>`` in the operator's terminal.
The body is the ``--json`` line of ``harnessed agents list``, byte for byte.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed.acp import probes
from harnessed.gateway import deps

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the agent probe states to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/agents")
    def agents(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Every declared agent: its newest probe and whether it matches the launch, or unprobed."""
        return probes.json_payload(probes.states(conn))
