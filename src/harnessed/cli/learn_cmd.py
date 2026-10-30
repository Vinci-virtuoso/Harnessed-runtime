"""``harnessed learn`` - find and print a pillar walkthrough (decision 106, FR-183).

The walkthroughs ship as package data, so the files sit in ``site-packages`` where a newcomer
cannot find them (UJ-31), and ``--help`` cannot print a file. Bare, the command lists the four
pillars with one line and the installed path each; ``learn <pillar>`` prints the walkthrough's
text and its path; an unknown pillar exits 1 naming the four.
"""

from __future__ import annotations

from typing import Annotated

import typer

from harnessed import shipped
from harnessed.cli import render
from harnessed.errors import ConfigError

__all__ = ["learn"]

PILLARS = ("loops", "evals", "memory", "harness")


def learn(
    pillar: Annotated[
        str | None, typer.Argument(help="One of: loops, evals, memory, harness.")
    ] = None,
) -> None:
    """List the four pillar walkthroughs, or print one with its installed path."""
    directory = shipped.walkthroughs_dir()
    if pillar is None:
        for name in PILLARS:
            render.line(f"{name}  {directory / f'{name}.md'}")
        return
    if pillar not in PILLARS:
        render.error(ConfigError(f"unknown pillar {pillar!r}; choose one of {', '.join(PILLARS)}"))
        raise typer.Exit(1)
    path = directory / f"{pillar}.md"
    render.line(path.read_text(encoding="utf-8").rstrip("\n"))
    render.line(f"path  {path}")
