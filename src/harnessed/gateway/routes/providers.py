"""``GET /v1/providers``: each registered provider's state, by variable name (FR-89, decision 55).

Renders ``providers/registry.py::states`` through ``registry.json_payload`` and nothing else. The
state is the environment as ``config.load`` resolves it, so this is the one handler that takes no
database connection: no ledger row is behind it. ``configured`` says the key variable is set and
never what it is set to; no field of the payload can carry a key. ``priced_models`` is not here:
it is a count over ``prices``, and the Models page reads ``/v1/prices`` as its own panel instead.
The body is the ``--json`` line of ``harnessed providers list``, byte for byte.
"""

from __future__ import annotations

from fastapi import FastAPI

from harnessed import config
from harnessed.providers import registry

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the provider states to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/providers")
    def providers() -> dict[str, object]:
        """Every registered provider: name, configured, key_var and default_model."""
        return registry.json_payload(registry.states(config.load()))
