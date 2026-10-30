"""``create_app(home)``: the control app, the process's one write surface (FR-77, decision 49).

Docs and the OpenAPI route are off, so ``app.routes`` is exactly the ``POST`` table of decision 49
and nothing answers in a body this package did not spell. Every response is the canonical JSON
class by default and every error leaves through ``control/errors.py``. The route table spells its
own prefix, as the gateway's does, so ``dashboard_app`` mounts it whole at the same path.

The app holds its configuration (``home``) and its :class:`~harnessed.control.worker.Worker` on
``app.state``, and nothing else: the worker's in-flight future is the one piece of state between
requests (MUST rule 17 as amended). The lifespan registered here shuts the worker down with
``wait=True``; a process that mounts this app hands the same lifespan to its own app, because
Starlette does not run a mounted app's lifespan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import FastAPI

from harnessed.control import errors, routes
from harnessed.control.worker import Worker

__all__ = ["PREFIX", "create_app"]

#: Every control route is versioned under this prefix (decision 49).
PREFIX: Final = "/control/v1"


def create_app(home: Path) -> FastAPI:
    """The control app over ``home``: ``POST`` routes, one worker, one error body."""
    worker = Worker()
    app = FastAPI(
        title="harnessed control",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        default_response_class=errors.CanonicalJSONResponse,
        lifespan=worker.lifespan,
    )
    errors.install(app)
    app.state.home = home
    app.state.worker = worker
    routes.register(app, PREFIX)
    return app
