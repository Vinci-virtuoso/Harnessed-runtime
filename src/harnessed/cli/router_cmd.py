"""``harnessed router status`` - the twin of ``GET /v1/router``.

``router/decisions.py::probe_state`` then ``decisions.router_json_payload``, the same two
callables the route renders, so the ``--json`` line and the body are the same bytes (FR-37,
FR-121, decision 67). The newest recorded live rung, beside every route's confidence floor from
``agents.toml`` - never probed reads as nulls with ``probed: false``, never a guess.
"""

from __future__ import annotations

from typing import Annotated

import typer

from harnessed.cli import render
from harnessed.memory import db
from harnessed.router import decisions

__all__ = ["router_app", "status"]

router_app = typer.Typer(
    name="router",
    help="The pinned router model's latest live probe, and the confidence floors it routes on.",
    no_args_is_help=True,
)


@router_app.callback()
def _group() -> None:
    """Keep ``router`` a command group (see ``eval_cmd._group`` for why)."""


@router_app.command("status")
def status(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print the newest recorded router probe and every route's confidence floor."""
    found = decisions.probe_state(db.connect())
    if as_json:
        render.json_object(decisions.router_json_payload(found))
        return
    if found.routing_reason is not None:
        render.line(found.routing_reason)
    if not found.probed:
        render.line("router: never probed - run `harnessed doctor --live`")
        return
    render.rows(
        f"router: {found.model} ({found.status})",
        (
            f"latency={found.latency_seconds}s  cost={found.cost}  probed_at={found.probed_at}",
            f"detail: {found.detail}",
            *(f"floor  {row.route}={row.floor_percent}" for row in found.floors),
        ),
    )
