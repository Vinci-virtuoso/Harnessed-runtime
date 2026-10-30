"""``delegate_to_agent``: the one tool the model sees for handing work to a coding agent (FR-22).

One tool, not a session API. The model names a unit of work and what would make it acceptable,
and gets back a five-field summary. It cannot open a session, cannot drive a turn, cannot answer
a permission prompt and cannot ask which agent is configured - all of that is the harness's, and
``agents.toml`` is where the answer to "which agent" lives.

**Configuration is read when the tool runs, never when the registry is built.** A registry that
refused to construct because ``agents.toml`` was missing would take the whole loop down over a
file that only one tool needs. Reading late makes a missing or malformed registry an error
``ToolResult`` the model can read and route around, which is the same "errors are values"
contract every other tool honours.

Everything this tool needs arrives as an argument, because ``tools/`` reads no process variable
of its own: the sandbox root, the ``agents.toml`` path, the bus, the store, whether a human is
present and how to ask them. The composition root binds them; a test binds a temporary directory.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from harnessed.acp import config, delegate
from harnessed.acp.permissions import Prompter
from harnessed.events.bus import EventBus
from harnessed.tools.registry import Tool

__all__ = ["PARAMETERS", "TOOL_NAME", "hand_off", "tools"]

TOOL_NAME: Final = "delegate_to_agent"

DESCRIPTION: Final = (
    "Hand one self-contained task to an external agent and wait for its result. Use it for any "
    "work your own tools cannot do: researching on the web, reading or changing a codebase, or "
    "a multi-step investigation. Describe the task and what would make it acceptable; the agent "
    "runs to completion and returns a compact summary. Its full transcript is kept out of this "
    "conversation."
)

PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The unit of work to hand off, stated so it can be done unattended.",
        },
        "acceptance": {
            "type": "string",
            "description": "What would make the finished work acceptable.",
        },
        "agent": {
            "type": "string",
            "description": "Which configured agent to use. Omit for the configured default.",
        },
    },
    "required": ["task", "acceptance"],
    "additionalProperties": False,
}


def hand_off(
    home: Path,
    agents_path: Path,
    bus: EventBus | None,
    store: sqlite3.Connection | None,
    interactive: bool,
    prompter: Prompter | None,
    arguments: Mapping[str, Any],
) -> str:
    """Delegate one unit of work and return its summary as a single JSON object."""
    requested = arguments.get("agent")
    spec = config.agent_named(agents_path, str(requested) if requested else None)
    result = delegate.delegate(
        spec,
        task=str(arguments["task"]),
        acceptance=str(arguments["acceptance"]),
        home=home,
        bus=bus,
        store=store,
        interactive=interactive,
        prompter=prompter,
    )
    return json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def tools(
    root: Path,
    *,
    agents_path: Path | None = None,
    bus: EventBus | None = None,
    store: sqlite3.Connection | None = None,
    interactive: bool = False,
    prompter: Prompter | None = None,
) -> tuple[Tool, ...]:
    """The one ACP tool, bound to one agent registry. Exactly one, by contract."""
    registry_path = config.DEFAULT_PATH if agents_path is None else agents_path

    def handler(arguments: Mapping[str, Any]) -> str:
        return hand_off(root, registry_path, bus, store, interactive, prompter, arguments)

    return (
        Tool(
            name=TOOL_NAME,
            handler=handler,
            description=DESCRIPTION,
            parameters=PARAMETERS,
        ),
    )
