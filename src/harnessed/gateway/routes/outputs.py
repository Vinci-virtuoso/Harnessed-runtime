"""``GET /v1/outputs/{output_id}``: the stored bytes of one delegation output (FR-127, decision 65).

Renders ``acp/outputs.py::body`` and nothing else - decision 38's **second** named exception to
"every response is canonical JSON" (the SSE stream is the first, ``gateway/sse.py``): the body is
served with its own media type, ``X-Content-Type-Options: nosniff`` and
``Content-Security-Policy: sandbox``, never wrapped in ``CanonicalJSONResponse``. ``body`` raises
``SurfaceNotFound`` itself for an unknown id and, separately, for one over the serving cap, which
leaves through ``gateway/errors.py`` in the one error body exactly as every other lookup does. A
request never names a path: the bytes come straight off the ``outputs`` table, so no path a
request supplies ever reaches ``open()``. Its CLI twin, ``harnessed outputs get <output_id> --out
<path>``, writes the same bytes.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI, Response

from harnessed.acp import outputs
from harnessed.gateway import deps

__all__ = ["register"]


def register(app: FastAPI, prefix: str) -> None:
    """Add the output body route to ``app`` under ``prefix``."""

    @app.get(f"{prefix}/outputs/{{output_id}}")
    def output_body(
        output_id: str,
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> Response:
        """The stored bytes for ``output_id``, with their media type and decision 65's two
        headers, built fresh per request rather than a module-level dict (``gateway/sse.py``'s
        own headers argument is inline for the same reason: no mutable state at import time)."""
        found = outputs.body(conn, output_id)
        headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}
        return Response(content=found.content, media_type=found.media_type, headers=headers)
