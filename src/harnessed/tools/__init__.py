"""The tool seam: one registry that owns declaration and dispatch (FR-6).

Importing this package gets you :func:`default_registry`, which is the registry the loop runs
with. Nothing above this line knows how a tool is implemented, and nothing below it knows what a
loop is.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from harnessed.acp.permissions import Prompter
from harnessed.events.bus import EventBus
from harnessed.tools.builtin import tools as builtin_tools
from harnessed.tools.registry import Handler, Tool, ToolRegistry

__all__ = ["Handler", "Tool", "ToolRegistry", "default_registry"]


def default_registry(
    root: Path,
    bus: EventBus | None = None,
    *,
    agents_path: Path | None = None,
    store: sqlite3.Connection | None = None,
    interactive: bool = False,
    prompter: Prompter | None = None,
) -> ToolRegistry:
    """A registry holding every builtin tool, sandboxed to ``root``.

    ``root`` is the resolved ``Settings.home``. It is passed in rather than read here so this
    package reads no process variable of its own, and so a test can sandbox a run in a
    temporary directory without patching anything. The keyword arguments are what
    ``delegate_to_agent`` needs: which agent registry to read, where to write a transcript, and
    whether there is a human present to answer a permission prompt. All of them default to the
    unattended shape, which is the shape an overnight run has.
    """
    return ToolRegistry(
        builtin_tools(
            root,
            agents_path=agents_path,
            bus=bus,
            store=store,
            interactive=interactive,
            prompter=prompter,
        ),
        bus=bus,
    )
