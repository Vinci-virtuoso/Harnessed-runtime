"""The one error body and the status map (decision 24). Produced here and nowhere else.

``{"error": "<class name>", "message": "<str(exc)>"}``, always with a non-2xx status:
``SurfaceNotFound`` 404, ``CursorError``, ``LimitError``, ``ParameterError`` and ``SuiteMismatch``
400 - the last of those an ``EvalError``, because the class the store raises is the class the map
names - any other ``HarnessError`` 500
(the ``GatewayError`` base included, since its unnamed children are the server's), and anything that
is not a ``HarnessError`` 500 as ``InternalError``. A router miss - a path no route claims - is
the same 404 in the same body, so no response the app produces is FastAPI's ``{"detail": ...}``.

``message`` is ``str(exc)`` verbatim: everything stored passed ``redact.scrub()`` on write, and
this module adds no redaction pass and must not weaken one (Security).
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request

from harnessed.errors import (
    CursorError,
    HarnessError,
    LimitError,
    ParameterError,
    SuiteMismatch,
    SurfaceNotFound,
)
from harnessed.gateway.serialize import CanonicalJSONResponse

__all__ = ["INTERNAL_ERROR", "install", "status_of"]

#: The ``error`` name for anything that is not a ``HarnessError``: a bug, reported as one.
INTERNAL_ERROR: Final = "InternalError"

#: The exhaustive map. A ``HarnessError`` child absent from it is a 500 by the fallback below.
_STATUS_BY_CLASS: Final[tuple[tuple[type[HarnessError], int], ...]] = (
    (SurfaceNotFound, 404),
    (CursorError, 400),
    (LimitError, 400),
    (ParameterError, 400),
    (SuiteMismatch, 400),
)


def status_of(exc: BaseException) -> int:
    """The HTTP status decision 24 assigns ``exc``. Never 2xx."""
    for cls, status in _STATUS_BY_CLASS:
        if isinstance(exc, cls):
            return status
    return 500


def _body(error: str, message: str, status: int) -> CanonicalJSONResponse:
    return CanonicalJSONResponse({"error": error, "message": message}, status_code=status)


def _on_exception(request: Request, exc: Exception) -> CanonicalJSONResponse:
    """Every exception the app lets out, in the one body.

    One handler typed on ``Exception``, registered for both ``HarnessError`` and ``Exception``,
    because Starlette's handler signature is ``(Request, Exception)`` and ``mypy --strict`` is
    the gate. The ``isinstance`` dispatch inside is the status map. Plain functions, not
    coroutines: Starlette runs a sync handler in its threadpool, and ``acp/`` plus
    ``gateway/sse.py`` are the only modules the architecture lets define ``async def``.
    """
    if isinstance(exc, HarnessError):
        return _body(type(exc).__name__, str(exc), status_of(exc))
    return _body(INTERNAL_ERROR, str(exc), 500)


def _on_router_miss(request: Request, exc: Exception) -> CanonicalJSONResponse:
    """A path no route claims, or a method the route does not serve, still in the one body.

    Starlette raises its own ``HTTPException`` for these before any handler runs, and it looks
    a handler up by status code before it looks one up by class. Registering on the two codes
    the router emits is what keeps this module from naming a Starlette class the tech stack says
    nothing under ``src/`` imports. A 404 is reported under ``SurfaceNotFound``, the name the
    ledgers use for "not here"; a 405 keeps its number under the tree's root for the surface.
    """
    status = int(getattr(exc, "status_code", 404))
    if status == 404:
        return _body(SurfaceNotFound.__name__, f"no route {request.url.path}", 404)
    return _body("GatewayError", f"{request.method} is not served on {request.url.path}", status)


#: The status codes Starlette's router raises on its own (decision 24: every path is ``GET``).
_ROUTER_STATUSES: Final = (404, 405)


def install(app: FastAPI) -> None:
    """Register the handlers so every error path leaves through this module."""
    app.add_exception_handler(HarnessError, _on_exception)
    app.add_exception_handler(Exception, _on_exception)
    for status in _ROUTER_STATUSES:
        app.add_exception_handler(status, _on_router_miss)
