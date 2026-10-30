"""The eval read surfaces: recent runs, one scored run, and the comparison of two.

``GET /eval/runs?limit=`` renders ``evals/runner.py::recent`` through ``runner.summary_payload``,
one ``RunSummary`` per row - the same dict ``harnessed eval list --json`` prints (ticket 075).
``GET /eval/runs/{eval_run_id}`` renders ``evals/runner.py::scored_run`` through
``runner.json_payload``, the same dict ``harnessed eval show --json`` prints (decision 24);
``scored_run`` answers a run the ledger never scored with ``None``, which is the 404 here.

``GET /eval/compare?before=&after=`` renders ``evals/runner.py::comparison`` through
``runner.comparison_payload``, the same dict ``harnessed eval compare --json`` prints (FR-62,
decision 37). The comparison itself is ``evals/scoring.py``'s: the store read applies it, and this
module computes nothing, compares nothing and serves no verdict (MUST rule 1). Both query
parameters are required, which ``routes/__init__.py::required`` refuses in decision 24's one body
rather than FastAPI's own.

Every path parameter is an **eval run id** and nothing else (MUST rule 15). Its case run keys
are loop runs and answer under ``/v1/runs/{run_key}``; the two id sets never overlap.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI

from harnessed.evals import runner, suite
from harnessed.gateway import deps
from harnessed.gateway.routes import found, parse_limit, required

__all__ = ["register"]

#: What ``?limit=`` means when absent: the store's own default.
DEFAULT_LIMIT = 20


def register(app: FastAPI, prefix: str) -> None:
    """Add the eval listing, the single scored run and the comparison to ``app``."""

    @app.get(f"{prefix}/eval/runs")
    def eval_runs(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        limit: str | None = None,
    ) -> dict[str, object]:
        """At most ``limit`` scored eval runs, newest first."""
        rows = runner.recent(conn, parse_limit(limit, DEFAULT_LIMIT))
        return {"runs": [runner.summary_payload(row) for row in rows]}

    @app.get(f"{prefix}/eval/suites")
    def eval_suites(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """Every shipped suite with its pending count and latest run (ticket 238)."""
        return suite.listing_payload(suite.listing(conn))

    @app.get(f"{prefix}/eval/runs/{{eval_run_id}}")
    def eval_run(
        eval_run_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        """One scored run, rebuilt from the ledger, as ``eval show --json`` prints it."""
        scored = runner.scored_run(conn, eval_run_id)
        return runner.json_payload(found(scored, f"eval run {eval_run_id!r}"))

    @app.get(f"{prefix}/eval/compare")
    def eval_compare(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        before: str | None = None,
        after: str | None = None,
    ) -> dict[str, object]:
        """Two eval run ids of one suite, compared by ``evals/scoring.py`` and nothing else."""
        compared = runner.comparison(conn, required(before, "before"), required(after, "after"))
        return runner.comparison_payload(compared)
