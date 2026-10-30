"""Ticket 082: cached input tokens are recorded, not discarded.

Every provider Harnessed talks to reports how many input tokens were served from cache, each under
its own name: the OpenAI dialect (Groq) nests it in ``prompt_tokens_details.cached_tokens``,
DeepSeek flattens it to ``prompt_cache_hit_tokens``, Anthropic calls it ``cache_read_input_tokens``.
``translate.py`` is the only module that may know those names, so it folds all three onto one
field, ``Usage.cached_input_tokens``, and the field is a *breakdown* of ``input_tokens`` - a cached
token is still an input token, so ``total_tokens`` does not move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harnessed.providers import translate
from harnessed.providers.types import Usage

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "082" / "cached-usage.txt"


def openai_body(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl_082",
        "model": "m",
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": usage,
    }


def anthropic_body(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg_082",
        "model": "m",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": usage,
    }


GROQ = openai_body(
    {
        "prompt_tokens": 2048,
        "completion_tokens": 16,
        "prompt_tokens_details": {"cached_tokens": 1536},
    }
)
DEEPSEEK = openai_body(
    {
        "prompt_tokens": 2048,
        "completion_tokens": 16,
        "prompt_cache_hit_tokens": 1024,
        "prompt_cache_miss_tokens": 1024,
    }
)
ANTHROPIC = anthropic_body(
    {"input_tokens": 2048, "output_tokens": 16, "cache_read_input_tokens": 1792}
)


# ---- each dialect's name folds onto the one field ------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "body", "cached"),
    [
        ("openai", GROQ, 1536),
        ("openai", DEEPSEEK, 1024),
        ("anthropic", ANTHROPIC, 1792),
    ],
    ids=["groq-prompt_tokens_details", "deepseek-prompt_cache_hit", "anthropic-cache_read"],
)
def test_the_cache_hit_count_is_read_from_each_dialects_own_field(
    dialect: str, body: dict[str, Any], cached: int
) -> None:
    translated = (
        translate.from_openai_response(body)
        if dialect == "openai"
        else translate.from_anthropic_response(body)
    )
    assert translated.usage.cached_input_tokens == cached
    assert translated.usage.input_tokens == 2048


def test_an_evidence_line_per_dialect_shows_cached_beside_input() -> None:
    lines = []
    for label, response in (
        ("groq (openai dialect)", translate.from_openai_response(GROQ)),
        ("deepseek (openai dialect)", translate.from_openai_response(DEEPSEEK)),
        ("anthropic", translate.from_anthropic_response(ANTHROPIC)),
    ):
        usage = response.usage
        lines.append(
            f"{label}: input_tokens={usage.input_tokens} "
            f"cached_input_tokens={usage.cached_input_tokens} "
            f"output_tokens={usage.output_tokens} total_tokens={usage.total_tokens}"
        )
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert all("cached_input_tokens=0" not in line for line in lines)


# ---- absent or null is zero, never a crash -------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        openai_body({"prompt_tokens": 10, "completion_tokens": 2}),
        openai_body({"prompt_tokens": 10, "prompt_tokens_details": None}),
        openai_body({"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": None}}),
        openai_body({"prompt_tokens": 10, "prompt_cache_hit_tokens": None}),
        openai_body({"prompt_tokens": 10, "prompt_tokens_details": {}}),
    ],
    ids=["absent", "details-null", "cached-null", "deepseek-null", "details-empty"],
)
def test_an_openai_body_without_a_hit_count_reads_as_zero(body: dict[str, Any]) -> None:
    assert translate.from_openai_response(body).usage.cached_input_tokens == 0


@pytest.mark.parametrize(
    "usage",
    [{"input_tokens": 10}, {"input_tokens": 10, "cache_read_input_tokens": None}],
    ids=["absent", "null"],
)
def test_an_anthropic_body_without_a_hit_count_reads_as_zero(usage: dict[str, Any]) -> None:
    assert translate.from_anthropic_response(anthropic_body(usage)).usage.cached_input_tokens == 0


def test_the_field_defaults_to_zero() -> None:
    assert Usage().cached_input_tokens == 0


# ---- a breakdown of input, never an addition to it -----------------------------------------------


def test_cached_tokens_do_not_change_the_total() -> None:
    for response in (
        translate.from_openai_response(GROQ),
        translate.from_openai_response(DEEPSEEK),
        translate.from_anthropic_response(ANTHROPIC),
    ):
        assert response.usage.total_tokens == 2048 + 16


@pytest.mark.parametrize(
    ("serialize", "parse", "body"),
    [
        (translate.to_openai_response, translate.from_openai_response, GROQ),
        (translate.to_anthropic_response, translate.from_anthropic_response, ANTHROPIC),
    ],
    ids=["openai", "anthropic"],
)
def test_the_cached_count_survives_a_wire_round_trip(
    serialize: Any, parse: Any, body: dict[str, Any]
) -> None:
    once = parse(body)
    assert parse(serialize(once)).usage == once.usage
