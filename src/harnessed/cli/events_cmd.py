"""``harnessed events gate`` and ``harnessed events totals`` - the events table, counted.

``events totals`` is FR-70's twin of ``GET /v1/events/totals``: every event and every session,
all time, printed from ``read.totals_payload`` so the ``--json`` line and the body are the same
bytes. The rest of this docstring is about the gate.

FR-69's CLI twin. The gate is consulted once per run and can only fail open, so three buckets
describe every decision it has ever made: ``skip``, ``retrieve`` and ``failed_open``, with
``decided`` their sum. ``failed_open`` is its own bucket and is never folded into ``retrieve``
(decision 48): a gate that failed open did retrieve, for a different reason, and a figure that
merged them would read as confirmation of something that is not true.

Nothing is composed here. ``events/read.py`` owns the query, ``json_extract`` included, and
``read.gate_split_payload`` owns the shape; this module parses a flag and hands what came back to
``render``, so the ``--json`` line and ``GET /v1/events/gate`` are the same bytes (decision 38).
The database is opened directly, like ``sessions list`` and ``runs totals``: a count has no
business constructing a provider.
"""

from __future__ import annotations

from typing import Annotated

import typer

from harnessed.cli import render
from harnessed.events import read
from harnessed.memory import db

__all__ = ["event_totals", "events_app", "gate_split"]

events_app = typer.Typer(
    name="events",
    help="What the events table holds: its totals and the retrieval gate's split.",
    no_args_is_help=True,
)

#: FR-69's gloss beside the third figure. Words, never a percentage (D-1).
FAILED_OPEN_GLOSS = "the gate could not answer and memory was retrieved anyway"


@events_app.callback()
def _group() -> None:
    """Keep ``events`` a command group (see ``eval_cmd._group`` for why)."""


@events_app.command("gate")
def gate_split(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Count the retrieval gate's decisions over every run: skip, retrieve and failed open.

    ``--json`` prints ``events/read.py::gate_split_payload`` for ``read.gate_split``, the same
    two callables ``GET /v1/events/gate`` serves, so the line and the body are the same bytes.
    Every figure is a string on the wire (MUST rule 29) and ``decided`` is served, never summed
    by a reader. An empty table prints ``0`` four times, never a blank.
    """
    payload = read.gate_split_payload(read.gate_split(db.connect()))
    if as_json:
        render.json_object(payload)
        return
    render.rows(
        "retrieval gate, all time",
        (
            f"skip         {payload['skip']}",
            f"retrieve     {payload['retrieve']}",
            f"failed open  {payload['failed_open']}  ({FAILED_OPEN_GLOSS})",
            f"decided      {payload['decided']}",
        ),
    )


@events_app.command("totals")
def event_totals(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Count every event and every session the events table holds, all time.

    ``--json`` prints ``events/read.py::totals_payload`` for ``read.totals``, the same two
    callables ``GET /v1/events/totals`` serves. A session is a ``seq`` 1 row, as the listing
    counts it. An empty table prints ``0`` twice, never a blank.
    """
    payload = read.totals_payload(read.totals(db.connect()))
    if as_json:
        render.json_object(payload)
        return
    render.rows(
        f"the events table, all time, counted {payload['counted_at']}",
        (
            f"events    {payload['events']}",
            f"sessions  {payload['sessions']}",
        ),
    )
