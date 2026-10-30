"""FR-28 - the same loop, unchanged, against real endpoints.

Every other provider test in this repository runs against the recorded corpus under
``tests/fixtures/wire/``. Those prove the translation is self-consistent; they cannot prove it
matches what a real endpoint sends back. This file is the only place that asks a live endpoint,
and it is the reason the ``live`` marker exists.

**This costs real money and runs on the operator's key.** Every prompt here is trivial, every
``max_tokens`` is small, and every ``run_loop`` is given an explicit low ceiling rather than
``Settings.max_iterations`` - a runaway loop against a live endpoint is a bill, not a red test.

Two things about the shape of the module:

* The whole file carries ``pytestmark = pytest.mark.live``, so the ticket's ``verify`` command
  (``-m live``) selects all of it and the default selection (``-m "not live"``) deselects all of
  it. A test here that forgot the marker would be outside its own gate on one run and inside the
  offline suite on the other.
* The ``(provider, model)`` pairs are a table **in this file**, not a variable.
  ``HARNESSED_MODEL`` is a single global and there is no per-provider default model anywhere in
  ``providers/``, so one variable cannot serve two endpoints - pointing both at it sends one
  vendor's model id to the other and earns a 400. These tests therefore delete both
  ``HARNESSED_MODEL`` and ``HARNESSED_BASE_URL`` for their duration and name the model on the
  request.

No vendor SDK is imported here, and none may be: ``test_provider_quirks.py`` sweeps ``tests/``
as well as ``src/`` for that import. A live endpoint that refuses arrives as an ordinary test
failure, which is the correct outcome for a smoke test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from conftest import DOCUMENTED_ENV_VARS
from harnessed import config
from harnessed.loop import NO_MORE_TOOL_CALLS, run_loop
from harnessed.providers import registry, translate
from harnessed.providers.base import Provider
from harnessed.providers.types import ChatRequest, Message, ToolCall
from harnessed.tools.registry import Tool, ToolRegistry

pytestmark = pytest.mark.live

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOOP_PACKAGE = "src/harnessed/loop"
PROVIDERS_PACKAGE = "src/harnessed/providers/"

#: Set by :func:`run_child_pytest` in the child process. The skip-reason test below spawns this
#: very file, so without a sentinel the child would spawn a grandchild and so on.
CHILD_SENTINEL = "HARNESSED_LIVE_CHILD"


@dataclass(frozen=True, slots=True)
class LivePair:
    """One endpoint and one model id it actually serves.

    The pair is the unit because neither half is useful alone: a provider without a model id it
    serves is a 400, and a model id without its provider has no endpoint to reach.
    """

    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


#: The two OpenAI-compatible endpoints this ticket proves the loop against, each with the
#: cheapest tool-capable model it serves. Groq verified live on 2026-08-22, DeepSeek re-verified on
#: 2026-09-13 after it retired ``deepseek-v4-flash`` (ticket 086); the ids are spelled with the
#: vendors' own casing, which is lowercase on one of them.
LIVE_PAIRS: tuple[LivePair, ...] = (
    LivePair("groq", "openai/gpt-oss-20b"),
    LivePair("deepseek", "deepseek-flash"),
)

#: The identical task both endpoints are asked. Trivial on purpose, and never asserted on by
#: wording - only that something came back.
PLAIN_TASK = "Reply with a single short sentence naming any colour."

#: The task that cannot be answered without the tool below.
TOOL_TASK = (
    "Call the add_numbers tool with a=2 and b=3, then reply with the number it returned. "
    "You have no other way to add."
)

SYSTEM_PROMPT = "You are a terse assistant. Answer in one short sentence."

#: Small enough to keep a smoke run cheap, large enough that a reasoning model still reaches an
#: answer. A ceiling so low that the model stops mid-thought would fail on cost control rather
#: than on the behaviour under test - which is exactly what 256 did here on the first run: one
#: endpoint spent the whole allowance reasoning and returned ``stop_reason="max_tokens"`` with
#: no content and no tool call. The headroom is the fix; loosening the assertion would not be.
MAX_TOKENS = 768

#: The loop ceiling for a live run, passed explicitly rather than read from ``Settings`` so a
#: misconfigured ``HARNESSED_MAX_ITERATIONS`` cannot turn a smoke test into a bill.
LIVE_LIMIT = 4


# ---- building a live provider -------------------------------------------------------------------


def key_var(pair: LivePair) -> str:
    """The variable holding this pair's key. Asked of the registry, never spelled here."""
    var = registry.key_var_for(pair.provider)
    assert var is not None, f"{pair.provider} needs no key and cannot be a live pair"
    return var


def live_settings(pair: LivePair, monkeypatch: pytest.MonkeyPatch) -> config.Settings:
    """Settings for one pair, or a skip naming the variable the operator has not set.

    The environment is read through ``config.load`` and nowhere else (All AI agents MUST rule 3);
    the two globals that would override the pair table are deleted first, so ``settings.model``
    and ``settings.base_url`` are both ``None`` and the pair is the only thing that can decide
    which model reaches which endpoint.
    """
    monkeypatch.delenv("HARNESSED_MODEL", raising=False)
    monkeypatch.delenv("HARNESSED_BASE_URL", raising=False)
    settings = config.load(provider=pair.provider)
    if settings.api_key_for(pair.provider) is None:
        pytest.skip(f"no live key for {pair.provider}: set {key_var(pair)} to run this test")
    return settings


def live_provider(pair: LivePair, monkeypatch: pytest.MonkeyPatch) -> Provider:
    """The constructed adapter for one pair. Skips rather than fails when the key is absent."""
    return registry.create(live_settings(pair, monkeypatch))


def request_for(pair: LivePair, task: str, *, tools: ToolRegistry | None = None) -> ChatRequest:
    """One normalized request. ``model`` comes from the pair, never from the environment.

    ``tools`` is explicit because only ``run_loop`` fills the field for itself. A direct
    ``complete`` that asks for a tool it never declared is not a harness bug the endpoint should
    have to tolerate, and one of these two answers it with a 400 saying so.
    """
    return ChatRequest(
        model=pair.model,
        messages=(Message(role="user", content=task),),
        system=SYSTEM_PROMPT,
        tools=() if tools is None else tuple(tools.schemas()),
        max_tokens=MAX_TOKENS,
    )


# ---- the one tool these tests offer --------------------------------------------------------------

ADD_PARAMETERS: Mapping[str, Any] = {
    "type": "object",
    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
    "required": ["a", "b"],
}


def adding_registry() -> tuple[ToolRegistry, list[Mapping[str, Any]]]:
    """A registry with one tool, plus the list its handler appends every call it receives.

    The list is what makes "the tool executed" observable without asserting on the model's
    wording: either the handler ran or it did not.
    """
    calls: list[Mapping[str, Any]] = []

    def add_numbers(arguments: Mapping[str, Any]) -> str:
        calls.append(dict(arguments))
        return str(arguments["a"] + arguments["b"])

    tool = Tool(
        name="add_numbers",
        handler=add_numbers,
        description="Add two numbers and return the sum.",
        parameters=ADD_PARAMETERS,
    )
    return ToolRegistry([tool]), calls


# ---- the marker discipline (AC 1) ----------------------------------------------------------------


def child_environment(**overrides: str) -> dict[str, str]:
    """This process's environment minus every documented variable, plus ``overrides``.

    Subtracting rather than starting from an empty dict is deliberate: on Windows a minimal
    environment has no ``SystemRoot`` and no ``PATH``, and the child interpreter would fail to
    start for a reason that has nothing to do with the assertion (learnings, ticket 030).
    """
    env = {name: value for name, value in os.environ.items() if name not in DOCUMENTED_ENV_VARS}
    env.update(overrides)
    return env


def child_result(*args: str, env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """Run pytest in a child process under this repository's own ini options.

    ``-c pyproject.toml`` is what carries ``addopts``, the declared markers and
    ``--strict-markers``; without it the child would be answering a different question than the
    one the parent asked.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(PYPROJECT), "-p", "no:cacheprovider", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=None if env is None else dict(env),
    )
    return completed.returncode, completed.stdout + completed.stderr


def live_test_ids() -> tuple[str, ...]:
    """Every test this module contributes, parametrisation expanded.

    The deselection assertion counts them rather than hardcoding a number, so a live test added
    later cannot quietly leave part of this file inside the offline selection.
    """
    ids: list[str] = []
    for name, value in sorted(globals().items()):
        if not name.startswith("test_") or not callable(value):
            continue
        cases = 1
        for mark in getattr(value, "pytestmark", ()):
            if mark.name == "parametrize":
                cases *= len(mark.args[1])
        ids.extend(f"{name}[{index}]" for index in range(cases))
    return tuple(ids)


def test_every_live_test_is_skipped_with_a_named_variable_when_no_key_is_present() -> None:
    """Strip every documented variable and this file must skip, not fail and not spend money.

    The child is this same file, so ``CHILD_SENTINEL`` guards against a grandchild: this test
    returns immediately when it is set, and every other test in the file is either key-gated or
    free to run.
    """
    if os.environ.get(CHILD_SENTINEL):
        pytest.skip("running as the child of the skip-discipline test")

    code, output = child_result(
        "-m",
        "live",
        "-rs",
        "-q",
        "tests/test_live_providers.py",
        env=child_environment(**{CHILD_SENTINEL: "1"}),
    )
    assert code == 0, output
    for pair in LIVE_PAIRS:
        assert key_var(pair) in output, output


def test_the_default_selection_deselects_this_whole_file() -> None:
    """The offline suite is untouched: under the default ``-m "not live"`` nothing here is run."""
    if os.environ.get(CHILD_SENTINEL):
        pytest.skip("running as the child of the skip-discipline test")

    code, output = child_result("--collect-only", "-q", "tests/test_live_providers.py")

    # Exit 5 is pytest's "no tests collected", and here it is the assertion rather than a
    # nuisance: under the default selection this file contributes nothing at all, so nothing in
    # it can change the offline suite's result. A 0 would mean something here had run.
    assert code == 5, output
    assert f"({len(live_test_ids())} deselected)" in output, output


# ---- the loop against a real endpoint (AC 2 and AC 3) --------------------------------------------


@pytest.mark.parametrize("pair", LIVE_PAIRS, ids=str)
def test_the_identical_task_answers_and_stops_on_guardrail_one(
    pair: LivePair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same task, the same loop, two endpoints, a non-empty reply from each.

    The registry is empty, so the model has nothing to ask for and guardrail 1 fires on the
    first turn. Nothing is asserted about what the model said - only that it said something and
    that the run ended for the reason the loop names.
    """
    model = live_provider(pair, monkeypatch)
    result = run_loop(model, ToolRegistry(), request_for(pair, PLAIN_TASK), limit=LIVE_LIMIT)

    assert result.reply.strip() != ""
    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.iterations == 1
    assert result.tool_calls == ()


# ---- tool-result shaping on a real wire (AC 4) ---------------------------------------------------


@pytest.mark.parametrize("pair", LIVE_PAIRS, ids=str)
def test_a_live_tool_call_executes_and_its_result_is_accepted_back(
    pair: LivePair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real endpoint asks for the tool, the loop runs it, and the endpoint accepts the result.

    This is the assertion the fixture corpus cannot make. ``to_openai_tool_result_messages``
    produces N ``role:"tool"`` messages and the fixtures agree with that shape by construction;
    only a real endpoint can refuse it. A second turn happening at all is the proof that it did
    not refuse.
    """
    model = live_provider(pair, monkeypatch)
    tools, executed = adding_registry()

    result = run_loop(model, tools, request_for(pair, TOOL_TASK), limit=LIVE_LIMIT)

    assert executed, f"{pair} never called the tool: {result.reply!r}"
    assert executed[0]["a"] == 2
    assert executed[0]["b"] == 3
    assert result.tool_calls != ()
    assert result.iterations >= 2
    assert result.guardrail == NO_MORE_TOOL_CALLS
    assert result.reply.strip() != ""


# ---- malformed arguments are an error result, never an exception (AC 5) --------------------------

#: Deliberately unbalanced. What a weak model emits when it truncates its own arguments.
MALFORMED_ARGUMENTS = '{"a": 2, "b":'


def corrupt_tool_arguments(response_payload: dict[str, Any]) -> dict[str, Any]:
    """Replace the arguments of every tool call in one wire body with malformed JSON.

    The malformation is injected rather than waited for. A model that reliably emits broken JSON
    on demand does not exist, so the honest version of this criterion is: take the body a real
    endpoint produced, break exactly the one field the criterion is about, and re-parse it with
    the same ``translate`` function the adapter uses. Everything else in the body - the call id,
    the tool name, the finish reason - is what the endpoint actually sent, and the retry leg
    below goes back over the real wire.
    """
    for choice in response_payload["choices"]:
        for call in choice["message"].get("tool_calls") or ():
            call["function"]["arguments"] = MALFORMED_ARGUMENTS
    return response_payload


@pytest.mark.parametrize("pair", LIVE_PAIRS[:1], ids=str)
def test_malformed_arguments_come_back_as_a_retryable_error_result(
    pair: LivePair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broken arguments are a sentence the model reads, not a traceback the operator reads.

    Three legs: a real endpoint asks for the tool; its own answer is re-parsed with the
    arguments broken and dispatched, which must yield an error ``ToolResult`` and never raise;
    and the resulting transcript - assistant turn, tool error, all of it - goes back to the same
    live endpoint, which must accept it and answer.
    """
    model = live_provider(pair, monkeypatch)
    tools, executed = adding_registry()
    first = model.complete(request_for(pair, TOOL_TASK, tools=tools))
    assert first.tool_calls != (), f"{pair} never asked for the tool: {first.content!r}"

    broken = translate.from_openai_response(
        corrupt_tool_arguments(translate.to_openai_response(first))
    )
    call: ToolCall = broken.tool_calls[0]
    assert call.parse_error is not None
    assert call.arguments == {}

    result = tools.dispatch(call)

    assert result.is_error
    assert result.call_id == call.id
    assert "JSON" in result.content
    assert executed == [], "a call that never parsed must not reach the handler"

    retry = run_loop(
        model,
        tools,
        ChatRequest(
            model=pair.model,
            messages=(
                Message(role="user", content=TOOL_TASK),
                Message(role="assistant", content=broken.content, tool_calls=broken.tool_calls),
                Message(role="tool", tool_results=(result,)),
            ),
            system=SYSTEM_PROMPT,
            max_tokens=MAX_TOKENS,
        ),
        limit=LIVE_LIMIT,
    )

    assert retry.reply.strip() != ""


# ---- the loop did not move (AC 3 and AC 6) -------------------------------------------------------


def git_diff_stat(*paths: str) -> str:
    """``git diff --stat HEAD`` over ``paths``, stripped.

    ``HEAD`` rather than the bare index, so a change that has been staged cannot hide from the
    assertion below.
    """
    completed = subprocess.run(
        ["git", "diff", "--stat", "HEAD", "--", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout.strip()


def test_the_loop_package_did_not_change() -> None:
    """The abstraction held, or the ticket is blocked rather than patched.

    If a live endpoint had needed ``loop/engine.py`` to move, ``providers/`` was not the seam it
    claims to be, and this assertion is the one that says so.
    """
    assert git_diff_stat(LOOP_PACKAGE) == ""


def test_nothing_outside_the_provider_quarantine_changed() -> None:
    """Making two live endpoints work costs entries in ``providers/`` and nothing else."""
    changed = [
        line.split("|")[0].strip()
        for line in git_diff_stat("src/harnessed").splitlines()
        if "|" in line
    ]
    assert [path for path in changed if not path.startswith(PROVIDERS_PACKAGE)] == []
