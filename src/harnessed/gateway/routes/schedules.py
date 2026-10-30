"""``GET /v1/schedules``: the schedules read (FR-142, decision 79).

Renders ``tasks/schedule.py::listing`` through its own emitter and nothing else: the schedules, the
newest install row's ``installed`` and ``where``, and a reason beside each null. It never runs
the operating system's scheduler. The body is ``harnessed schedule list --json``, byte for byte.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed.gateway import deps
from harnessed.tasks import schedule

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the one schedules read to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/schedules")
    def schedules(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Every active schedule as the CLI lists it."""
        return schedule.listing_payload(schedule.listing(conn))
