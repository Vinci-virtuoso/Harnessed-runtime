"""``create_app()``: the FastAPI application, and the health route it always carries.

The app is built per call from the database path it is handed and holds nothing else: no
connection, no cache, no module-level state (MUST rule 17). Docs and the OpenAPI route are off,
so ``app.routes`` is exactly the decision 24 table and nothing answers in a body this package
did not spell. Every response is a :class:`CanonicalJSONResponse` by default and every error
leaves through ``gateway/errors.py``.

Health reports the path it was handed and the schema version the package ships; it does not
open the file. Opening it read-only, one connection per request, and guarding the on-disk
version against the shipped one before anything is served are ``deps.py``'s (decision 18); a
health route that opened the database its own way would be a second read path. The resolved
path rides on ``app.state`` so ``deps.connection`` can find it from the request: that is the
app's configuration, fixed at construction, not state between requests.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from harnessed.gateway import deps, errors
from harnessed.gateway.routes import (
    agents,
    decisions,
    evals,
    events,
    improve,
    memory,
    messages,
    outputs,
    permissions,
    prices,
    providers,
    router,
    runs,
    schedules,
    tasks,
)
from harnessed.gateway.serialize import CanonicalJSONResponse

__all__ = ["PREFIX", "create_app"]

#: Every route is versioned under this prefix (decision 24).
PREFIX = "/v1"


def create_app(database_path: Path) -> FastAPI:
    """The application over ``database_path``. Absolute or not, health reports it resolved.

    Raises :class:`~harnessed.errors.SchemaOutOfDateError` when the file exists and is behind the
    shipped migrations; it never migrates (MUST rule 13).
    """
    resolved = database_path.resolve()
    deps.guard(resolved)
    app = FastAPI(
        title="harnessed",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        default_response_class=CanonicalJSONResponse,
    )
    errors.install(app)
    app.state.database_path = resolved
    runs.register(app, PREFIX)
    evals.register(app, PREFIX)
    improve.register(app, PREFIX)
    memory.register(app, PREFIX)
    events.register(app, PREFIX)
    prices.register(app, PREFIX)
    providers.register(app, PREFIX)
    agents.register(app, PREFIX)
    decisions.register(app, PREFIX)
    router.register(app, PREFIX)
    outputs.register(app, PREFIX)
    permissions.register(app, PREFIX)
    messages.register(app, PREFIX)
    tasks.register(app, PREFIX)
    schedules.register(app, PREFIX)

    @app.get(f"{PREFIX}/health")
    def health() -> dict[str, object]:
        """The schema version the package ships, the absolute database path, the route count."""
        return {
            "schema_version": deps.shipped_version(),
            "database_path": str(resolved),
            "route_count": len(app.routes),
        }

    return app
