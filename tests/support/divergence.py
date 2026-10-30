"""Seeded divergence for the proof that the parity check can fail (FR-36, ticket 071).

Every helper here works on what a read callable returned, in memory, and returns a tampered
copy. None of them writes a row: a divergence that survived the test would be a corrupted
ledger, not a test fixture. This module lives under ``tests/support`` and nowhere under
``src/harnessed``; ``tests/test_boundaries.py`` asserts that no shipped module can reach it.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Sequence
from typing import TypeVar

from harnessed.evals.runner import EvalRun

__all__ = ["diverged_scored_run", "dropped", "redelivered", "transposed"]

T = TypeVar("T")

ScoredRun = Callable[[sqlite3.Connection, str], EvalRun | None]


def diverged_scored_run(original: ScoredRun, *, delta: float = 1e-6) -> ScoredRun:
    """A read callable shaped like ``runner.scored_run`` whose first case row's ``score`` is
    ``delta`` away from what the ledger holds. A miss stays a miss.

    The shift points into ``[0.0, 1.0]``: ``evals/scoring.py`` refuses a case score outside that
    range, so a perfect case shifted upward would make the gateway answer 500 and the parity
    check would fail on an exception rather than on its assertion, which is not the failure this
    seed exists to prove.
    """

    def scored_run(conn: sqlite3.Connection, eval_run_id: str) -> EvalRun | None:
        run = original(conn, eval_run_id)
        if run is None or not run.cases:
            return run
        first, *rest = run.cases
        direction = -1.0 if first.score + delta > 1.0 else 1.0
        shifted = dataclasses.replace(first, score=first.score + direction * delta)
        return dataclasses.replace(run, cases=(shifted, *rest))

    return scored_run


def dropped(events: Sequence[T], index: int) -> tuple[T, ...]:
    """The sequence with the element at ``index`` missing, as a replay that lost one event."""
    return (*events[:index], *events[index + 1 :])


def transposed(events: Sequence[T], first: int, second: int) -> tuple[T, ...]:
    """The sequence with the elements at ``first`` and ``second`` swapped, seqs travelling with
    them, as a replay that delivered two events out of order."""
    swapped = list(events)
    swapped[first], swapped[second] = swapped[second], swapped[first]
    return tuple(swapped)


def redelivered(frames: Sequence[T], seq: int, *, key: str = "id") -> tuple[T, ...]:
    """The frames with the one whose ``key`` equals ``seq`` sent a second time at the end, as a
    stream that redelivered an already-sent event on resume."""
    again = [frame for frame in frames if getattr(frame, key) == seq]
    assert len(again) == 1, f"seq {seq} occurs {len(again)} times in the untampered stream"
    return (*frames, again[0])
