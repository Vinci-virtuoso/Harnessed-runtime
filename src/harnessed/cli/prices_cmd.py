"""``harnessed prices refresh`` and ``harnessed prices list`` - what a provider charges (FR-73).

``refresh`` is the only command that asks a provider for a price, and this module is the only one
that names ``providers/pricing.py::fetch`` (decision 53, MUST rule 28, asserted by
``tests/test_pricing.py``). It reads each configured provider's own models listing, upserts the
rows it priced, and says per provider how many models were priced and which provider publishes no
catalog, by name. There is no fallback: a provider that publishes none leaves its models unpriced.

``list`` is the twin of ``GET /v1/prices``: ``pricing.stored`` then ``pricing.json_payload``, so the
``--json`` line and the body are the same bytes (FR-37). It asks nothing; with no network it
answers exactly as it does with one. Each row carries its own ``fetched_at``, labelled as such.
"""

from __future__ import annotations

from typing import Annotated

import typer

from harnessed import config
from harnessed.cli import render
from harnessed.errors import ProviderError
from harnessed.memory import db
from harnessed.providers import pricing

__all__ = ["list_prices", "prices_app", "refresh"]

prices_app = typer.Typer(
    name="prices",
    help="What each provider charges, read once from its own catalog and stored.",
    no_args_is_help=True,
)


@prices_app.callback()
def _group() -> None:
    """Keep ``prices`` a command group (see ``eval_cmd._group`` for why)."""


def _outcome(catalog: pricing.Catalog) -> str:
    if not catalog.publishes:
        return (
            f"publishes no catalog: {catalog.listed} models listed, none with a price, "
            "so its models stay unpriced"
        )
    return (
        f"{len(catalog.prices)} models priced, {catalog.unpriced} listed without a usable price"
        f"  ({catalog.source_url})"
    )


@prices_app.command("refresh")
def refresh(
    provider: Annotated[
        str | None,
        typer.Option(
            config.PROVIDER_FLAG, help="Ask this provider only. Default: every configured one."
        ),
    ] = None,
) -> None:
    """Ask each configured provider for its catalog, once, and store what it publishes.

    The only command that reaches the network for a price. A provider that answers with no prices
    is named as publishing no catalog; one that refuses or cannot be reached is named on stderr,
    the rest are still stored, and the command exits non-zero.
    """
    settings = config.load()
    asked = (provider,) if provider is not None else pricing.configured(settings)
    if not asked:
        raise ProviderError(
            "no provider with a catalog is configured, so there is nothing to ask. Set one of "
            + ", ".join(sorted(v for v in config.PROVIDER_KEY_VARS.values() if v))
            + ", or name one with --provider."
        )
    conn = db.connect()
    outcomes: list[str] = []
    refused: list[str] = []
    for name in asked:
        try:
            catalog = pricing.fetch(settings, name)
        except ProviderError as exc:
            refused.append(str(exc))
            outcomes.append(f"{name:<11} not stored: see the error below")
            continue
        db.write(conn, pricing.upserts(catalog))
        outcomes.append(f"{name:<11} {_outcome(catalog)}")
    render.rows(f"prices, per million tokens, {pricing.CURRENCY}", outcomes)
    if provider is None:
        not_asked = sorted(
            name
            for name, var in config.PROVIDER_KEY_VARS.items()
            if var is not None and name not in asked
        )
        if not_asked:
            render.line(f"not configured, so not asked: {', '.join(not_asked)}")
    if refused:
        raise ProviderError(" ".join(refused))


@prices_app.command("list")
def list_prices(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print every stored price, by provider then model, each with its own ``fetched_at``.

    ``--json`` prints ``pricing.json_payload`` for ``pricing.stored``, the two callables
    ``GET /v1/prices`` serves. Nothing here asks a provider anything.
    """
    rows = pricing.stored(db.connect())
    if as_json:
        render.json_object(pricing.json_payload(rows))
        return
    if not rows:
        render.line("no prices stored. `harnessed prices refresh` asks each configured provider.")
        return
    render.rows(
        f"{len(rows)} stored price(s), per million tokens, as each provider published them",
        tuple(
            f"{row.provider}  {row.model}  input {row.input}  output {row.output}  "
            f"cached_input {row.cached_input if row.cached_input is not None else 'unpublished'}  "
            f"{row.currency}  fetched_at {row.fetched_at}"
            for row in rows
        ),
    )
