"""``harnessed providers list`` - which providers are configured, by variable name (FR-89).

The twin of ``GET /v1/providers``: ``registry.states`` over ``config.load`` then
``registry.json_payload``, so the ``--json`` line and the body are the same bytes (FR-37). It asks
no provider anything and prints no key: ``configured`` is whether the key variable is set, and the
variable is named rather than read back. Setting a key is ``harnessed init`` (or the Models page,
which calls the same composition); this command only reports.
"""

from __future__ import annotations

from typing import Annotated

import typer

from harnessed import config
from harnessed.cli import render
from harnessed.providers import registry

__all__ = ["list_providers", "providers_app"]

providers_app = typer.Typer(
    name="providers",
    help="Which providers are configured, by the name of their key variable, never its value.",
    no_args_is_help=True,
)


@providers_app.callback()
def _group() -> None:
    """Keep ``providers`` a command group (see ``eval_cmd._group`` for why)."""


def _key(state: registry.ProviderState) -> str:
    return "needs no key" if state.key_var is None else f"key {state.key_var}"


@providers_app.command("list")
def list_providers(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print every registered provider: configured, its key variable and its default model.

    ``--json`` prints ``registry.json_payload`` for ``registry.states``, the two callables
    ``GET /v1/providers`` serves. No key value is ever printed.
    """
    states = registry.states(config.load())
    if as_json:
        render.json_object(registry.json_payload(states))
        return
    render.rows(
        f"{len(states)} registered provider(s)",
        tuple(
            f"{state.name:<11} configured: {'true' if state.configured else 'false'}  "
            f"{_key(state)}  default_model {state.default_model or 'none known'}"
            for state in states
        ),
    )
