"""The one route under ``/v1/improve/cycles``: a recorded improvement cycle, read back by id.

``GET /improve/cycles/{improvement_run_id}`` renders ``improve/steps.py::cycle_for`` through
``steps.json_payload``, the same dict ``harnessed improve --json`` prints (decision 24).
``cycle_for`` rebuilds the ``Cycle`` from the ledger and answers an id no run row carries with
``None``, which is the 404 here; a run row with no steps is an empty cycle at 200, because an
interrupted start is a fact the ledger holds.

The path parameter is an **improvement run id** and nothing else (MUST rule 15). The cycle's
eval runs answer under ``/v1/eval/runs/{eval_run_id}``; this surface does not reach for them.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed.gateway import deps
from harnessed.gateway.routes import found
from harnessed.improve import steps

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the single recorded cycle to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/improve/cycles/{{improvement_run_id}}")
    def improve_cycle(
        improvement_run_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """One recorded cycle, rebuilt from the ledger, as ``improve --json`` prints it."""
        cycle = steps.cycle_for(conn, improvement_run_id)
        return steps.json_payload(found(cycle, f"improvement run {improvement_run_id!r}"))
