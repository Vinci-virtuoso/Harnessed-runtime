"""``harnessed decisions list <session_id>`` - the twin of ``GET /v1/sessions/{id}/decisions``.

``router/decisions.py::for_session`` then ``decisions.json_payload``, the same two callables the
route renders, so the ``--json`` line and the body are the same bytes (FR-37, FR-120, decision
67). The store raises ``SurfaceNotFound`` itself for a session id the events table never held,
which propagates to ``main``'s error contract exactly as ``spend --session`` does.
"""

from __future__ import annotations

from typing import Annotated

import typer

from harnessed.cli import render
from harnessed.memory import db
from harnessed.router import decisions

__all__ = ["decisions_app", "list_decisions"]

decisions_app = typer.Typer(
    name="decisions",
    help="A session's routing decisions, in order.",
    no_args_is_help=True,
)


@decisions_app.callback()
def _group() -> None:
    """Keep ``decisions`` a command group (see ``eval_cmd._group`` for why)."""


@decisions_app.command("list")
def list_decisions(
    session_id: Annotated[str, typer.Argument(help="The session id `harnessed run` printed.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print every decision recorded under one session, in order, each with its options."""
    found = decisions.for_session(db.connect(), session_id)
    if as_json:
        render.json_object(decisions.json_payload(found))
        return
    if not found:
        render.line(f"session {session_id}: no decisions recorded")
        return
    render.rows(
        f"{len(found)} decision(s) for session {session_id}",
        tuple(
            f"idx {row.idx}  hop {row.hop}  {row.outcome}  route={row.route}  "
            f"handler={row.handler}  confidence={row.confidence_percent}  model={row.model}"
            for row in found
        ),
    )
