"""The explicit loop and its two labelled guardrails (FR-4).

Every test here drives ``run_loop`` with a scripted model rather than a fixture directory,
because what is under test is the loop's control flow and not the corpus: the interesting
scripts ("never asks for a tool", "always asks for a tool", "asks for a tool that explodes")
are statements about the shape of a run, and committing three fixture scopes to say them would
put the claim in a data file instead of next to the assertion.

The scripted model satisfies the provider seam structurally, which is the whole point of the
seam: the loop cannot tell it from ``FakeProvider`` or from a live adapter, and nothing in this
module imports one.
"""

from __future__ import annotations

import dataclasses
import inspect
from dataclasses import dataclass, field

import pytest

from harnessed.errors import ToolError
from harnessed.events.bus import EventBus
from harnessed.loop import (
    ITERATION_LIMIT,
    NO_MORE_TOOL_CALLS,
    LoopResult,
    build_system_prompt,
    run_loop,
)
from harnessed.loop.guardrails import GUARDRAILS
from harnessed.providers.types import (
    ChatRequest,
    ChatResponse,
    Message,
    ToolCall,
    ToolResult,
)
from harnessed.tools import Tool, ToolRegistry
from support.spies import RecordingObserver, is_subsequence

# ---- the scripted model ------------------------------------------------------------------------


@dataclass
class ScriptedModel:
    """Replay a scripted list of responses, then repeat the last one forever.

    Repeating the tail is what lets one two-entry script express "and from here on the model
    always asks for a tool", which is the shape guardrail 2 exists for.
    """

    script: list[ChatResponse]
    seen: list[ChatRequest] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.seen.append(request)
        index = min(len(self.seen) - 1, len(self.script) - 1)
        return self.script[index]

    @property
    def calls(self) -> int:
        return len(self.seen)


def answer(text: str) -> ChatResponse:
    """A turn where the model is done talking to tools."""
    return ChatResponse(response_id="resp_answer", model="fake", content=text)


def asks_for(name: str, call_id: str = "call_1", **arguments: object) -> ChatResponse:
    """A turn where the model asks for one tool."""
    return ChatResponse(
        response_id="resp_tool",
        model="fake",
        content="",
        tool_calls=(ToolCall(id=call_id, name=name, arguments=dict(arguments)),),
        stop_reason="tool_use",
    )


def request_for(text: str = "what is the answer?") -> ChatRequest:
    return ChatRequest(model="fake", messages=(Message(role="user", content=text),))


def echo_registry(bus: EventBus | None = None) -> ToolRegistry:
    """A registry holding one tool that always succeeds."""
    return ToolRegistry(
        [
            Tool(
                name="echo",
                handler=lambda arguments: str(arguments.get("text", "")),
                description="echo the text back",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            )
        ],
        bus=bus,
    )


def exploding_registry(message: str = "the disk caught fire") -> ToolRegistry:
    """A registry holding one tool that raises, plus one that does not."""

    def explode(arguments: object) -> str:
        raise RuntimeError(message)

    registry = echo_registry()
    registry.register(Tool(name="boom", handler=explode, description="always raises"))
    return registry


# ---- guardrail 1: the model stopped asking for tools --------------------------------------------


def test_a_model_that_never_asks_for_a_tool_exits_at_guardrail_one() -> None:
    result = run_loop(ScriptedModel([answer("42")]), echo_registry(), request_for(), limit=5)

    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.reply == "42"
    assert result.tool_calls == ()


def test_guardrail_one_reports_the_number_of_turns_taken() -> None:
    model = ScriptedModel([asks_for("echo"), asks_for("echo", "call_2"), answer("done")])

    result = run_loop(model, echo_registry(), request_for(), limit=10)

    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.iterations == 3
    assert result.iterations == model.calls


def test_the_loop_stops_calling_the_model_once_guardrail_one_fires() -> None:
    model = ScriptedModel([answer("42")])

    run_loop(model, echo_registry(), request_for(), limit=9)

    assert model.calls == 1


# ---- guardrail 2: the iteration limit -----------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_a_model_that_always_asks_for_a_tool_exits_at_exactly_the_limit(limit: int) -> None:
    model = ScriptedModel([asks_for("echo")])

    result = run_loop(model, echo_registry(), request_for(), limit=limit)

    assert result.guardrail == ITERATION_LIMIT
    assert result.iterations == limit
    assert model.calls == limit


def test_a_limit_of_zero_calls_the_model_never() -> None:
    model = ScriptedModel([asks_for("echo")])

    result = run_loop(model, echo_registry(), request_for(), limit=0)

    assert result.guardrail == ITERATION_LIMIT
    assert result.iterations == 0
    assert model.calls == 0


def test_the_result_names_the_guardrail_that_ended_the_run() -> None:
    ended_by = {
        run_loop(ScriptedModel([answer("x")]), echo_registry(), request_for(), limit=3).guardrail,
        run_loop(
            ScriptedModel([asks_for("echo")]), echo_registry(), request_for(), limit=3
        ).guardrail,
    }

    assert ended_by == set(GUARDRAILS)


def test_there_are_exactly_two_guardrails() -> None:
    assert GUARDRAILS == (NO_MORE_TOOL_CALLS, ITERATION_LIMIT)
    assert len(set(GUARDRAILS)) == 2


# ---- the shape of LoopResult --------------------------------------------------------------------


def test_the_first_three_fields_are_positionally_reply_tool_calls_iterations() -> None:
    fields = dataclasses.fields(LoopResult)

    assert [f.name for f in fields[:3]] == ["reply", "tool_calls", "iterations"]

    result = LoopResult("hello", (ToolCall(id="c1", name="echo"),), 2)
    assert result.reply == "hello"
    assert result.tool_calls[0].name == "echo"
    assert result.iterations == 2


def test_anything_added_after_the_first_three_is_keyword_only_with_a_default() -> None:
    added = dataclasses.fields(LoopResult)[3:]

    for f in added:
        has_default = f.default is not dataclasses.MISSING or (
            f.default_factory is not dataclasses.MISSING
        )
        assert f.kw_only, f"{f.name} must be keyword-only"
        assert has_default, f"{f.name} must have a default"


def test_the_first_three_fields_are_not_keyword_only() -> None:
    assert not any(f.kw_only for f in dataclasses.fields(LoopResult)[:3])


def test_loop_result_is_frozen() -> None:
    result = LoopResult("hello", (), 1)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.reply = "changed"  # type: ignore[misc]


# ---- the loop is synchronous --------------------------------------------------------------------


def test_run_loop_is_not_a_coroutine_function() -> None:
    assert inspect.iscoroutinefunction(run_loop) is False


def test_run_loop_returns_a_loop_result_not_an_awaitable() -> None:
    result = run_loop(ScriptedModel([answer("x")]), echo_registry(), request_for(), limit=1)

    assert isinstance(result, LoopResult)
    assert not inspect.isawaitable(result)


# ---- a failing tool is an observation, never an exception ---------------------------------------


def test_a_raising_tool_comes_back_as_an_error_result_and_the_loop_continues() -> None:
    model = ScriptedModel([asks_for("boom"), answer("recovered")])

    result = run_loop(model, exploding_registry(), request_for(), limit=5)

    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.iterations == 2
    assert result.reply == "recovered"

    observed = [r for message in result.messages for r in message.tool_results]
    assert len(observed) == 1
    assert observed[0].is_error is True
    assert "the disk caught fire" in observed[0].content


def test_no_exception_escapes_run_loop_when_the_tool_always_raises() -> None:
    model = ScriptedModel([asks_for("boom")])

    result = run_loop(model, exploding_registry(), request_for(), limit=3)

    assert result.guardrail == ITERATION_LIMIT
    assert result.iterations == 3


def test_an_unregistered_tool_is_observed_rather_than_raised() -> None:
    model = ScriptedModel([asks_for("no_such_tool"), answer("understood")])

    result = run_loop(model, echo_registry(), request_for(), limit=4)

    observed = [r for message in result.messages for r in message.tool_results]
    assert observed[0].is_error is True
    assert "no_such_tool" in observed[0].content


def test_a_call_whose_arguments_never_parsed_is_observed_rather_than_raised() -> None:
    malformed = ChatResponse(
        response_id="resp_bad",
        model="fake",
        tool_calls=(ToolCall(id="c1", name="echo", parse_error="Expecting value"),),
        stop_reason="tool_use",
    )
    model = ScriptedModel([malformed, answer("understood")])

    result = run_loop(model, echo_registry(), request_for(), limit=4)

    observed = [r for message in result.messages for r in message.tool_results]
    assert observed[0].is_error is True


def test_a_registry_error_does_not_leak_out_of_the_loop() -> None:
    registry = echo_registry()

    with pytest.raises(ToolError):
        registry.register(Tool(name="echo", handler=lambda arguments: ""))


# ---- what the loop hands back and forward -------------------------------------------------------


def test_the_transcript_records_every_turn_and_every_observation() -> None:
    model = ScriptedModel([asks_for("echo", text="hi"), answer("done")])

    result = run_loop(model, echo_registry(), request_for(), limit=5)

    roles = [message.role for message in result.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert result.messages[2].tool_results == (ToolResult(call_id="call_1", content="hi"),)


def test_every_tool_call_the_model_made_is_collected_in_order() -> None:
    model = ScriptedModel([asks_for("echo", "call_1"), asks_for("echo", "call_2"), answer("done")])

    result = run_loop(model, echo_registry(), request_for(), limit=5)

    assert [call.id for call in result.tool_calls] == ["call_1", "call_2"]


def test_the_registry_declares_the_tools_the_model_sees() -> None:
    model = ScriptedModel([answer("x")])

    run_loop(model, echo_registry(), request_for(), limit=1)

    assert [schema.name for schema in model.seen[0].tools] == ["echo"]


def test_the_request_metadata_survives_every_turn() -> None:
    model = ScriptedModel([asks_for("echo"), answer("done")])
    request = dataclasses.replace(request_for(), metadata={"scope": "smoke-001"})

    run_loop(model, echo_registry(), request, limit=5)

    assert all(seen.metadata == {"scope": "smoke-001"} for seen in model.seen)
    assert model.seen[1].system == model.seen[0].system


def test_the_history_grows_by_one_turn_per_iteration() -> None:
    model = ScriptedModel([asks_for("echo"), asks_for("echo", "call_2"), answer("done")])

    run_loop(model, echo_registry(), request_for(), limit=5)

    assert [len(seen.messages) for seen in model.seen] == [1, 3, 5]


# ---- the loop publishes ------------------------------------------------------------------------


def observed_run(limit: int, script: list[ChatResponse]) -> tuple[LoopResult, RecordingObserver]:
    bus = EventBus(run_id="run_test")
    observer = RecordingObserver()
    bus.subscribe(observer)
    registry = echo_registry(bus=bus)
    result = run_loop(ScriptedModel(script), registry, request_for(), limit=limit, bus=bus)
    return result, observer


def test_a_run_publishes_the_start_and_finish_of_every_iteration() -> None:
    _, observer = observed_run(5, [asks_for("echo"), answer("done")])

    assert len(observer.named("loop.iteration.started")) == 2
    assert len(observer.named("loop.iteration.finished")) == 2
    assert is_subsequence(
        [
            "loop.iteration.started",
            "tool.call.started",
            "tool.call.finished",
            "loop.iteration.finished",
            "loop.iteration.started",
            "loop.iteration.finished",
            "loop.guardrail.tripped",
        ],
        observer.names,
    )


def test_the_iteration_events_carry_the_iteration_number() -> None:
    _, observer = observed_run(5, [asks_for("echo"), answer("done")])

    assert [e.payload["iteration"] for e in observer.named("loop.iteration.started")] == [1, 2]
    assert [e.payload["iteration"] for e in observer.named("loop.iteration.finished")] == [1, 2]


def test_the_guardrail_event_identifies_which_guardrail_ended_the_run() -> None:
    _, done = observed_run(5, [answer("x")])
    _, capped = observed_run(2, [asks_for("echo")])

    tripped = done.named("loop.guardrail.tripped")
    assert len(tripped) == 1
    assert tripped[0].payload["guardrail"] == NO_MORE_TOOL_CALLS
    assert tripped[0].payload["iterations"] == 1

    assert capped.named("loop.guardrail.tripped")[0].payload["guardrail"] == ITERATION_LIMIT
    assert capped.named("loop.guardrail.tripped")[0].payload["iterations"] == 2


def test_the_guardrail_is_published_exactly_once_per_run() -> None:
    _, observer = observed_run(3, [asks_for("echo")])

    assert len(observer.named("loop.guardrail.tripped")) == 1


def test_the_loop_does_not_republish_the_tool_events_the_registry_owns() -> None:
    _, observer = observed_run(5, [asks_for("echo"), answer("done")])

    assert len(observer.named("tool.call.started")) == 1
    assert len(observer.named("tool.call.finished")) == 1


def test_a_run_without_a_bus_publishes_nothing_and_still_completes() -> None:
    result = run_loop(ScriptedModel([answer("x")]), echo_registry(), request_for(), limit=2)

    assert result.iterations == 1


# ---- the system prompt seam ---------------------------------------------------------------------


def test_the_base_prompt_survives_assembly_untouched() -> None:
    assert build_system_prompt("You are a careful agent.") == "You are a careful agent."


def test_a_procedure_is_appended_inside_a_labelled_envelope() -> None:
    assembled = build_system_prompt("base", ["always answer in JSON"])

    assert assembled.startswith("base")
    assert "always answer in JSON" in assembled
    assert assembled.count("<procedure>") == 1
    assert assembled.count("</procedure>") == 1


def test_procedures_are_appended_in_order_and_never_replace_the_base() -> None:
    assembled = build_system_prompt("base", ["first", "second"])

    assert assembled.index("base") < assembled.index("first") < assembled.index("second")
    assert assembled.count("<procedure>") == 2


def test_an_empty_procedure_is_not_given_an_envelope() -> None:
    assert build_system_prompt("base", ["", "   "]) == "base"


def test_the_assembled_prompt_reaches_the_model_verbatim() -> None:
    model = ScriptedModel([answer("x")])
    system = build_system_prompt("base", ["always answer in JSON"])
    request = dataclasses.replace(request_for(), system=system)

    run_loop(model, echo_registry(), request, limit=1)

    assert model.seen[0].system == system
