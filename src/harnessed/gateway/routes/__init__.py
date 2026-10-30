"""The route modules, one per store that answers them (decision 24).

Each module exposes one ``register(app, prefix)`` and every handler renders exactly one
subsystem callable through :class:`~harnessed.gateway.serialize.CanonicalJSONResponse`. No
module here spells a query (MUST rule 14) or re-shapes a payload (MUST rule 16); ``app.py``
registers each module under ``PREFIX`` by calling it.

Two small translations live here because every route module needs them and neither is a
query or a re-shaping. :func:`found` turns a store's ``None`` into the 404 of decision 24:
``semantic.get`` and ``runner.scored_run`` answer a miss with ``None`` because a miss is an
answer to them, and the one place that answer becomes an HTTP status is the gateway.
:func:`parse_limit` hand-parses ``?limit=`` (decision 19): a typed parameter would let FastAPI
answer a malformed value in a body this package never spelled. :func:`required` is the same
argument for a parameter that has no default at all (decision 37): a required query parameter is
the client's to supply, and a missing one leaves as a 400 in the one body rather than as FastAPI's
validation shape. :func:`presorted` answers a payload that is already wire-shaped, with its keys
ascending, without FastAPI's encoder re-walking it (the tasks list, ticket 265).
"""

from __future__ import annotations

from fastapi import Response

from harnessed.errors import LimitError, ParameterError, SurfaceNotFound
from harnessed.gateway.serialize import PresortedJSONResponse

__all__ = ["found", "parse_limit", "presorted", "required"]


def found[T](value: T | None, what: str) -> T:
    """``value`` unless the store answered ``None``, which is :class:`SurfaceNotFound` (404)."""
    if value is None:
        raise SurfaceNotFound(f"no {what}: the ledgers hold nothing under that id.")
    return value


def parse_limit(raw: str | None, default: int) -> int:
    """``?limit=`` as a positive integer; absent means ``default``.

    Only a run of ASCII digits above zero is a limit. Anything else is a :class:`LimitError`,
    which leaves as 400 in the one body of decision 24 rather than as FastAPI's own validation
    shape.
    """
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit() or int(raw) == 0:
        raise LimitError(f"limit {raw!r} is not a positive integer.")
    return int(raw)


def required(value: str | None, name: str) -> str:
    """``value`` unless the query string omitted it or left it empty, which is 400.

    Empty counts as absent: ``?before=&after=x`` states no id, and reading the ledgers for ``""``
    would answer a 404 about an id the caller never gave. The refusal happens before any store
    call, so a half-stated question never reaches a connection.
    """
    if not value:
        raise ParameterError(f"the {name!r} query parameter is required and was not given.")
    return value


def presorted(payload: object) -> Response:
    """``payload`` as the canonical bytes, for a store payload whose dicts hold ascending keys.

    Returned whole from a handler it skips FastAPI's response-model encoder, which re-walks every
    value of a thousand-row list; the bytes are those ``harnessed ... --json`` prints.
    """
    return PresortedJSONResponse(payload)
