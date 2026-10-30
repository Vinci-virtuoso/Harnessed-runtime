"""``create_app(home)``: one ASGI app that serves the UI and mounts the read gateway (FR-65).

Decision 43, in one table: the shipped export at ``/`` and ``gateway.create_app(database_path)``
**mounted** at ``/v1``. Mounted, not merged, is the whole point. The gateway is called with no
new argument, keeps its own route table, its own read-only connection per request and its own
tests, so FR-33's "0 non-``GET`` routes, connection opened read-only" stays literally true of
``gateway/`` inside a process that can also write. ``control.create_app(home)`` is mounted at
``/control/v1`` beside it (ticket 136), and the two never import each other.

Mount order matters: ``/v1`` and ``/control/v1`` are registered before the ``/`` static mount, so
the static catch-all can never shadow a route. A path under ``/`` that no exported file matches
serves the export's ``404.html``, and it is **not** decision 24's error body, because it is not the
gateway answering.

Two small pieces of logic live here, and both exist because the two things being mounted were
built without each other in mind:

* The gateway's route table already spells ``/v1`` (decision 24), and the control app's spells
  ``/control/v1`` the same way. Starlette's ``Mount`` strips
  the matched prefix into ``root_path`` before the sub-app routes, which would put every gateway
  route at ``/v1/v1/...``. :class:`AtOwnPrefix` mounts the sub-app at ``/v1`` and hands it the
  path whole, so the route table it keeps is the one that answers.
* Next's export writes each page as ``<page>.html`` beside a ``<page>/`` metadata directory, so
  a direct load of ``/runs`` finds a directory with no ``index.html`` in it. :class:`Export`
  looks up ``<page>.html`` where the plain lookup found no file - the "small router in the same
  module" decision 43 named as the fallback, kept inside ``StaticFiles`` rather than beside it.

``fastapi`` is imported at module level: this module is behind the ``gateway`` extra with the
package it mounts, and ``cli/dashboard_cmd.py`` reaches it inside its function body exactly as
``serve_cmd.py`` reaches the gateway (decision 21).
"""

from __future__ import annotations

import os
import stat
from collections.abc import MutableMapping
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import Mount
from fastapi.staticfiles import StaticFiles

from harnessed import control, gateway
from harnessed.memory import db

__all__ = ["CONTROL_MOUNT", "EXPORT_DIRECTORY", "GATEWAY_MOUNT", "create_app", "export_directory"]

#: Where the gateway is mounted. Its own route table spells the same prefix (decision 24).
GATEWAY_MOUNT = "/v1"

#: Where the control app is mounted. Its own route table spells the same prefix (decision 49).
CONTROL_MOUNT = control.PREFIX

#: The package directory the export stage writes and the wheel carries (decision 44).
EXPORT_DIRECTORY = "dashboard_static"

#: The suffix Next gives an exported page: ``/runs`` is ``runs.html``.
PAGE_SUFFIX = ".html"


class AtOwnPrefix(Mount):
    """A mount whose sub-app routes on the whole path, because its table spells the prefix.

    ``Mount.matches`` moves the matched prefix into the child scope's ``root_path``, and the
    sub-app then routes on what is left. The gateway registers ``/v1/runs``, not ``/runs``, so
    the child keeps the parent's ``root_path`` and matches on the full path instead. Nothing
    about the sub-app changes: same routes, same handlers, same dependency for the connection.
    """

    # The first element is Starlette's ``Match``, which ``fastapi.routing`` does not re-export and
    # nothing under ``src/`` may import from ``starlette`` (tech stack); a full match is the one
    # case that hands back a child scope, so that is what is tested.
    def matches(self, scope: MutableMapping[str, Any]) -> tuple[Any, MutableMapping[str, Any]]:
        match, child = super().matches(scope)
        if child:
            child = {**child, "root_path": scope.get("root_path", "")}
        return match, child


class Export(StaticFiles):
    """The static export, with Next's ``<page>.html`` naming understood on a direct load."""

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        full_path, found = super().lookup_path(path)
        if found is not None and stat.S_ISREG(found.st_mode):
            return full_path, found
        page, page_found = super().lookup_path(f"{path}{PAGE_SUFFIX}")
        if page_found is not None and stat.S_ISREG(page_found.st_mode):
            return page, page_found
        return full_path, found


def export_directory() -> Path:
    """The shipped export, found through the package so a wheel and a checkout agree."""
    return Path(str(resources.files("harnessed") / EXPORT_DIRECTORY))


def create_app(home: Path) -> FastAPI:
    """The one app over ``home``: the export at ``/``, the gateway at ``/v1``, control beside it.

    Starlette runs no lifespan of a mounted app, so the control app's - the one that shuts its
    worker down with ``wait=True`` - is handed to this app as its own.

    Raises whatever ``gateway.create_app`` raises for the database under ``home`` (a schema
    behind the shipped migrations is ``SchemaOutOfDateError``, never a migration; MUST rule 13)
    and ``RuntimeError`` from ``StaticFiles`` when the export was never staged.
    """
    control_app = control.create_app(home)
    app = FastAPI(
        title="harnessed dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=control_app.router.lifespan_context,
    )
    app.router.routes.append(
        AtOwnPrefix(GATEWAY_MOUNT, app=gateway.create_app(home / db.DATABASE_FILENAME))
    )
    app.router.routes.append(AtOwnPrefix(CONTROL_MOUNT, app=control_app))
    app.mount("/", Export(directory=export_directory(), html=True))
    return app
