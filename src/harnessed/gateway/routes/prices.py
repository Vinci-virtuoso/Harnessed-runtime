"""``GET /v1/prices``: the stored rows, as strings, each with its own ``fetched_at`` (FR-73).

Renders ``providers/pricing.py::stored`` through ``pricing.json_payload`` and nothing else. It asks
no provider anything: the one read of a catalog is ``harnessed prices refresh`` (decision 53, MUST
rule 28), and a scan in ``tests/test_pricing.py`` holds this module to never naming it. Every rate
is the decimal string the refresh stored (MUST rule 29); ``fetched_at`` is each row's own, never the
time the page asked. The body is the ``--json`` line of ``harnessed prices list``, byte for byte.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed.gateway import deps
from harnessed.providers import pricing

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the stored prices to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/prices")
    def prices(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Every stored price, by provider then model, every rate a string."""
        return pricing.json_payload(pricing.stored(conn))
