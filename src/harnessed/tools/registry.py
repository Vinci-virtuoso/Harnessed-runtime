"""Declaration and dispatch, owned in one place. The seam the loop calls and never branches on.

FR-6. Four outcomes - the tool succeeded, the tool raised, the tool is not registered, the
arguments never parsed - and one shape for all four:
:class:`~harnessed.providers.types.ToolResult`. Errors return as values here, because an
exception crossing this seam would put a ``try`` block in ``loop/engine.py`` and a tool-name
conditional shortly after it (`loop-readability.md`).

Dispatch is a table lookup by name. There is no ``if name == ...`` ladder and there cannot be
one: registration is the only way a tool becomes callable, which is what lets ticket 021 add
``delegate_to_agent`` without touching this module.

This is the sole consumer of the malformed-arguments marker produced by
``providers/translate.py`` (architecture open question 8). :meth:`ToolRegistry.dispatch` reads
``call.parse_error`` before anything else and, when it is set, answers with an error result
explaining what was malformed, without ever looking a handler up. The model gets a sentence it
can self-correct from and the loop gets the same shape it gets from every other outcome.

The catch around the handler call is deliberately total. A tool is arbitrary code - a filesystem
write, and from ticket 021 a subprocess talking a protocol - so a seam that promised "errors are
values" while only catching the errors it anticipated would not be a seam at all. This module and
``events/bus.py`` are the two sanctioned broad catches in the package, each one guarding a call
into code it does not own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from harnessed.errors import HarnessError, ToolError, ToolNotFoundError
from harnessed.events.bus import EventBus
from harnessed.providers.types import ToolCall, ToolResult, ToolSchema
from harnessed.tools import schema

__all__ = ["Handler", "Tool", "ToolRegistry"]

#: What a tool body is: arguments in, one string of content out. Anything else it wants to say
#: it says by raising, and the raise becomes an error result.
Handler = Callable[[Mapping[str, Any]], str]

_STARTED: Final = "tool.call.started"
_FINISHED: Final = "tool.call.finished"
_FAILED: Final = "tool.call.failed"


@dataclass(frozen=True, slots=True)
class Tool:
    """One registered tool: its declaration and the callable behind it."""

    name: str
    handler: Handler
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def schema(self) -> ToolSchema:
        """The declaration the model sees. The same object arguments are validated against."""
        return ToolSchema(name=self.name, description=self.description, parameters=self.parameters)


class ToolRegistry:
    """The table: declaration out of :meth:`schemas`, execution through :meth:`dispatch`."""

    def __init__(self, tools: Iterable[Tool] = (), bus: EventBus | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._bus = bus
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> Tool:
        """Add ``tool``. A duplicate name is an error, never a silent overwrite."""
        if tool.name in self._tools:
            raise ToolError(
                f"a tool named {tool.name!r} is already registered. "
                f"Two tools with one name means the model cannot address either."
            )
        self._tools[tool.name] = tool
        return tool

    def names(self) -> tuple[str, ...]:
        """Every registered name, in registration order."""
        return tuple(self._tools)

    def handler_for(self, name: str) -> Handler:
        """The callable registered under ``name``, or ``ToolNotFoundError`` naming the choices."""
        return self._tool_for(name).handler

    def schemas(self) -> list[ToolSchema]:
        """Every declaration, in registration order. This is what fills ``ChatRequest.tools``."""
        return [tool.schema() for tool in self._tools.values()]

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Run ``call`` and answer with a result. Total: it has no failure mode of its own."""
        self._publish(_STARTED, call)
        if call.parse_error is not None:
            return self._failure(
                call,
                f"the arguments for tool {call.name!r} were not valid JSON and could not be "
                f"read: {call.parse_error}. Send the call again with its arguments as a "
                f"well-formed JSON object.",
            )
        try:
            tool = self._tool_for(call.name)
            schema.validate(tool.name, tool.parameters, call.arguments)
            content = tool.handler(call.arguments)
        except HarnessError as exc:
            return self._failure(call, str(exc))
        except Exception as exc:
            return self._failure(call, f"tool {call.name!r} failed: {exc}")
        result = ToolResult(call_id=call.id, content=content)
        self._publish(_FINISHED, call)
        return result

    # ---- internals ---------------------------------------------------------------------------

    def _tool_for(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(
                f"no tool named {name!r} is registered. "
                f"Registered tools: {', '.join(self.names()) or 'none'}."
            ) from exc

    def _failure(self, call: ToolCall, message: str) -> ToolResult:
        self._publish(_FAILED, call, error=message)
        return ToolResult(call_id=call.id, content=message, is_error=True)

    def _publish(self, name: str, call: ToolCall, error: str | None = None) -> None:
        """Announce one moment of the dispatch, when there is a bus listening."""
        if self._bus is None:
            return
        payload: dict[str, object] = {"call_id": call.id, "name": call.name}
        if error is not None:
            payload["error"] = error
        self._bus.publish(name, payload)
