"""``harnessed permissions list|answer`` - the twins of ``GET /v1/permissions`` and the console's
answer (FR-130, decision 74).

``list`` is ``acp/pending.py::listing`` then ``listing_payload``, the two callables the route
renders, so the ``--json`` line and the body are the same bytes. ``answer`` is
``pending.operator_answer``, the one call ``POST /control/v1/permissions/{id}`` also makes, with
``via`` ``cli``. An id already answered or expired prints ``already answered: ...`` and exits 1
through ``main``'s error contract; an unknown id is a ``SurfaceNotFound``.
"""

from __future__ import annotations

from typing import Annotated

import typer

from harnessed.acp import pending
from harnessed.cli import render
from harnessed.memory import db

__all__ = ["answer_permission", "list_permissions", "permissions_app"]

permissions_app = typer.Typer(
    name="permissions",
    help="An agent's requests to run something, and your answers to them.",
    no_args_is_help=True,
)


@permissions_app.callback()
def _group() -> None:
    """Keep ``permissions`` a command group (see ``eval_cmd._group`` for why)."""


@permissions_app.command("list")
def list_permissions(
    state: Annotated[str | None, typer.Option("--state", help="One state, e.g. `pending`.")] = None,
    session: Annotated[
        str | None, typer.Option("--session", help="Every decision of this session.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print permission requests, oldest first, by state or by session."""
    conn = db.connect()
    try:
        rows = pending.listing(conn, state=state, session=session)
    finally:
        conn.close()
    if as_json:
        render.json_object(pending.listing_payload(rows))
        return
    if not rows:
        render.line("no permission requests")
        return
    render.rows(
        f"{len(rows)} permission request(s)",
        tuple(
            f"{row.permission_id}  {row.state}  {row.agent}  {row.tool_kind}  {row.requested_at}"
            for row in rows
        ),
    )


@permissions_app.command("answer")
def answer_permission(
    permission_id: Annotated[str, typer.Argument(help="The id `permissions list` printed.")],
    verdict: Annotated[str, typer.Argument(help="approve or deny.")],
) -> None:
    """Answer one pending request. The agent is told yes or no on its next tick."""
    if verdict not in pending.ANSWERS:
        raise typer.BadParameter("answer with approve or deny.", param_hint="verdict")
    row = pending.operator_answer(permission_id, verdict, via="cli")
    render.line(f"{row.state}: {row.permission_id}")
