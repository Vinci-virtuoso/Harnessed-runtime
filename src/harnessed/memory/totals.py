"""What the three memory stores hold, counted once, all time (FR-70).

``GET /v1/memory/totals`` and ``harnessed memory totals`` answer from here. The four figures sit
under one ``SourceChip`` because they come from one package: ``mem_semantic``, ``mem_episodic`` and
the active procedure set are all this package's tables, so the aggregate is this package's store
read (decision 41, MUST rule 14) and the gateway carries the dict it is handed.

Archived facts are their own count and are never filtered into the active one (005's rule): a
retired fact is still in memory, it is only out of retrieval, and a tile that folded it in would
claim retrieval can see it. ``procedures_active`` is the active set, not every procedure ever
written: ``procedures`` is append-only, and its length says how many were ever promoted, which is
not what a run is using.

One statement, so the four figures are one snapshot of the file. An empty store is ``"0"`` for
each, never ``null``: the tables exist and hold no rows, and that is a measurement.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from harnessed.memory.semantic import ACTIVE, ARCHIVED

__all__ = ["MemoryTotals", "totals", "totals_payload"]

#: The four counts in one pass. The two fact counts name the status literally, the same literal
#: ``semantic.py`` writes, so a third status would be counted in neither rather than in both.
_COUNT_MEMORY: Final = (
    "SELECT "
    f"(SELECT COUNT(*) FROM mem_semantic WHERE status = '{ACTIVE}'), "
    f"(SELECT COUNT(*) FROM mem_semantic WHERE status = '{ARCHIVED}'), "
    "(SELECT COUNT(*) FROM mem_episodic), "
    "(SELECT COUNT(*) FROM active_procedures)"
)


@dataclass(frozen=True, slots=True)
class MemoryTotals:
    """The three stores, counted: facts split by status, every episode, the active set."""

    facts_active: int
    facts_archived: int
    episodes: int
    procedures_active: int


def totals(conn: sqlite3.Connection) -> MemoryTotals:
    """Every figure the memory tile shows, over the whole store (decision 47: all time)."""
    facts_active, facts_archived, episodes, procedures_active = conn.execute(
        _COUNT_MEMORY
    ).fetchone()
    return MemoryTotals(
        facts_active=int(facts_active),
        facts_archived=int(facts_archived),
        episodes=int(episodes),
        procedures_active=int(procedures_active),
    )


def totals_payload(found: MemoryTotals) -> dict[str, object]:
    """The one wire shape for :func:`totals`, printed by the CLI and served by the gateway.

    Every figure is a **string** (MUST rule 29): the tile renders the characters as sent and
    derives nothing (decision 38, MUST rule 16).
    """
    return {
        "facts_active": str(found.facts_active),
        "facts_archived": str(found.facts_archived),
        "episodes": str(found.episodes),
        "procedures_active": str(found.procedures_active),
    }
