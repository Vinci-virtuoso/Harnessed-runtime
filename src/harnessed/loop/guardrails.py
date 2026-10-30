"""The two exits, named (FR-4).

A run ends for exactly two reasons and this module holds both of them, spelled out, so that
``engine.py`` can label each exit with a constant rather than with a comment about a bare
``break``. Naming them here is also what makes "there is no third exit" a testable claim: the
tuple below has two entries and a third would have to be added in this file, in view of the
test that counts them.

There is never a third. A retry is the model's job, mediated by a tool result it can read; a
timeout, a budget or a cancellation is a concern of whatever calls the loop, not of the loop.
Every request for "just one more exit" is a request to move logic into the file that exists to
argue against exactly that (`loop-readability.md`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

__all__ = ["GUARDRAILS", "ITERATION_LIMIT", "NO_MORE_TOOL_CALLS", "limit_reached", "model_is_done"]

#: Guardrail 1. The model answered without asking for a tool, so the work is finished.
NO_MORE_TOOL_CALLS: Final = "no_more_tool_calls"

#: Guardrail 2. The model would keep going and the configured ceiling says otherwise.
ITERATION_LIMIT: Final = "iteration_limit"

#: Both exits, in the order a reader meets them. Two entries, forever.
GUARDRAILS: Final[tuple[str, str]] = (NO_MORE_TOOL_CALLS, ITERATION_LIMIT)


def model_is_done(tool_calls: Sequence[object]) -> bool:
    """Guardrail 1: the model asked for no tools on this turn, so there is nothing left to run."""
    return not tool_calls


def limit_reached(iterations: int, limit: int) -> bool:
    """Guardrail 2: ``iterations`` turns have been taken and the ceiling allows no more."""
    return iterations >= limit
