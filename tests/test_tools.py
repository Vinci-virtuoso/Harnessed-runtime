"""FR-6: the tool registry owns declaration and dispatch, and errors come back as values.

Four outcomes, one shape, zero branches in the loop. A tool that succeeded, one that raised, one
that is not registered, and one whose arguments never parsed all leave this seam as a
``ToolResult``. That is what lets ``loop/engine.py`` stay under 80 lines with no tool-name
conditional in it (`loop-readability.md`), and it is why ``dispatch()`` is total: nothing a tool
can do turns into an exception crossing the seam.

This module is also the consumer side of architecture open question 8. It owns the AST scan that
keeps the marker to one producer (``providers/translate.py``) and one consumer
(``tools/registry.py``), and the input-safety scan that keeps model output out of ``eval``, out
of a shell, and inside ``HARNESSED_HOME``.

The loop is ticket 011 and is scope_out here, so "the loop continues to the next iteration" is
asserted the only honest way available today: a batch of calls is dispatched in order and every
one of them returns a result, including the ones after the failure.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from harnessed.errors import HarnessError, ToolArgumentError, ToolError, ToolNotFoundError
from harnessed.events.bus import EventBus
from harnessed.providers.types import ToolCall, ToolResult, ToolSchema
from harnessed.tools import default_registry
from harnessed.tools import schema as schema_module
from harnessed.tools.builtin import fs
from harnessed.tools.registry import Tool, ToolRegistry
from support.spies import RecordingObserver

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
TOOLS_ROOT = PACKAGE_ROOT / "tools"

#: The two modules architecture open question 8 permits to know the marker exists.
MARKER_OWNERS = {"providers/translate.py", "tools/registry.py"}

ECHO_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


# ---- benches ---------------------------------------------------------------------------------


def echo(arguments: Mapping[str, Any]) -> str:
    return str(arguments["text"])


def boom(arguments: Mapping[str, Any]) -> str:
    raise RuntimeError("the tool exploded")


def harness_boom(arguments: Mapping[str, Any]) -> str:
    raise ToolError("the tool refused politely")


@pytest.fixture
def registry() -> ToolRegistry:
    """A registry with one working tool and two that fail in different ways."""
    return ToolRegistry(
        (
            Tool(
                name="echo",
                handler=echo,
                description="Echo the text back.",
                parameters=ECHO_SCHEMA,
            ),
            Tool(name="boom", handler=boom, description="Always raises."),
            Tool(name="harness_boom", handler=harness_boom, description="Raises a HarnessError."),
        )
    )


@pytest.fixture
def home(tmp_path: Path) -> Iterator[Path]:
    """A throwaway sandbox root. ``tools/`` takes it as an argument, never from the environment."""
    root = tmp_path / ".harnessed"
    root.mkdir()
    yield root


# ---- the four outcomes, one shape ------------------------------------------------------------


def test_a_registered_tool_that_succeeds_returns_a_result_carrying_its_output(
    registry: ToolRegistry,
) -> None:
    result = registry.dispatch(ToolCall(id="call_1", name="echo", arguments={"text": "hello"}))
    assert result == ToolResult(call_id="call_1", content="hello", is_error=False)


def test_a_registered_tool_that_raises_comes_back_as_an_error_result(
    registry: ToolRegistry,
) -> None:
    """FR-6: a failing tool is a tool result, never an exception out of the loop."""
    result = registry.dispatch(ToolCall(id="call_2", name="boom"))
    assert result.call_id == "call_2"
    assert result.is_error is True
    assert "the tool exploded" in result.content


def test_a_tool_raising_from_the_error_tree_is_reported_the_same_way(
    registry: ToolRegistry,
) -> None:
    """The seam is total. A ``HarnessError`` child is a value here, not a propagating raise."""
    result = registry.dispatch(ToolCall(id="call_3", name="harness_boom"))
    assert result.is_error is True
    assert "the tool refused politely" in result.content


def test_an_unregistered_tool_is_an_error_result_not_a_crash(registry: ToolRegistry) -> None:
    result = registry.dispatch(ToolCall(id="call_4", name="no_such_tool"))
    assert result.call_id == "call_4"
    assert result.is_error is True
    assert "no_such_tool" in result.content
    # The message names what *is* available, so a weak model can self-correct on the next turn.
    assert "echo" in result.content


def test_a_parse_error_is_an_error_result_explaining_the_malformed_arguments(
    registry: ToolRegistry,
) -> None:
    call = ToolCall(id="call_5", name="echo", arguments={}, parse_error="Expecting a comma")
    result = registry.dispatch(call)
    assert result.call_id == "call_5"
    assert result.is_error is True
    assert "Expecting a comma" in result.content
    assert "echo" in result.content


def test_dispatch_checks_parse_error_before_it_ever_looks_up_a_handler(
    registry: ToolRegistry,
) -> None:
    """Open question 8: ``parse_error`` is checked first, without a handler lookup.

    The tool named here is registered and would succeed, so nothing but ordering can make this
    fail, and ``handler_for`` is replaced by a tripwire so a lookup would be observable.
    """
    looked_up: list[str] = []

    def tripwire(name: str) -> Any:
        looked_up.append(name)
        raise AssertionError("dispatch looked up a handler for a call it could not parse")

    registry.handler_for = tripwire  # type: ignore[method-assign]
    result = registry.dispatch(
        ToolCall(id="call_6", name="echo", arguments={"text": "x"}, parse_error="bad json")
    )
    assert result.is_error is True
    assert looked_up == []


def test_an_unknown_tool_with_a_parse_error_reports_the_parse_error(
    registry: ToolRegistry,
) -> None:
    """Ordering again, from the other side: the marker wins over the missing handler."""
    call = ToolCall(id="call_7", name="ghost", arguments={}, parse_error="unterminated string")
    result = registry.dispatch(call)
    assert "unterminated string" in result.content


@pytest.mark.parametrize(
    "call",
    [
        ToolCall(id="c", name="echo", arguments={"text": "ok"}),
        ToolCall(id="c", name="boom"),
        ToolCall(id="c", name="ghost"),
        ToolCall(id="c", name="echo", arguments={}, parse_error="bad json"),
        ToolCall(id="c", name="echo", arguments={"text": 7}),
    ],
    ids=["success", "raises", "unknown", "parse_error", "bad_arguments"],
)
def test_every_outcome_has_the_same_shape_and_keeps_the_call_id(
    registry: ToolRegistry, call: ToolCall
) -> None:
    result = registry.dispatch(call)
    assert isinstance(result, ToolResult)
    assert result.call_id == call.id


def test_a_batch_survives_a_failure_in_the_middle_so_the_loop_can_continue(
    registry: ToolRegistry,
) -> None:
    """The loop is ticket 011; what it needs from here is that dispatch never stops the batch."""
    calls = (
        ToolCall(id="a", name="echo", arguments={"text": "first"}),
        ToolCall(id="b", name="boom"),
        ToolCall(id="c", name="ghost"),
        ToolCall(id="d", name="echo", arguments={}, parse_error="bad json"),
        ToolCall(id="e", name="echo", arguments={"text": "last"}),
    )
    results = [registry.dispatch(call) for call in calls]
    assert [result.call_id for result in results] == ["a", "b", "c", "d", "e"]
    assert [result.is_error for result in results] == [False, True, True, True, False]
    assert results[-1].content == "last"


# ---- declaration -----------------------------------------------------------------------------


def test_the_registry_emits_a_schema_for_every_registered_tool(registry: ToolRegistry) -> None:
    schemas = registry.schemas()
    assert all(isinstance(item, ToolSchema) for item in schemas)
    assert [item.name for item in schemas] == list(registry.names())
    echo_schema = next(item for item in schemas if item.name == "echo")
    assert echo_schema.description == "Echo the text back."
    assert echo_schema.parameters == ECHO_SCHEMA


def test_dispatch_is_table_driven_every_registered_name_returns_a_handler(
    registry: ToolRegistry,
) -> None:
    """FR-6: the registry answers by name, so no ``if name == ...`` ladder can exist."""
    for name in registry.names():
        assert callable(registry.handler_for(name))


def test_the_tools_package_compares_against_no_tool_name(home: Path) -> None:
    """A table, not a ladder: no literal tool name is compared anywhere in ``tools/``."""
    known = set(default_registry(home).names()) | {"echo", "boom"}
    offenders = []
    for path in sorted(TOOLS_ROOT.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Compare):
                continue
            literals = {
                child.value
                for child in [node.left, *node.comparators]
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
            if literals & known:
                offenders.append(f"{_module_of(path)}:{node.lineno}")
    assert offenders == []


def test_an_unknown_name_raises_from_the_error_tree_rather_than_a_key_error(
    registry: ToolRegistry,
) -> None:
    with pytest.raises(ToolNotFoundError) as caught:
        registry.handler_for("ghost")
    assert isinstance(caught.value, HarnessError)
    assert "ghost" in str(caught.value)


def test_registering_the_same_name_twice_is_an_error_not_a_silent_overwrite() -> None:
    bench = ToolRegistry((Tool(name="echo", handler=echo),))
    with pytest.raises(ToolError):
        bench.register(Tool(name="echo", handler=echo))


def test_the_registry_accepts_a_tool_it_did_not_ship_with(registry: ToolRegistry) -> None:
    """Ticket 021 adds ``delegate_to_agent`` by registering it, with no registry change."""
    registry.register(Tool(name="delegate_to_agent", handler=echo, parameters=ECHO_SCHEMA))
    assert "delegate_to_agent" in registry.names()
    result = registry.dispatch(
        ToolCall(id="d1", name="delegate_to_agent", arguments={"text": "done"})
    )
    assert result == ToolResult(call_id="d1", content="done", is_error=False)


def test_the_default_registry_declares_the_builtin_filesystem_tools(home: Path) -> None:
    bench = default_registry(home)
    assert "fs_read" in bench.names()
    assert "fs_write" in bench.names()
    assert {item.name for item in bench.schemas()} == set(bench.names())


def test_the_default_registry_still_carries_delegate_to_agent(home: Path) -> None:
    """v4 (and ``HARNESSED_ROUTER=off``) keeps the hand-off tool: ``tools/`` carries 0 diff for
    ticket 168 (decision 60, FR-107). Only :func:`~harnessed.cli.run_cmd.routed_registry` drops it.
    """
    assert "delegate_to_agent" in default_registry(home).names()


def test_the_routed_registry_composes_every_builtin_tool_minus_delegate_to_agent(
    home: Path,
) -> None:
    """Ticket 168, AC1: with routing on, the run composition's registry exposes 0
    ``delegate_to_agent`` schemas - a Groq route delegates from code, never by the model asking.
    """
    from harnessed.cli.run_cmd import routed_registry

    routed = routed_registry(home)
    default_names = default_registry(home).names()
    assert "delegate_to_agent" not in routed.names()
    assert {"fs_read", "fs_write"} <= set(routed.names())
    assert routed.names() == tuple(name for name in default_names if name != "delegate_to_agent")


# ---- argument validation ---------------------------------------------------------------------


def test_arguments_are_validated_against_the_schema_before_the_handler_runs() -> None:
    """Input safety: a schema violation never reaches the tool body."""
    ran: list[Mapping[str, Any]] = []

    def record(arguments: Mapping[str, Any]) -> str:
        ran.append(arguments)
        return "ran"

    bench = ToolRegistry((Tool(name="echo", handler=record, parameters=ECHO_SCHEMA),))
    result = bench.dispatch(ToolCall(id="v1", name="echo", arguments={"text": 7}))
    assert result.is_error is True
    assert "text" in result.content
    assert ran == []


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "text"),
        ({"text": 7}, "string"),
        ({"text": "ok", "extra": 1}, "extra"),
    ],
    ids=["missing_required", "wrong_type", "unexpected_property"],
)
def test_schema_validation_names_the_offending_property(
    arguments: Mapping[str, Any], expected: str
) -> None:
    with pytest.raises(ToolArgumentError) as caught:
        schema_module.validate("echo", ECHO_SCHEMA, arguments)
    assert expected in str(caught.value)


def test_a_tool_declaring_no_parameters_accepts_an_empty_call() -> None:
    schema_module.validate("boom", {}, {})


@pytest.mark.parametrize(
    ("declared", "value"),
    [
        ("string", "s"),
        ("integer", 3),
        ("number", 1.5),
        ("boolean", True),
        ("array", [1]),
        ("object", {"a": 1}),
    ],
)
def test_every_declared_json_type_accepts_its_own_value(declared: str, value: object) -> None:
    schema_module.validate("t", {"properties": {"v": {"type": declared}}}, {"v": value})


def test_a_boolean_is_not_an_integer() -> None:
    """``True`` is an ``int`` in Python and is not an integer in JSON Schema."""
    with pytest.raises(ToolArgumentError):
        schema_module.validate("t", {"properties": {"v": {"type": "integer"}}}, {"v": True})


def test_an_unknown_declared_type_is_a_declaration_error_not_a_silent_pass() -> None:
    with pytest.raises(ToolError):
        schema_module.validate("t", {"properties": {"v": {"type": "date"}}}, {"v": "x"})


def test_an_optional_property_may_simply_be_absent() -> None:
    """Only ``required`` names are demanded; the rest are checked when they are supplied."""
    schema_module.validate(
        "t", {"properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}, {"a": "x"}
    )


def test_a_property_declared_without_a_type_accepts_any_value() -> None:
    """An untyped declaration is a deliberate "anything goes", not an accident to guess at."""
    schema_module.validate("t", {"properties": {"v": {"description": "free"}}}, {"v": [1, 2]})


def test_arguments_that_are_not_a_mapping_are_refused() -> None:
    with pytest.raises(ToolArgumentError):
        schema_module.validate("t", ECHO_SCHEMA, ["text"])  # type: ignore[arg-type]


# ---- the sandbox -----------------------------------------------------------------------------


def test_fs_write_then_fs_read_round_trips_inside_home(home: Path) -> None:
    bench = default_registry(home)
    written = bench.dispatch(
        ToolCall(id="w", name="fs_write", arguments={"path": "notes.txt", "content": "hi"})
    )
    assert written.is_error is False
    assert (home / "notes.txt").read_text(encoding="utf-8") == "hi"
    read = bench.dispatch(ToolCall(id="r", name="fs_read", arguments={"path": "notes.txt"}))
    assert read.content == "hi"
    assert read.is_error is False


def test_fs_write_creates_the_parent_directory_inside_home(home: Path) -> None:
    bench = default_registry(home)
    result = bench.dispatch(
        ToolCall(id="w", name="fs_write", arguments={"path": "a/b/c.txt", "content": "deep"})
    )
    assert result.is_error is False
    assert (home / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "deep"


def test_reading_a_missing_file_is_an_error_result_naming_the_path(home: Path) -> None:
    result = default_registry(home).dispatch(
        ToolCall(id="r", name="fs_read", arguments={"path": "absent.txt"})
    )
    assert result.is_error is True
    assert "absent.txt" in result.content


def test_reading_a_directory_is_an_error_result_not_a_traceback(home: Path) -> None:
    (home / "sub").mkdir()
    result = default_registry(home).dispatch(
        ToolCall(id="r", name="fs_read", arguments={"path": "sub"})
    )
    assert result.is_error is True


@pytest.mark.parametrize(
    "escape",
    ["../outside.txt", "a/../../outside.txt", "./../../outside.txt"],
    ids=["parent", "through_a_child", "dot_prefixed"],
)
def test_a_relative_escape_from_home_is_refused(home: Path, escape: str) -> None:
    bench = default_registry(home)
    result = bench.dispatch(
        ToolCall(id="w", name="fs_write", arguments={"path": escape, "content": "leak"})
    )
    assert result.is_error is True
    assert not (home.parent / "outside.txt").exists()


def test_an_absolute_path_outside_home_is_refused(home: Path, tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    result = default_registry(home).dispatch(
        ToolCall(id="w", name="fs_write", arguments={"path": str(target), "content": "leak"})
    )
    assert result.is_error is True
    assert not target.exists()


def test_the_sandbox_check_is_the_resolve_then_is_relative_to_pattern(home: Path) -> None:
    """Architecture: the check is ``Path.resolve().is_relative_to(...)``, not a string prefix."""
    with pytest.raises(ToolArgumentError) as caught:
        fs.resolve_inside(home, "../outside.txt")
    assert "outside" in str(caught.value)
    assert fs.resolve_inside(home, "inside.txt") == (home / "inside.txt").resolve()


def test_a_sibling_directory_sharing_the_prefix_is_not_inside_home(
    home: Path, tmp_path: Path
) -> None:
    """``.harnessed-evil`` starts with ``.harnessed``; a string prefix test would let it through."""
    sibling = tmp_path / ".harnessed-evil"
    sibling.mkdir()
    with pytest.raises(ToolArgumentError):
        fs.resolve_inside(home, str(sibling / "loot.txt"))


# ---- observability ---------------------------------------------------------------------------


def test_dispatch_publishes_started_then_finished_for_a_successful_call() -> None:
    bus = EventBus(run_id="run_tools")
    observer = bus.subscribe(RecordingObserver())
    bench = ToolRegistry((Tool(name="echo", handler=echo, parameters=ECHO_SCHEMA),), bus=bus)
    bench.dispatch(ToolCall(id="e1", name="echo", arguments={"text": "hi"}))
    assert observer.names == ["tool.call.started", "tool.call.finished"]
    assert observer.events[0].payload["name"] == "echo"
    assert observer.events[0].payload["call_id"] == "e1"


@pytest.mark.parametrize(
    "call",
    [
        ToolCall(id="f", name="boom"),
        ToolCall(id="f", name="ghost"),
        ToolCall(id="f", name="echo", arguments={}, parse_error="bad json"),
        ToolCall(id="f", name="echo", arguments={"text": 7}),
    ],
    ids=["raises", "unknown", "parse_error", "bad_arguments"],
)
def test_every_failing_outcome_publishes_tool_call_failed(call: ToolCall) -> None:
    bus = EventBus(run_id="run_tools")
    observer = bus.subscribe(RecordingObserver())
    bench = ToolRegistry(
        (
            Tool(name="echo", handler=echo, parameters=ECHO_SCHEMA),
            Tool(name="boom", handler=boom),
        ),
        bus=bus,
    )
    bench.dispatch(call)
    assert observer.names[-1] == "tool.call.failed"


def test_a_registry_without_a_bus_dispatches_silently(registry: ToolRegistry) -> None:
    assert registry.dispatch(ToolCall(id="q", name="boom")).is_error is True


# ---- structure: the marker has exactly one producer and one consumer -------------------------


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_of(path: Path) -> str:
    return path.relative_to(PACKAGE_ROOT).as_posix()


def marker_uses(tree: ast.Module) -> list[int]:
    """Line numbers where the marker is *used* rather than declared.

    A use is reading or writing the field, passing it by keyword, or naming either spelling as a
    whole string constant. The annotated declaration on the frozen dataclass in
    ``providers/types.py`` is the shape itself, not a use, and prose in a docstring is prose -
    matching whole constants only is what keeps the scan from flagging the paragraph that
    explains the rule.
    """
    names = {"parse_error", "_arg_parse_error"}
    declared = {
        node.target.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id in names
    }
    lines = {node.lineno for node in ast.walk(tree) if _names_the_marker(node, names)}
    return sorted(lines - declared)


def _names_the_marker(node: ast.AST, names: set[str]) -> bool:
    """Whether ``node`` spells either marker name in a position that is a use of it."""
    if isinstance(node, ast.Attribute):
        return node.attr in names
    if isinstance(node, ast.keyword):
        return node.arg in names
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in names
    return False


def test_parse_error_is_single_owner() -> None:
    """Architecture open question 8: one producer, one consumer, and no third party."""
    owners = {
        _module_of(path) for path in sorted(PACKAGE_ROOT.rglob("*.py")) if marker_uses(_parse(path))
    }
    assert owners == MARKER_OWNERS


def test_the_marker_declaration_lives_only_on_the_frozen_tool_call() -> None:
    """The field is declared once, in the module that owns the normalized shapes."""
    declarers = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "parse_error"
            ):
                declarers.append(_module_of(path))
    assert declarers == ["providers/types.py"]


@pytest.mark.parametrize(
    "source",
    [
        "def f(call):\n    return call.parse_error\n",
        "def f(entry):\n    return entry.get('parse_error')\n",
        "def f(entry):\n    return ToolCall(parse_error=entry)\n",
        "KEY = '_arg_parse_error'\n",
    ],
    ids=["attribute", "string_key", "keyword", "constant"],
)
def test_a_third_occurrence_fails_the_scan(source: str) -> None:
    """The scan has teeth: a would-be third party is detected in every spelling."""
    assert marker_uses(ast.parse(source)) != []


def test_the_scan_ignores_prose_and_the_declaration() -> None:
    """Neither a docstring explaining the rule nor the dataclass field counts as a use."""
    source = (
        '"""The parse_error field is serialized under the marker name."""\n'
        "class C:\n"
        "    parse_error: str | None = None\n"
    )
    assert marker_uses(ast.parse(source)) == []


# ---- structure: input safety -----------------------------------------------------------------


def test_tools_never_evaluate_or_shell_out_model_output() -> None:
    """Architecture: model output is never ``eval``'d and never shell-interpolated."""
    forbidden = {"eval", "exec", "compile", "system", "popen", "run", "check_output", "check_call"}
    offenders = []
    for path in sorted(TOOLS_ROOT.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if called in forbidden:
                offenders.append(f"{_module_of(path)}:{node.lineno} {called}")
    assert offenders == []


def test_tools_import_only_what_the_import_table_permits() -> None:
    """``tools -> acp, providers.types, events``, plus the leaves every module may use."""
    permitted = (
        "harnessed.acp",
        "harnessed.clock",
        "harnessed.errors",
        "harnessed.events",
        "harnessed.ids",
        "harnessed.providers.types",
        "harnessed.tools",
    )
    seen = set()
    for path in sorted(TOOLS_ROOT.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("harnessed"):
                seen.add(node.module or "")
            elif isinstance(node, ast.Import):
                seen.update(
                    alias.name for alias in node.names if alias.name.startswith("harnessed")
                )
    assert seen
    for module in sorted(seen):
        assert any(
            module == allowed or module.startswith(f"{allowed}.") for allowed in permitted
        ), f"{module} is outside the import table for tools/"


def test_the_sandbox_root_is_never_read_from_the_environment_by_tools() -> None:
    """Rule 3: ``config.py`` is the only reader. ``tools/`` takes the root as an argument."""
    for path in sorted(TOOLS_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "environ" not in source
        assert "getenv" not in source
