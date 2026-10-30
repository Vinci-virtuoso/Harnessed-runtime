"""The five routes under ``/v1/runs``: the totals, the mean turn, the loop-run listing, one
loop run, and its trace; and ``/v1/spend``, the journal's cost.

``GET /runs/totals`` renders ``loop/runs.py::totals`` through ``totals_payload`` (FR-67): one
store read over the tables ``loop/`` owns, one emitter, every count a string (decision 41,
MUST rule 29). ``GET /runs/turn_seconds`` renders ``loop/runs.py::turn_seconds`` through
``turn_seconds_payload`` the same way (FR-68, decision 54): the store counts, subtracts and
rounds, and the handler carries. Both are registered before ``{run_key}`` so the literal
segment is a route and never a key.

``GET /runs?limit=`` renders ``loop/runs.py::listing`` (FR-41): every row carries ``key_kind``,
so a five-case eval lists as five case runs and never as five top-level runs. ``GET
/runs/{run_key}`` renders ``loop/runs.py::run_payload``, the same dict ``harnessed run --json``
prints, and the store raises ``SurfaceNotFound`` itself for a key the journal does not hold.
``GET /runs/{run_key}/trace`` renders ``evals/trace.py::assemble`` and nothing else, whatever
the name suggests: the join across ``turns`` and ``tool_results`` is that module's, and the
handler calls it once. ``assemble`` answers an unknown key with an empty tuple and leaves the
saying to its caller; here that is the 404, because a trace for a run that never happened is
absence, and the trace module refuses to render absence as emptiness.

``GET /spend`` renders ``loop/runs.py::spend`` through ``spend_payload`` (FR-74, decision 53):
the store joins the journal to the stored prices, multiplies in ``Decimal`` and rounds, and the
three null classes travel beside ``total``. ``?session=`` scopes the same read to one run key,
and the store refuses a key the journal does not hold. It lives here because ``turns`` is the
journal's table, and the handler asks no provider for anything.

``{run_key}`` accepts a session id from ``harnessed run`` or a case run key from a scored eval
case, because ``turns.run_id`` holds both shapes (decision 24, MUST rule 15). Routes are
registered by :func:`register` so ``app.routes`` stays flat (MUST rule 17).
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import Depends, FastAPI

from harnessed.evals import trace
from harnessed.gateway import deps
from harnessed.gateway.routes import found, parse_limit
from harnessed.loop import runs

__all__ = ["register"]

#: What ``?limit=`` means when absent: the store's own default.
DEFAULT_LIMIT = 20


def register(app: FastAPI, prefix: str) -> None:
    """Add the totals, the mean turn, the listing, one loop run and its trace under
    ``prefix``."""

    @app.get(f"{prefix}/runs/totals")
    def loop_run_totals(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Loop runs, turns and tool calls over the whole journal, every count a string."""
        return runs.totals_payload(runs.totals(conn))

    @app.get(f"{prefix}/runs/turn_seconds")
    def loop_turn_seconds(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """The mean turn in seconds over the journal, rounded in the store, a string or null."""
        return runs.turn_seconds_payload(runs.turn_seconds(conn))

    @app.get(f"{prefix}/spend")
    def loop_spend(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        session: str | None = None,
    ) -> dict[str, object]:
        """What the journal cost in USD, the three null classes beside it; 404 from the store."""
        return runs.spend_payload(runs.spend(conn, session))

    @app.get(f"{prefix}/runs")
    def loop_runs(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        limit: str | None = None,
    ) -> dict[str, object]:
        """At most ``limit`` loop runs, newest first, each labelled with its key kind."""
        rows = runs.listing(conn, parse_limit(limit, DEFAULT_LIMIT))
        return {"runs": [asdict(row) for row in rows]}

    @app.get(f"{prefix}/runs/{{run_key}}")
    def loop_run(
        run_key: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """One loop run, the payload ``harnessed run --json`` prints; 404 from the store."""
        return runs.run_payload(conn, run_key)

    @app.get(f"{prefix}/runs/{{run_key}}/trace")
    def loop_run_trace(
        run_key: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> list[dict[str, Any]]:
        """One object per iteration of ``run_key``, the trace module's join, or 404."""
        objects = trace.assemble(conn, run_key)
        return list(found(objects or None, f"loop run {run_key!r}"))
