"""``harnessed outputs get <output_id> --out <path>`` - the twin of ``GET /v1/outputs/{output_id}``.

``acp/outputs.py::body`` and nothing else, the same callable the route renders, so the bytes this
writes to ``--out`` and the gateway's response body are identical (decision 65). There is no
``--json`` line: the surface is bytes, not a document, the second named exception ``fastapi.md``
lists. ``body`` raises ``SurfaceNotFound`` itself for an unknown or too-large id, which propagates
to ``main``'s error contract exactly as every other lookup does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from harnessed.acp import outputs
from harnessed.memory import db

__all__ = ["get_output", "outputs_app"]

outputs_app = typer.Typer(
    name="outputs", help="An agent delegation's stored output files.", no_args_is_help=True
)


@outputs_app.callback()
def _group() -> None:
    """Keep ``outputs`` a command group (see ``eval_cmd._group`` for why)."""


@outputs_app.command("get")
def get_output(
    output_id: Annotated[str, typer.Argument(help="The output id a message's reply named.")],
    out: Annotated[Path, typer.Option("--out", help="Where to write the stored bytes.")],
) -> None:
    """Write one output's stored bytes to ``--out``, exactly as the gateway would serve them."""
    found = outputs.body(db.connect(), output_id)
    out.write_bytes(found.content)
