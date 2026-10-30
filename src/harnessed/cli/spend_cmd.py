"""``harnessed spend`` - what the run journal cost, and what it could not price (FR-74).

The twin of ``GET /v1/spend``: ``loop/runs.py::spend`` then ``runs.spend_payload``, so the
``--json`` line and the body are the same bytes (FR-37, decision 38), with ``--session`` as the
route's ``?session=``. It reads the prices ``harnessed prices refresh`` stored and asks no provider
anything (MUST rule 28). The prose form says ``unknown`` with the reason that applies where the
total is ``null``, and never prints ``0.0000`` for a journal nothing could price.
"""

from __future__ import annotations

from typing import Annotated

import typer

from harnessed.cli import render
from harnessed.loop import runs
from harnessed.memory import db

__all__ = ["spend"]


def spend(
    session: Annotated[
        str | None,
        typer.Option("--session", help="One run key only: the session id `harnessed run` printed."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """What the run journal cost in USD, with the turns it could not price counted beside it.

    The database is opened directly, like ``runs totals``: a read of stored rows has no business
    constructing a provider.
    """
    found = runs.spend(db.connect(), session)
    payload = runs.spend_payload(found)
    if as_json:
        render.json_object(payload)
        return
    if found.total is not None:
        total = f"{payload['total']} {payload['currency']}"
    elif found.prices_fetched_at is None:
        total = f"unknown ({runs.NO_PRICES_REASON})"
    else:
        total = f"unknown ({runs.NONE_PRICED_REASON})"
    unpriced_models = ", ".join(found.unpriced_models) or "none"
    render.rows(
        f"spend, {'session ' + session if session else 'all time'}",
        (
            f"total           {total}",
            f"priced turns    {payload['priced_turns']}",
            f"unknown usage   {payload['unknown_usage_turns']}  (recorded before migration 006)",
            f"unknown model   {payload['unknown_model_turns']}  (recorded before migration 010)",
            f"unpriced turns  {payload['unpriced_turns']}  (models: {unpriced_models})",
            f"prices fetched  {payload['prices_fetched_at'] or 'never'}",
            f"router          {payload['router_cost'] or 'none reported'}  "
            f"({payload['router_decisions']} decisions)",
            f"agents          {payload['agent_cost'] or 'none reported'}  (openrouter bills)",
            f"api equivalent  {payload['api_equivalent'] or 'none reported'}  "
            "(subscription agents, not in total)",
            f"not metered     {payload['not_metered_turns']}  (agent turns with no reported cost)",
        ),
    )
