"""``harnessed gate`` - exit 0 only when every deterministic case passes.

The verdict is a pass/fail read over the run's cases, not a score comparison: each ``passed``
was decided in ``graders.py`` against the case's own declared threshold, and reading it back is
what ``promoter._broken`` does for the unseen suite. The rule the architecture reserves to
``evals/scoring.py`` and ``improve/promoter.py`` is the ranking of two runs, which carries the
epsilon; there is only one run here and nothing to rank it against.

The gate always runs fresh and under its own label. Sharing ``eval run``'s label would let it
resume a run scored under a system prompt that is no longer in force - the trap ticket 026
recorded - and would leave a row that ``harnessed eval run`` then resumed in turn.
"""

from __future__ import annotations

from typing import Annotated, Final

import typer

from harnessed.cli import render
from harnessed.cli.run_cmd import open_session
from harnessed.evals import runner
from harnessed.evals.suite import load_named_suite

__all__ = ["GATE_LABEL", "gate"]

#: The ledger label a gate run is filed under. Its own, so it resumes nothing and is resumed by
#: nothing.
GATE_LABEL: Final = "gate"


def gate(
    suite: Annotated[str, typer.Argument(help="The suite the gate scores.")] = "smoke",
) -> None:
    """Score a suite and exit non-zero unless every case passed."""
    loaded = load_named_suite(suite)
    session = open_session()
    scored = runner.run_suite(
        loaded,
        session.solve,
        session.conn,
        model=session.model,
        provider=session.provider_name,
        system=session.system(),
        label=GATE_LABEL,
        fresh=True,
        bus=session.bus,
    )
    render.eval_run(scored)
    if all(case.passed for case in scored.cases):
        render.line(f"gate: {scored.passes}/{scored.case_count} cases passed")
        return
    render.failure(
        f"gate: {scored.passes}/{scored.case_count} cases passed in suite {scored.suite}. "
        f"The deterministic evals are not at 100%."
    )
    raise typer.Exit(code=1)
