"""The loop: the product, and the teaching artifact of the project (FR-4, FR-5).

``engine`` holds ``run_loop`` and nothing else of consequence; ``guardrails`` holds its two
exits, named; ``prompt`` assembles the system prompt a run starts from; ``journal`` is the ledger
that makes a run resumable (FR-7). Everything the loop needs it is handed - a model, a registry,
a request, a ceiling, a journal - so it can be read without reading anything else.
"""

from __future__ import annotations

from harnessed.loop.engine import LoopResult, Model, run_loop
from harnessed.loop.guardrails import GUARDRAILS, ITERATION_LIMIT, NO_MORE_TOOL_CALLS
from harnessed.loop.journal import NO_JOURNAL, Journal, Ledger, RecordedTurn
from harnessed.loop.prompt import build_system_prompt, envelope

__all__ = [
    "GUARDRAILS",
    "ITERATION_LIMIT",
    "NO_JOURNAL",
    "NO_MORE_TOOL_CALLS",
    "Journal",
    "Ledger",
    "LoopResult",
    "Model",
    "RecordedTurn",
    "build_system_prompt",
    "envelope",
    "run_loop",
]
