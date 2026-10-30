"""The frozen event taxonomy (FR-21).

One name list, in one place, checked at every publish. The scheme is
``<domain>.<noun>.<past-tense-verb>``, lowercase and dot-separated, and a name is never renamed
once shipped: v2's SSE contract sets its ``event:`` field to the name verbatim, so a rename here
is a breaking change to a wire contract that has already left the building.

A later ticket that needs a new name adds it to :data:`NAMES` in the same commit as the publish
and only after checking the scheme still holds (All AI agents MUST rule 4). Adding is allowed.
Removing and renaming are not.

Two names read as nouns rather than past-tense verbs, ``acp.permission.request`` and
``acp.permission.decision``. They are frozen as the plan authored them rather than corrected,
because correcting a frozen name is the one thing this module exists to prevent.
"""

from __future__ import annotations

from typing import Final

__all__ = ["NAMES", "OBSERVER_FAILED"]

#: The name the bus publishes when an observer raises. Held as a constant because the bus is the
#: only publisher of it and a typo there would be a silent hole in the isolation guarantee.
OBSERVER_FAILED: Final = "observer.failed"

#: Every name that may be published. Nothing else reaches an observer or the ``events`` table.
NAMES: Final[frozenset[str]] = frozenset(
    {
        # the run itself
        "run.started",
        "run.finished",
        # the explicit loop (FR-4)
        "loop.iteration.started",
        "loop.iteration.finished",
        "loop.guardrail.tripped",
        # the router (FR-109)
        "loop.route.decided",
        # the provider seam (FR-2, FR-8)
        "provider.request.sent",
        "provider.response.received",
        # the tool seam (FR-6)
        "tool.call.started",
        "tool.call.finished",
        "tool.call.failed",
        # retrieval and its gate (FR-11, FR-12)
        "retrieval.gate.decided",
        "retrieval.gate.failed_open",
        "retrieval.assembled",
        "retrieval.budget.truncated",
        # memory (FR-10)
        "memory.write.committed",
        # consolidation (FR-152)
        "memory.consolidation.finished",
        # evals (FR-16)
        "eval.run.started",
        "eval.case.started",
        "eval.case.scored",
        "eval.run.finished",
        # the flywheel (FR-17 to FR-20)
        "improve.run.started",
        "improve.step.recorded",
        "improve.reflection.written",
        "improve.candidate.created",
        "improve.promotion.decided",
        "improve.run.finished",
        # delegation (FR-22, FR-23)
        "acp.delegation.started",
        "acp.permission.request",
        "acp.permission.decision",
        "acp.delegation.finished",
        "acp.delegation.cached",
        "acp.tool.called",
        "acp.tool.finished",
        # tasks (FR-140)
        "task.started",
        "task.paused",
        "task.finished",
        # cross-cutting
        "error.raised",
        OBSERVER_FAILED,
    }
)
