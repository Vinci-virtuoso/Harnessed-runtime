"""The control app's error body and status map (decision 24 as extended by FR-77).

The same ``{"error": "<class name>", "message": "<str(exc)>"}`` the read gateway answers with,
always with a non-2xx status. It is a second file rather than an import, because ``control/``
imports nothing from the gateway package (FR-77); ``tests/test_control_app.py`` asserts the two
maps agree on every class both can raise, so they cannot drift silently.

The map: ``RunInFlight`` 409; ``PromptError``, ``SessionNotContinued`` (a case run key posted a
follow-up), ``CredentialRefused``, ``ProviderNotConfiguredError`` and ``MissingCredentialError``
400; ``SurfaceNotFound`` 404 (a
session id no journal holds); ``CursorError``, ``LimitError``, ``ParameterError`` and
``SuiteMismatch`` 400 as the gateway has them; any other ``HarnessError`` 500; anything else 500
as ``InternalError``. A path no route claims is 404 ``SurfaceNotFound``, a method a route does not
serve is 405 ``ControlError``, and a body FastAPI cannot parse is 400 ``PromptError``: no response
this app produces is FastAPI's ``{"detail": ...}``, and none is a 200 carrying an error.

``message`` is ``str(exc)`` verbatim: everything stored passed ``redact.scrub()`` on write, and
this module adds no redaction pass and must not weaken one (Security).
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

from harnessed.errors import (
    ControlError,
    CredentialRefused,
    CursorError,
    HarnessError,
    LimitError,
    MissingCredentialError,
    ParameterError,
    PermissionAnswered,
    PromptError,
    ProviderNotConfiguredError,
    RunInFlight,
    SessionNotContinued,
    SuiteMismatch,
    SurfaceNotFound,
    TaskAwaitingAnswer,
    TaskStateError,
)
from harnessed.serialize import canonical

__all__ = ["INTERNAL_ERROR", "CanonicalJSONResponse", "install", "status_of"]

#: The ``error`` name for anything that is not a ``HarnessError``: a bug, reported as one.
INTERNAL_ERROR: Final = "InternalError"


class CanonicalJSONResponse(Response):
    """One line of ``serialize.canonical(payload)``, UTF-8, ``application/json`` (decision 19).

    The gateway's class of the same name, spelled again rather than imported for the same reason
    as the map below. It lives here because the error bodies are its first user; the app makes it
    every route's default, so a 202 and a 400 are serialized by the one spelling.
    """

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return canonical(content).encode("utf-8")


#: The exhaustive map. A ``HarnessError`` child absent from it is a 500 by the fallback below.
_STATUS_BY_CLASS: Final[tuple[tuple[type[HarnessError], int], ...]] = (
    (RunInFlight, 409),
    (PermissionAnswered, 409),
    (TaskStateError, 409),
    (TaskAwaitingAnswer, 409),
    (PromptError, 400),
    (SessionNotContinued, 400),
    (CredentialRefused, 400),
    (ProviderNotConfiguredError, 400),
    (MissingCredentialError, 400),
    (SurfaceNotFound, 404),
    (CursorError, 400),
    (LimitError, 400),
    (ParameterError, 400),
    (SuiteMismatch, 400),
)


def status_of(exc: BaseException) -> int:
    """The HTTP status the control app assigns ``exc``. Never 2xx."""
    for cls, status in _STATUS_BY_CLASS:
        if isinstance(exc, cls):
            return status
    return 500


def _body(error: str, message: str, status: int) -> CanonicalJSONResponse:
    return CanonicalJSONResponse({"error": error, "message": message}, status_code=status)


def _on_exception(request: Request, exc: Exception) -> CanonicalJSONResponse:
    """Every exception the app lets out, in the one body. Plain ``def``, as the gateway's is."""
    if isinstance(exc, PermissionAnswered):
        return CanonicalJSONResponse(
            {
                "error": type(exc).__name__,
                "message": str(exc),
                "answer": exc.answer,
                "rule": exc.rule,
                "answered_via": exc.answered_via,
                "answered_at": exc.answered_at,
            },
            status_code=409,
        )
    if isinstance(exc, HarnessError):
        return _body(type(exc).__name__, str(exc), status_of(exc))
    return _body(INTERNAL_ERROR, str(exc), 500)


def _on_unreadable_body(request: Request, exc: Exception) -> CanonicalJSONResponse:
    """A body FastAPI could not parse as JSON never reached a handler: it is a refused prompt."""
    return _body(PromptError.__name__, "the request body is not JSON", 400)


def _on_router_miss(request: Request, exc: Exception) -> CanonicalJSONResponse:
    """A path no route claims, or a method the route does not serve, still in the one body."""
    status = int(getattr(exc, "status_code", 404))
    if status == 404:
        return _body(SurfaceNotFound.__name__, f"no route {request.url.path}", 404)
    return _body(
        ControlError.__name__, f"{request.method} is not served on {request.url.path}", status
    )


#: The status codes Starlette's router raises on its own (every control route is ``POST``).
_ROUTER_STATUSES: Final = (404, 405)


def install(app: FastAPI) -> None:
    """Register the handlers so every error path leaves through this module."""
    app.add_exception_handler(HarnessError, _on_exception)
    app.add_exception_handler(Exception, _on_exception)
    app.add_exception_handler(RequestValidationError, _on_unreadable_body)
    for status in _ROUTER_STATUSES:
        app.add_exception_handler(status, _on_router_miss)
