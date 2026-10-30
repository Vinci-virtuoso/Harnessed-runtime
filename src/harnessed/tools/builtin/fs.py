"""The two sandboxed filesystem tools: read a file, write a file, both under ``HARNESSED_HOME``.

The sandbox is one function, :func:`resolve_inside`, and every path a model supplies goes through
it. It joins the candidate onto the root and then resolves, so an absolute path, a ``..`` walk and
a symlink all collapse to a real location before the check, and the check is
``Path.resolve().is_relative_to(root)`` rather than a string prefix -
``.harnessed-evil`` starts with ``.harnessed`` and is not inside it.

The root arrives as an argument. ``tools/`` reads no process variable of its own (All AI agents
MUST rule 3); the caller resolves ``Settings.home`` and hands it down, which is also what lets a
test sandbox a run in ``tmp_path`` without patching anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any, Final

from harnessed.errors import ToolArgumentError
from harnessed.tools.registry import Tool

__all__ = ["READ_PARAMETERS", "WRITE_PARAMETERS", "read", "resolve_inside", "tools", "write"]

READ_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path relative to the harness home directory."}
    },
    "required": ["path"],
    "additionalProperties": False,
}

WRITE_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path relative to the harness home directory."},
        "content": {"type": "string", "description": "The full text to write."},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}


def resolve_inside(root: Path, candidate: str) -> Path:
    """The real location ``candidate`` names inside ``root``, or ``ToolArgumentError``.

    Joining first means an absolute candidate replaces the root and is then caught by the check,
    so there is one code path for every spelling of "outside" rather than one per trick.
    """
    home = root.resolve()
    target = (home / candidate).resolve()
    if not target.is_relative_to(home):
        raise ToolArgumentError(
            f"path {candidate!r} resolves outside the harness home directory and was refused. "
            f"Paths must stay under {home.name}."
        )
    return target


def read(root: Path, arguments: Mapping[str, Any]) -> str:
    """Return the text of one file inside ``root``."""
    candidate = str(arguments["path"])
    target = resolve_inside(root, candidate)
    if not target.is_file():
        raise ToolArgumentError(f"there is no file at {candidate!r} to read")
    return target.read_text(encoding="utf-8")


def write(root: Path, arguments: Mapping[str, Any]) -> str:
    """Write one file inside ``root``, creating its parent directories, and confirm what it did."""
    candidate = str(arguments["path"])
    target = resolve_inside(root, candidate)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = str(arguments["content"])
    target.write_text(body, encoding="utf-8")
    return f"wrote {len(body)} characters to {candidate}"


def tools(root: Path) -> tuple[Tool, ...]:
    """The filesystem tools, bound to one sandbox root."""
    return (
        Tool(
            name="fs_read",
            handler=partial(read, root),
            description="Read a UTF-8 text file from the harness home directory.",
            parameters=READ_PARAMETERS,
        ),
        Tool(
            name="fs_write",
            handler=partial(write, root),
            description="Write a UTF-8 text file into the harness home directory.",
            parameters=WRITE_PARAMETERS,
        ),
    )
