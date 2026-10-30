"""``harnessed serve`` - the read-only gateway, as a CLI command (decision 21).

Four calls and no logic: resolve settings, build the app, render the one startup line, hand the
app to uvicorn. Living here rather than behind ``python -m uvicorn`` is what gives FR-34 one
configuration path, because ``config.load()`` runs where every other command runs it instead of
from whatever directory an operator happened to be standing in.

``harnessed.gateway`` and ``uvicorn`` are imported inside the function body, never at module
level, so a checkout without the ``gateway`` extra still imports this module and still lists
``serve`` in ``--help``; the ``ImportError`` becomes ``GatewayExtraMissing`` naming the one
command that fixes it (FR-31). Uvicorn's logger is silenced, not adopted (decision 22): with
``log_config=None`` and ``access_log=False`` nothing but Python's last-resort handler writes to
stderr, and the startup line is rendered by ``cli/render.py`` like every other line.
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Final

import typer

from harnessed import config
from harnessed.cli import render
from harnessed.errors import GatewayExtraMissing, HomeNotFoundError
from harnessed.memory import db

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "serve"]

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8756

#: What a non-loopback bind is warned with. The gateway has no auth surface at all (v2 is
#: read-only on loopback by design), so binding wider exposes every ledger to the network.
_EXPOSURE_WARNING: Final = (
    "{host} is not a loopback address. The gateway has no auth surface: every ledger it serves "
    "is readable by anything that can reach this port."
)


def _is_loopback(host: str) -> bool:
    """A literal loopback address, by the same predicate the socket guard uses (MUST rule 18).

    A hostname is not an address and is warned like any other host: a resolver may answer it
    with something other than loopback, and the warning is about what could listen.
    """
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(
    host: Annotated[
        str, typer.Option("--host", help="Interface to bind. Loopback unless you mean it.")
    ] = DEFAULT_HOST,
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = DEFAULT_PORT,
    provider: Annotated[
        str | None,
        typer.Option(config.PROVIDER_FLAG, help="Resolve settings as if for this provider."),
    ] = None,
    token_budget: Annotated[
        int | None,
        typer.Option(config.TOKEN_BUDGET_FLAG, help="Resolve settings with this token budget."),
    ] = None,
) -> None:
    """Serve the ledgers read-only over HTTP. GET plus SSE; every write stays in the CLI."""
    settings = config.load(provider=provider, token_budget=token_budget)
    if not settings.home.is_dir():
        # The default home is relative, so a wrong working directory would otherwise end in an
        # empty database created here and served as if it were the real one (FR-34).
        raise HomeNotFoundError(settings.home)
    database_path = (settings.home / db.DATABASE_FILENAME).resolve()
    try:
        import uvicorn

        from harnessed.gateway import create_app
    except ImportError as exc:
        raise GatewayExtraMissing() from exc
    app = create_app(database_path)
    if not _is_loopback(host):
        render.warning(_EXPOSURE_WARNING.format(host=host))
    render.line(f"serving http://{host}:{port}  database {database_path}")
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
