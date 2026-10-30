"""The tools that ship with the harness.

One module per tool family. ``fs`` is the sandboxed filesystem pair; ``delegate`` is the single
ACP tool, and it is single by contract - ``acp.md`` says the model sees exactly one delegation
tool and hands off a unit of work rather than driving a session.

Everything a tool needs beyond its sandbox root arrives here as a keyword argument. This package
reads no process variable of its own, so the composition root binds the agent registry, the bus,
the store and whether a human is present to answer a permission prompt.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from harnessed.acp.permissions import Prompter
from harnessed.events.bus import EventBus
from harnessed.tools.builtin import delegate, fs
from harnessed.tools.registry import Tool

__all__ = ["tools"]


def tools(
    root: Path,
    *,
    agents_path: Path | None = None,
    bus: EventBus | None = None,
    store: sqlite3.Connection | None = None,
    interactive: bool = False,
    prompter: Prompter | None = None,
) -> tuple[Tool, ...]:
    """Every builtin tool, bound to one sandbox root, in declaration order."""
    return fs.tools(root) + delegate.tools(
        root,
        agents_path=agents_path,
        bus=bus,
        store=store,
        interactive=interactive,
        prompter=prompter,
    )
