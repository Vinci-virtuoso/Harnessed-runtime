"""``harnessed health`` - the gateway's health triple, printed without a server (ticket 232).

``GET /v1/health`` reports the schema version the package ships, the absolute database path and
how many routes the app registers. This command builds the same app in process, through the same
``create_app``, and prints the same three keys through the same canonical spelling, so the
``--json`` line and the body are the same bytes (FR-134, decision 19). The gateway is not edited;
the app is only constructed, never served.

``harnessed.gateway`` is imported inside the function, never at module level, so a checkout
without the ``gateway`` extra still imports the command surface (``serve_cmd`` does the same).
"""

from __future__ import annotations

from typing import Annotated

import typer

from harnessed.cli import render
from harnessed.memory import db

__all__ = ["health"]


def health(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print the schema version, the database path and the gateway's route count."""
    from harnessed.gateway import create_app

    application = create_app(db.database_path())
    payload: dict[str, object] = {
        "schema_version": db.load_migrations()[-1].version,
        "database_path": str(db.database_path().resolve()),
        "route_count": len(application.routes),
    }
    if as_json:
        render.json_object(payload)
        return
    render.rows(
        "gateway health",
        tuple(f"{key}  {value}" for key, value in payload.items()),
    )
