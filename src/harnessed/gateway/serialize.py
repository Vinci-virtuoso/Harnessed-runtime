"""The canonical response class: ``serialize.canonical(payload)`` encoded to UTF-8, nothing more.

Decision 19. The bytes a browser fetches are the bytes ``--json`` prints, because both come from
the one spelling ``harnessed/serialize.py`` owns. This class re-serializes, re-rounds and
re-keys nothing: ``render`` is one call, and there is no pydantic model anywhere in ``gateway/``
to sit between the store's payload and the wire.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import Response

from harnessed.serialize import canonical, canonical_presorted

__all__ = ["CanonicalJSONResponse", "PresortedJSONResponse"]


class CanonicalJSONResponse(Response):
    """One line of canonical JSON, UTF-8, ``application/json``."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return canonical(content).encode("utf-8")


class PresortedJSONResponse(CanonicalJSONResponse):
    """The same bytes for a payload whose dicts hold their keys ascending: no sort on the way."""

    def render(self, content: Any) -> bytes:
        return canonical_presorted(content).encode("utf-8")
