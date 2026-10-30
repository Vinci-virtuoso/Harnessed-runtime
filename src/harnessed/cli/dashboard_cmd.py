"""``harnessed dashboard`` - the one process, as a CLI command (decision 46).

Four calls and no logic, exactly as decision 21 shapes ``serve``: resolve settings, build the one
app, render the one startup line, hand the app to uvicorn. The default port is the gateway's,
8756, so every ``curl`` line in v3's evidence still resolves; ``serve`` and ``dashboard`` are
alternatives, never companions, so two servers never want the port at once.

The one thing this command does that ``serve`` does not is **refuse** a bind that is not
loopback. ``serve`` warns, because a read-only ``GET`` surface has nothing to authorize; this
process carries the control app, which starts runs and writes credentials with no auth in front
of it, so a host ``serve_cmd._is_loopback`` rejects exits non-zero **before settings are
resolved, before the app is built and before any socket exists** (FR-66). The predicate is
``serve_cmd``'s own, reached through the module rather than copied, because a second loopback
predicate is how a guard silently loosens (MUST rule 18).

``harnessed.dashboard_app`` and ``uvicorn`` are imported inside the function body, never at
module level, so a checkout without the ``gateway`` extra still imports this module and still
lists ``dashboard`` in ``--help``; the ``ImportError`` becomes ``GatewayExtraMissing`` naming the
one command that fixes it. An export nobody staged is ``ExportMissing`` naming the build command,
raised here rather than left to the static mount's ``RuntimeError`` (decision 44).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from harnessed import config
from harnessed.cli import render, serve_cmd
from harnessed.errors import BindRefused, ExportMissing, GatewayExtraMissing, HomeNotFoundError
from harnessed.memory import db

__all__ = ["COMMAND", "INDEX_PAGE", "dashboard", "staged"]

#: How the command is named in errors and in the doctor's ladder.
COMMAND = "harnessed dashboard"

#: The one file every staged export carries; its absence is the export's absence.
INDEX_PAGE = "index.html"


def staged(directory: Path) -> bool:
    """Whether ``directory`` holds a staged export: the root page is there to serve."""
    return (directory / INDEX_PAGE).is_file()


def dashboard(
    host: Annotated[
        str,
        typer.Option("--host", help="Interface to bind. Loopback only; anything else is refused."),
    ] = serve_cmd.DEFAULT_HOST,
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = serve_cmd.DEFAULT_PORT,
    provider: Annotated[
        str | None,
        typer.Option(config.PROVIDER_FLAG, help="Resolve settings as if for this provider."),
    ] = None,
    token_budget: Annotated[
        int | None,
        typer.Option(config.TOKEN_BUDGET_FLAG, help="Resolve settings with this token budget."),
    ] = None,
) -> None:
    """Serve the dashboard: the UI at /, the read gateway at /v1, on loopback only."""
    if not serve_cmd._is_loopback(host):
        raise BindRefused(host)
    settings = config.load(provider=provider, token_budget=token_budget)
    if not settings.home.is_dir():
        raise HomeNotFoundError(settings.home)
    database_path = (settings.home / db.DATABASE_FILENAME).resolve()
    try:
        import uvicorn

        from harnessed import dashboard_app
    except ImportError as exc:
        raise GatewayExtraMissing(COMMAND) from exc
    export = dashboard_app.export_directory()
    if not staged(export):
        raise ExportMissing(export)
    app = dashboard_app.create_app(settings.home)
    render.line(f"serving http://{host}:{port}  database {database_path}")
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
