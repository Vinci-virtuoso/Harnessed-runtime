"""Ticket 077: a run that hit its token limit says so, instead of reading as finished.

A truncated answer and a finished answer used to end the same way: guardrail 1, the same two
terminal events, the same trailer. The journal recorded ``max_tokens`` and every other surface
reported a clean finish, which is MUST rule 12's defect class - output that reads as
confirmation of something that is not true.

How a run ended is two facts (decision 27). The guardrail says why the loop stopped looping; the
stop reason says how the model's last turn ended. Both are reported, neither is derived from the
other, and the loop still has exactly two guardrails.

Everything here is offline: a stub model answers with ``max_tokens`` and the socket guard stays
in force.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessed.cli import run_cmd
from harnessed.cli.app import app
from harnessed.events import read
from harnessed.events.bus import EventBus
from harnessed.events.names import NAMES
from harnessed.loop import ITERATION_LIMIT, NO_MORE_TOOL_CALLS, LoopResult, run_loop
from harnessed.loop.guardrails import GUARDRAILS
from harnessed.memory import db, gate
from harnessed.providers.types import ChatRequest, ChatResponse, Message, StopReason, ToolCall
from harnessed.tools import Tool, ToolRegistry
from support.spies import RecordingObserver

runner = CliRunner()

PROMPT = "explain the whole loop"
CUT_OFF = "The loop reasons, then acts, then"

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "077"


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- the stub model ----------------------------------------------------------------------------


@dataclass
class Scripted:
    """Replay a script, repeating its tail. The loop cannot tell it from a real provider."""

    script: list[ChatResponse]
    seen: list[ChatRequest] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.seen.append(request)
        return self.script[min(len(self.seen) - 1, len(self.script) - 1)]


def ending(stop_reason: StopReason, text: str = CUT_OFF) -> ChatResponse:
    """A turn with no tool calls, ended the way ``stop_reason`` says."""
    return ChatResponse(response_id="resp_end", model="fake", content=text, stop_reason=stop_reason)


def asks_for_echo() -> ChatResponse:
    return ChatResponse(
        response_id="resp_tool",
        model="fake",
        content="",
        tool_calls=(ToolCall(id="call_1", name="echo", arguments={"text": "x"}),),
        stop_reason="tool_use",
    )


def echo_registry() -> ToolRegistry:
    return ToolRegistry(
        [Tool(name="echo", handler=lambda arguments: "x", description="echo the text back")]
    )


def request_for(text: str = PROMPT) -> ChatRequest:
    return ChatRequest(model="fake", messages=(Message(role="user", content=text),))


# ---- the loop: the stop reason is read, never inferred ----------------------------------------


@pytest.mark.parametrize(
    "stop_reason", ["end_turn", "max_tokens", "stop_sequence", "content_filter", "other"]
)
def test_loop_result_carries_the_last_turns_stop_reason(stop_reason: StopReason) -> None:
    """Every stop reason with no tool call exits at guardrail 1, and each is reported as itself."""
    result = run_loop(Scripted([ending(stop_reason)]), echo_registry(), request_for(), limit=5)

    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.stop_reason == stop_reason
    assert result.truncated is (stop_reason == "max_tokens")


def test_the_iteration_limit_reports_the_last_turn_it_actually_took() -> None:
    """Guardrail 2 fires with the model still asking for tools, and the stop reason says so."""
    result = run_loop(Scripted([asks_for_echo()]), echo_registry(), request_for(), limit=2)

    assert result.guardrail == ITERATION_LIMIT
    assert result.stop_reason == "tool_use"
    assert result.truncated is False


def test_a_loop_that_took_no_turn_reports_no_stop_reason() -> None:
    """With no response there is nothing to read, and nothing is invented in its place."""
    result = run_loop(Scripted([ending("end_turn")]), echo_registry(), request_for(), limit=0)

    assert result.guardrail == ITERATION_LIMIT
    assert result.stop_reason is None
    assert result.truncated is False


def test_the_guardrail_event_carries_both_facts() -> None:
    observer = RecordingObserver()
    bus = EventBus()
    bus.subscribe(observer)

    run_loop(Scripted([ending("max_tokens")]), echo_registry(), request_for(), limit=5, bus=bus)

    (tripped,) = observer.named("loop.guardrail.tripped")
    assert tripped.payload["guardrail"] == NO_MORE_TOOL_CALLS
    assert tripped.payload["stop_reason"] == "max_tokens"


def test_loop_result_stays_positionally_stable() -> None:
    """``stop_reason`` is keyword-only with a default, so the first three fields never move."""
    result = LoopResult("r", (), 1)

    assert (result.reply, result.tool_calls, result.iterations) == ("r", (), 1)
    assert result.stop_reason is None
    assert result.truncated is False


# ---- the tree: still two guardrails, no event renamed -----------------------------------------


def test_the_loop_still_has_exactly_two_guardrails() -> None:
    assert GUARDRAILS == (NO_MORE_TOOL_CALLS, ITERATION_LIMIT)


def test_no_terminal_event_name_was_removed_or_renamed() -> None:
    """A name count is the wrong assertion (ticket 081 adds one); presence is the right one."""
    assert {"run.finished", "loop.guardrail.tripped", "run.started"} <= set(NAMES)


# ---- the product: `harnessed run` against a truncating model ----------------------------------


@pytest.fixture
def results(monkeypatch: pytest.MonkeyPatch) -> list[LoopResult]:
    """Capture the in-memory result the CLI renders, without changing the run."""
    captured: list[LoopResult] = []
    real = run_cmd.run_loop

    def spy(model: object, registry: object, request: ChatRequest, **kwargs: object) -> LoopResult:
        result = real(model, registry, request, **kwargs)  # type: ignore[arg-type]
        captured.append(result)
        return result

    monkeypatch.setattr(run_cmd, "run_loop", spy)
    return captured


def answering_with(monkeypatch: pytest.MonkeyPatch, stop_reason: StopReason) -> None:
    """Let the gate talk to the real fake provider and the loop to a stub ending on ``stop_reason``.

    Only the built provider object is swapped; settings still resolve from
    ``HARNESSED_PROVIDER`` exactly as an operator's would (ticket 053).
    """
    real_build = run_cmd.build_provider

    def build(settings: object) -> object:
        underlying = real_build(settings)  # type: ignore[arg-type]

        class Truncating:
            def complete(self, request: ChatRequest) -> ChatResponse:
                if request.metadata.get("scope") == gate.GATE_SCOPE:
                    return underlying.complete(request)
                return ending(stop_reason)

        return Truncating()

    monkeypatch.setattr(run_cmd, "build_provider", build)


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


def trailer_of(output: str) -> str:
    return output.rstrip("\n").splitlines()[-1]


def run_id_of(trailer: str) -> str:
    return trailer.split()[1]


def legacy_trailer(result: LoopResult, run_id: str) -> str:
    """The trailer exactly as it was printed before ticket 077."""
    return (
        f"run {run_id}  {result.iterations} iteration(s), {len(result.tool_calls)} tool call(s), "
        f"ended on {result.guardrail}"
    )


def test_a_truncated_run_reports_both_facts_on_every_surface(
    clean_env: None,
    conn: sqlite3.Connection,
    results: list[LoopResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answering_with(monkeypatch, "max_tokens")

    outcome = runner.invoke(app, ["run", PROMPT])

    assert outcome.exit_code == 0, outcome.output
    (result,) = results
    trailer = trailer_of(outcome.output)
    run_id = run_id_of(trailer)

    # the result: a structural field, read off the response
    assert result.stop_reason == "max_tokens"
    assert result.truncated is True

    # the trailer: driven by the structural field, so it cannot read as a clean finish
    assert trailer != legacy_trailer(result, run_id)
    assert trailer.startswith(legacy_trailer(result, run_id))
    assert result.stop_reason in trailer.removeprefix(legacy_trailer(result, run_id))

    # the events: both terminal payloads carry the pair
    events = read.replay(conn, run_id)
    (tripped,) = (e for e in events if e.name == "loop.guardrail.tripped")
    (finished,) = (e for e in events if e.name == "run.finished")
    for event in (tripped, finished):
        assert event.payload["guardrail"] == NO_MORE_TOOL_CALLS
        assert event.payload["stop_reason"] == "max_tokens"

    capture(
        "truncation-reported.txt",
        f"stub provider answers `harnessed run {PROMPT!r}` with stop_reason=max_tokens\n\n"
        f"run.finished payload:           {dict(finished.payload)}\n"
        f"loop.guardrail.tripped payload: {dict(tripped.payload)}\n"
        f"LoopResult.stop_reason:         {result.stop_reason}\n"
        f"LoopResult.truncated:           {result.truncated}\n\n"
        f"trailer:\n{trailer}\n",
    )


def test_a_finished_run_prints_the_trailer_and_json_it_always_did(
    clean_env: None,
    conn: sqlite3.Connection,
    results: list[LoopResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For an ``end_turn`` finish nothing an operator or a parser reads may move."""
    answering_with(monkeypatch, "end_turn")

    outcome = runner.invoke(app, ["run", PROMPT])
    as_json = runner.invoke(app, ["run", PROMPT, "--json"])

    assert outcome.exit_code == 0, outcome.output
    assert as_json.exit_code == 0, as_json.output
    result = results[0]
    trailer = trailer_of(outcome.output)
    assert result.truncated is False
    assert trailer == legacy_trailer(result, run_id_of(trailer))

    payload = run_cmd.runs.run_payload(conn, run_id_of_json(as_json.output))
    assert set(payload) == {
        "run_key",
        "key_kind",
        "started_at",
        "finished_at",
        "iterations",
        "tool_calls",
        "stop_reason",
        "reply",
        "usage",
        "duration_seconds",
        "duration_reason",
        "provider",
        "model",
    }
    assert as_json.output.strip() == run_cmd.render.canonical(payload)


def run_id_of_json(output: str) -> str:
    marker = '"run_key":"'
    start = output.index(marker) + len(marker)
    return output[start : output.index('"', start)]
