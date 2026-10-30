"""Ticket 078: token limits account for models that reason before they answer.

On gpt-oss the retrieval gate's eight tokens were spent reasoning and the answer came back ``''``,
so the gate failed open; the main reply ran out at 1024 mid-sentence. Reasoning is a property of
the **model**, not the endpoint, so it lives in ``quirks.MODELS`` (decision 26) and is applied in
each adapter's ``shape_request``: the wire ceiling is ``max_tokens + reasoning_headroom``, while
``ChatRequest.max_tokens`` keeps meaning "tokens for the answer" and ``memory/gate.py`` learns
nothing (MUST rule 19).

Everything here is offline. The stub replaces the adapter's SDK client, spends ``REASONING``
tokens of the wire ceiling before it answers, and answers with whatever is left, which is how the
real endpoint behaved on 2026-09-10.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harnessed import config
from harnessed.config import Settings
from harnessed.events.names import NAMES
from harnessed.loop import ITERATION_LIMIT, NO_MORE_TOOL_CALLS, run_loop
from harnessed.loop.guardrails import GUARDRAILS
from harnessed.memory import gate
from harnessed.providers import anthropic as anthropic_adapter
from harnessed.providers import openai_compat, quirks
from harnessed.providers.types import ChatRequest, Message
from harnessed.tools import Tool, ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "harnessed"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "078"

REASONING_MODEL = "openai/gpt-oss-120b"
PLAIN_MODEL = "allam-2-7b"

#: What the stub spends thinking before it writes a word. Well inside the headroom, well above
#: the gate's eight tokens: the shape of the live observation.
REASONING = 600

#: A reply long enough that 1024 answer tokens hold it and 1024 minus the reasoning do not.
LONG_REPLY_TOKENS = 900


# ---- the stub reasoning endpoint ---------------------------------------------------------------


@dataclass
class ReasoningEndpoint:
    """Spends ``reasoning`` tokens of the wire ceiling, then answers with what is left.

    ``answer_tokens`` is how long the whole answer is; a ceiling that leaves less than that cuts
    it off and reports the dialect's own truncation word. Every payload it was sent is kept, so a
    test can read the wire ceiling off the request the adapter actually built.
    """

    answer: str
    answer_tokens: int
    reasoning: int = REASONING
    sent: list[dict[str, Any]] = field(default_factory=list)

    def room(self, payload: dict[str, Any]) -> tuple[str, bool]:
        self.sent.append(payload)
        left = int(payload["max_tokens"]) - self.reasoning
        if left <= 0:
            return "", True
        if left < self.answer_tokens:
            words = self.answer.split()
            return " ".join(words[: max(1, len(words) * left // self.answer_tokens)]), True
        return self.answer, False

    def openai(self, **payload: Any) -> SimpleNamespace:
        content, cut = self.room(payload)
        body = {
            "id": "chatcmpl_reasoning",
            "model": payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "length" if cut else "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": payload["max_tokens"]},
        }
        return SimpleNamespace(model_dump=lambda: body)

    def anthropic(self, **payload: Any) -> SimpleNamespace:
        content, cut = self.room(payload)
        body = {
            "id": "msg_reasoning",
            "type": "message",
            "role": "assistant",
            "model": payload["model"],
            "content": [{"type": "text", "text": content}] if content else [],
            "stop_reason": "max_tokens" if cut else "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": payload["max_tokens"]},
        }
        return SimpleNamespace(model_dump=lambda: body)


@pytest.fixture
def live_settings(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """A configured live provider whose endpoint is never reached; chdir keeps `.env` out."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-credential-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-credential-value")
    return config.load()


def openai_adapter(settings: Settings, endpoint: ReasoningEndpoint) -> Any:
    adapter = openai_compat.OpenAICompatAdapter(settings=settings, provider="groq")
    adapter._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=endpoint.openai))
    )
    return adapter


def anthropic_live(settings: Settings, endpoint: ReasoningEndpoint) -> Any:
    adapter = anthropic_adapter.AnthropicAdapter(settings=settings, provider="anthropic")
    adapter._client = SimpleNamespace(messages=SimpleNamespace(create=endpoint.anthropic))
    return adapter


def no_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    """The world before this ticket: no model is known to reason."""
    monkeypatch.setattr(quirks, "MODELS", {})


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- the table ---------------------------------------------------------------------------------


def test_model_caps_is_a_frozen_value_with_a_non_reasoning_default() -> None:
    default = quirks.ModelCaps(reasons=False, reasoning_headroom=0)
    assert default == quirks.DEFAULT_MODEL_CAPS
    with pytest.raises(AttributeError):
        quirks.DEFAULT_MODEL_CAPS.reasoning_headroom = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("model", "reasons"),
    [
        ("openai/gpt-oss-120b", True),
        ("openai/gpt-oss-20b", True),
        ("gpt-oss:20b", True),
        ("allam-2-7b", False),
        ("fake-1", False),
        ("", False),
    ],
)
def test_model_caps_for_reads_the_table_by_prefix(model: str, reasons: bool) -> None:
    caps = quirks.model_caps_for(model)
    assert caps.reasons is reasons
    assert (caps.reasoning_headroom > 0) is reasons


def test_the_longest_matching_prefix_wins() -> None:
    table = {"a": quirks.ModelCaps(True, 1), "ab": quirks.ModelCaps(True, 2)}
    assert quirks.model_caps_for("abc", table=table).reasoning_headroom == 2
    assert quirks.model_caps_for("ax", table=table).reasoning_headroom == 1
    assert quirks.model_caps_for("b", table=table) == quirks.DEFAULT_MODEL_CAPS


# ---- the wire: headroom is added on a reasoning model and nowhere else ------------------------


@pytest.mark.parametrize(
    ("shape", "caps"),
    [
        (openai_compat.shape_request, quirks.DEFAULT_CAPS),
        (anthropic_adapter.shape_request, quirks.caps_for("https://api.anthropic.com")),
    ],
)
def test_a_reasoning_model_gets_its_headroom_on_the_wire(shape: Any, caps: Any) -> None:
    request = ChatRequest(model=REASONING_MODEL, messages=(Message(role="user", content="hi"),))
    headroom = quirks.model_caps_for(REASONING_MODEL).reasoning_headroom

    assert shape(request, caps)["max_tokens"] == request.max_tokens + headroom
    assert request.max_tokens == 1024, "the normalized request keeps meaning tokens-for-answer"


@pytest.mark.parametrize(
    ("shape", "caps"),
    [
        (openai_compat.shape_request, quirks.DEFAULT_CAPS),
        (anthropic_adapter.shape_request, quirks.caps_for("https://api.anthropic.com")),
    ],
)
@pytest.mark.parametrize("model", [PLAIN_MODEL, "fake-1", "claude-sonnet-5"])
def test_a_non_reasoning_request_is_byte_identical_to_before(
    shape: Any, caps: Any, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shaped with the shipped table and with no table at all, the two payloads are equal."""
    request = ChatRequest(
        model=model,
        system="be brief",
        messages=(Message(role="user", content="hi"),),
        max_tokens=gate.MAX_TOKENS,
    )
    after = shape(request, caps)
    no_headroom(monkeypatch)
    before = shape(request, caps)

    assert after == before
    assert after["max_tokens"] == gate.MAX_TOKENS


# ---- the gate: a real answer, not '' ----------------------------------------------------------


@pytest.mark.parametrize("build", [openai_adapter, anthropic_live])
def test_the_gate_gets_a_real_answer_from_a_reasoning_model(
    live_settings: Settings, build: Any
) -> None:
    endpoint = ReasoningEndpoint(answer="yes", answer_tokens=1)
    decision = gate.decide(
        build(live_settings, endpoint), "recall my notes", small_model=REASONING_MODEL
    )

    assert decision.answer == "yes"
    assert decision.failed_open is False
    assert decision.retrieve is True
    assert (
        endpoint.sent[0]["max_tokens"]
        == gate.MAX_TOKENS + quirks.model_caps_for(REASONING_MODEL).reasoning_headroom
    )


def test_without_headroom_the_same_gate_answers_nothing_and_fails_open(
    live_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub reproduces the defect, so the test above is not green for a stub-shaped reason."""
    no_headroom(monkeypatch)
    endpoint = ReasoningEndpoint(answer="yes", answer_tokens=1)
    decision = gate.decide(
        openai_adapter(live_settings, endpoint), "recall my notes", small_model=REASONING_MODEL
    )

    assert decision.answer == ""
    assert decision.failed_open is True


def test_the_gate_module_keeps_eight_tokens_and_names_no_model() -> None:
    """MUST rule 19: the gate learns nothing. Its ceiling is unchanged and no vendor is spelled."""
    source = (SRC / "memory" / "gate.py").read_text(encoding="utf-8")

    assert gate.MAX_TOKENS == 8
    assert "gpt-oss" not in source
    assert "reasoning" not in source.casefold()


# ---- the main reply: room after reasoning, and an honest report when it still runs out ---------


def long_reply() -> str:
    return " ".join(f"word{i}" for i in range(LONG_REPLY_TOKENS))


def main_request(model: str = REASONING_MODEL) -> ChatRequest:
    return ChatRequest(model=model, messages=(Message(role="user", content="explain the loop"),))


def empty_registry() -> ToolRegistry:
    return ToolRegistry([Tool(name="echo", handler=lambda arguments: "x", description="echo")])


def test_the_default_limit_leaves_room_for_the_answer_after_reasoning(
    live_settings: Settings,
) -> None:
    endpoint = ReasoningEndpoint(answer=long_reply(), answer_tokens=LONG_REPLY_TOKENS)
    result = run_loop(
        openai_adapter(live_settings, endpoint), empty_registry(), main_request(), limit=3
    )

    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.stop_reason == "end_turn"
    assert result.truncated is False
    assert result.reply == long_reply()


def test_a_reply_that_still_runs_out_is_reported_truncated(live_settings: Settings) -> None:
    """Headroom is a ceiling, not a promise: past it, ticket 077's report is what is seen."""
    headroom = quirks.model_caps_for(REASONING_MODEL).reasoning_headroom
    endpoint = ReasoningEndpoint(
        answer=long_reply(), answer_tokens=LONG_REPLY_TOKENS, reasoning=headroom + 1024
    )
    result = run_loop(
        openai_adapter(live_settings, endpoint), empty_registry(), main_request(), limit=3
    )

    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.stop_reason == "max_tokens"
    assert result.truncated is True


def test_evidence_before_and_after(
    live_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's answer and the main reply's stop reason on the stub, without and with headroom."""

    def observe() -> tuple[gate.Decision, str | None, int]:
        gate_endpoint = ReasoningEndpoint(answer="yes", answer_tokens=1)
        decision = gate.decide(
            openai_adapter(live_settings, gate_endpoint), "recall", small_model=REASONING_MODEL
        )
        reply_endpoint = ReasoningEndpoint(answer=long_reply(), answer_tokens=LONG_REPLY_TOKENS)
        result = run_loop(
            openai_adapter(live_settings, reply_endpoint), empty_registry(), main_request(), limit=3
        )
        return decision, result.stop_reason, int(reply_endpoint.sent[0]["max_tokens"])

    after = observe()
    with monkeypatch.context() as patch:
        patch.setattr(quirks, "MODELS", {})
        before = observe()

    assert (before[0].failed_open, before[1]) == (True, "max_tokens")
    assert (after[0].failed_open, after[1]) == (False, "end_turn")

    lines = [f"model: {REASONING_MODEL}, stub reasoning spend: {REASONING} tokens", ""]
    for label, (decision, stop, ceiling) in (("before", before), ("after", after)):
        lines += [
            f"{label}:",
            f"  gate answer:          {decision.answer!r} (failed_open={decision.failed_open})",
            f"  main reply stop:      {stop}",
            f"  main reply wire max:  {ceiling}",
            "",
        ]
    assert capture("reasoning-budget.txt", "\n".join(lines)).exists()


# ---- the tree: the loop and the taxonomy did not move ------------------------------------------


def test_the_loop_still_has_two_guardrails_and_the_taxonomy_holds() -> None:
    assert GUARDRAILS == (NO_MORE_TOOL_CALLS, ITERATION_LIMIT)
    assert {"run.finished", "loop.guardrail.tripped", "retrieval.gate.failed_open"} <= set(NAMES)


def test_no_model_id_is_branched_on_outside_quirks() -> None:
    """MUST rule 19 as a scan: the reasoning model's family is spelled in `quirks.py` alone."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "quirks.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "gpt-oss" in node.value
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []
