"""The one canonical JSON spelling. Nothing else in the repository chooses separators.

``cli/render.py::json_object`` prints :func:`canonical` and the gateway's canonical response
encodes it, so the bytes an operator reads from ``--json`` and the bytes a browser fetches from
the wire are the same bytes, sorted the same way, with no space after a colon. The spelling is
the one every hashing call site already uses: ``sort_keys=True``, ``separators=(",", ":")``,
``ensure_ascii=False``.

A leaf module: it imports nothing internal to ``harnessed``, so every package may reach it.
"""

from __future__ import annotations

import json

__all__ = ["canonical", "canonical_presorted"]


def canonical(payload: object) -> str:
    """``payload`` as one line of canonical JSON: sorted keys, tight separators, unescaped text."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_presorted(payload: object) -> str:
    """:func:`canonical` for a payload whose every dict already holds its keys in ascending order.

    The same bytes without the sort, which is a third of the encoder's time over the thousand-task
    list (ticket 265). The caller owns the order: a dict out of order prints out of order.
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
