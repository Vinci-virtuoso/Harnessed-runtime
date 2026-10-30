"""The shared plumbing every eval file leans on. Waku's ``helpers.py``, ported.

The name is waku's, kept deliberately: fidelity to their layout is this ticket's premise, and a
reader who knows their suite should find this file where they expect it. Everything in it is one
of four things - the playback client, the response builders, the live-tier gate, or the factory
that builds an isolated harness - and nothing else belongs here.

**The structure is waku's; the types are ours.** Their block builders speak Anthropic's wire
shapes because their loop does. Harnessed normalises everything to ``providers/types.py``, so
:func:`text_response` and :func:`tool_response` return a :class:`ChatResponse` and
:class:`ScriptedClient` satisfies the existing ``Provider`` protocol. Anything else could not
drive ``run_loop`` at all.

:func:`make_harness` calls ``cli/run_cmd.py::open_session`` - the composition root that already
exists. Being able to do that is the whole reason this suite sits outside the package.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import pytest

from harnessed import config
from harnessed.cli.run_cmd import MemoryRun, Session, open_session, solve_with_memory
from harnessed.config import PROVIDER_KEY_VARS, SECRET_VARS
from harnessed.loop.engine import LoopResult
from harnessed.memory.gate import GATE_SCOPE
from harnessed.providers.base import Provider
from harnessed.providers.types import ChatRequest, ChatResponse, ToolCall

__all__ = [
    "ask_with_memory",
    "GATE_ANSWER",
    "ACTIVE_KEY_VAR",
    "ACTIVE_PROVIDER",
    "DATASET_PATH",
    "DOCUMENTED_ENV_VARS",
    "HAS_KEY",
    "MAX_ITERATIONS",
    "MAX_TOKENS",
    "OFFLINE_PROVIDER",
    "RUN_LIVE",
    "RUN_LIVE_VAR",
    "Case",
    "DatasetError",
    "ScriptExhaustedError",
    "ScriptedClient",
    "ask",
    "live_eval",
    "live_reason_for",
    "load_dataset",
    "make_harness",
    "parse_dataset",
    "text_response",
    "tool_response",
]

SUITE_ROOT: Final[Path] = Path(__file__).resolve().parent

#: The dataset the live tier parametrises over. One JSON object per line.
DATASET_PATH: Final[Path] = SUITE_ROOT / "dataset.jsonl"

#: The provider the offline tier names on purpose. FR-27 removed the default, so a suite that
#: named nothing would fail to build a provider at all.
OFFLINE_PROVIDER: Final = "fake"

#: The second gate on the live tier, beside a real key. Both must be open, and the skip reason
#: names both, because an operator who has one and not the other needs to know which.
RUN_LIVE_VAR: Final = "HARNESSED_RUN_LIVE_EVALS"

#: Every variable an isolated harness clears before building a session. The list is restated here
#: rather than imported from ``tests/conftest.py``: ``tests/`` is out of this ticket's scope, and
#: a suite that silently inherited a change to that tuple would be isolated by luck.
DOCUMENTED_ENV_VARS: Final[tuple[str, ...]] = (
    "HARNESSED_HOME",
    "HARNESSED_PROVIDER",
    "HARNESSED_MODEL",
    "HARNESSED_SMALL_MODEL",
    "HARNESSED_BASE_URL",
    "HARNESSED_TOKEN_BUDGET",
    "HARNESSED_MAX_ITERATIONS",
    "HARNESSED_EMBED_BASE_URL",
    "HARNESSED_EMBED_MODEL",
    RUN_LIVE_VAR,
    *sorted(SECRET_VARS),
)

#: A ceiling low enough that a runaway live run is a red test rather than a bill (ticket 033).
MAX_ITERATIONS: Final = 4

#: Room for a reasoning model to reach an answer. At 256 one live endpoint spent the whole
#: allowance thinking and returned no content at all (learnings, ticket 033).
MAX_TOKENS: Final = 768


# ---- the live-tier gate --------------------------------------------------------------------------


def _active_provider() -> str | None:
    """Whatever the operator configured, read through ``config.load`` and nowhere else."""
    return config.load().provider


#: The provider this machine is configured for, and the variable holding its key. Both may be
#: ``None`` on a fresh clone, which is exactly the state the live tier has to skip out of.
ACTIVE_PROVIDER: Final[str | None] = _active_provider()
ACTIVE_KEY_VAR: Final[str | None] = (
    None if ACTIVE_PROVIDER is None else PROVIDER_KEY_VARS.get(ACTIVE_PROVIDER)
)

#: Whether a **real** credential is configured. A provider whose key variable is ``None`` - ``fake``
#: replaying the fixture corpus, ``ollama`` on localhost - does not count, however successfully it
#: answers: a live tier that ran against FakeProvider would report that a model behaved well when
#: what behaved well was a recording. That is the exact failure FR-27 removed the default for.
HAS_KEY: Final[bool] = (
    ACTIVE_PROVIDER is not None
    and ACTIVE_KEY_VAR is not None
    and bool(os.environ.get(ACTIVE_KEY_VAR, "").strip())
)
RUN_LIVE: Final[bool] = os.environ.get(RUN_LIVE_VAR, "").strip() == "1"


def live_reason_for(provider: str | None, *, has_key: bool, run_live: bool) -> str:
    """The sentence a skipped live-tier test prints. It names every gate that is still shut.

    A reason that only said "live evals are off" would leave an operator with a key guessing which
    of the two switches they were missing, which is the failure this criterion exists to prevent.
    """
    shut: list[str] = []
    if not run_live:
        shut.append(f"set {RUN_LIVE_VAR}=1")
    if not has_key:
        key_var = None if provider is None else PROVIDER_KEY_VARS.get(provider)
        if key_var is not None:
            shut.append(f"configure provider {provider!r} and set {key_var}")
        else:
            # Either nothing is configured, or what is configured needs no credential at all -
            # ``fake`` and ``ollama``. Neither is a live endpoint, so both name the whole list.
            named = "a credentialled provider"
            if provider is not None:
                named = f"a credentialled provider ({provider!r} needs no key and is not live)"
            shut.append(f"configure {named} and set one of {', '.join(sorted(SECRET_VARS))}")
    if not shut:
        return "the live eval tier is open"
    return "the live eval tier is off: " + " and ".join(shut)


#: The reason a shut live tier prints, computed once from this machine's actual configuration.
LIVE_REASON: Final[str] = live_reason_for(ACTIVE_PROVIDER, has_key=HAS_KEY, run_live=RUN_LIVE)

_SKIP_UNLESS_LIVE: Final = pytest.mark.skipif(not (HAS_KEY and RUN_LIVE), reason=LIVE_REASON)


def live_eval(test: Any) -> Any:
    """Mark one test as the live tier: its own declared marker, plus the ``skipif`` that explains.

    It is deliberately a function rather than a composed ``MarkDecorator``. ``pytest.mark.x(y)``
    where ``y`` is another mark does **not** stack the two - pytest reads ``y`` as an *argument* to
    ``x`` and the inner mark is silently dropped, which is how a live tier ends up carrying the
    marker that exempts it from the socket guard while having lost the skip that was supposed to
    stop it from running at all.

    The marker is **not** ``pytest.mark.live``: ``addopts`` is ``-m "not live"``, which *deselects*
    rather than skips, and a deselected test never prints a reason.
    """
    return pytest.mark.live_eval(_SKIP_UNLESS_LIVE(test))


# ---- the playback client and its response builders -----------------------------------------------


class ScriptExhaustedError(RuntimeError):
    """A scripted exchange asked for one more turn than it was given.

    Outside the ``HarnessError`` tree on purpose, exactly like ``SocketGuardError``: the CLI
    catches ``HarnessError`` broadly, and a script that ran out has nowhere it should be able to
    hide. An ``IndexError`` out of a list subscript would be indistinguishable from a bug inside
    the loop, which is the confusion this class exists to end.
    """


def text_response(
    content: str, *, response_id: str = "resp_scripted", model: str = "scripted"
) -> ChatResponse:
    """One assistant turn that answers and asks for nothing. Guardrail 1 fires on it."""
    return ChatResponse(response_id=response_id, model=model, content=content)


def tool_response(
    name: str,
    arguments: Mapping[str, Any],
    *,
    call_id: str = "call_scripted",
    content: str = "",
    response_id: str = "resp_scripted",
    model: str = "scripted",
) -> ChatResponse:
    """One assistant turn that asks for a single tool. The loop dispatches it and comes back."""
    return ChatResponse(
        response_id=response_id,
        model=model,
        content=content,
        tool_calls=(ToolCall(id=call_id, name=name, arguments=dict(arguments)),),
        stop_reason="tool_use",
    )


#: What the scripted client answers the retrieval gate, out of the script's sight.
GATE_ANSWER: Final = "yes"


class ScriptedClient:
    """A ``Provider`` that plays back a fixed list of responses, in order, and keeps the requests.

    This is the offline tier's whole model. It has no fixtures, no predicates and no selection
    rule - a test that wants a particular exchange writes that exchange down, which is what makes
    a scripted eval readable next to the four facts it asserts.

    **The retrieval gate is answered here rather than from the script (ticket 044).** A real run
    asks the provider twice: once for the gate's one-word decision, then once per loop iteration.
    Before ticket 041 there was no gate call, so every script in this suite counts loop turns and
    nothing else. Serving the gate from the script would shift all of them by one and make each
    file's exchange read as though the loop asked a question it never asked.

    So a request carrying ``metadata["scope"] == "_gate"`` gets a fixed affirmative and **does not
    advance the script**. That is the same shape FakeProvider already uses - the committed corpus
    has a ``_gate/`` scope directory with one ``always`` variant answering ``yes`` - so the two
    offline providers now handle the gate the same way, and a scripted exchange stays a
    description of the loop. The gate's request is still recorded in :attr:`requests`, so a test
    that wants to assert on it still can.
    """

    def __init__(self, responses: Sequence[ChatResponse]) -> None:
        self._responses = list(responses)
        self._served = 0
        self.requests: list[ChatRequest] = []

    @property
    def loop_requests(self) -> list[ChatRequest]:
        """Every request the loop made, with the retrieval gate's own call filtered out.

        Use this rather than indexing :attr:`requests`. A run asks the provider for the gate's
        decision before it asks for anything else, so ``requests[0]`` is the gate's one-word
        question and not the loop's first turn - which is a subtle way for an assertion about the
        prompt, the tools or the transcript to be reading the wrong request entirely.
        """
        return [r for r in self.requests if r.metadata.get("scope") != GATE_SCOPE]

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if request.metadata.get("scope") == GATE_SCOPE:
            return text_response(GATE_ANSWER, model=request.model)
        if self._served >= len(self._responses):
            raise ScriptExhaustedError(
                f"the scripted client is exhausted: it was given {len(self._responses)} "
                f"response(s) and the loop asked for {self._served + 1}. Add the turn the "
                f"exchange actually needs rather than shortening the assertion."
            )
        served = self._responses[self._served]
        self._served += 1
        return served


# ---- the isolated harness ------------------------------------------------------------------------


def make_harness(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: Provider | None = None,
    provider: str | None = OFFLINE_PROVIDER,
    keep: Sequence[str] = (),
    agents_path: Path | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> Session:
    """One session, built by the composition root, sandboxed to ``home``.

    ``provider`` is passed as the ``--provider`` flag rather than set as a variable, so
    ``config._resolve_provider`` short-circuits at the first arm. The rest of ``config.load`` is
    isolated by chdir'ing into ``home`` before the session is built, so the operator's ``.env`` -
    the file arm - is never read either (finding 7).

    ``provider=None`` means "whatever this machine is configured for", which is what the live tier
    wants. It resolves to :data:`ACTIVE_PROVIDER` - read once, at import, before any of this
    clearing happened - and its key variable is restored alongside it. Clearing the environment and
    then asking for the operator's provider without giving the key back is how a live tier ends up
    raising ``ProviderNotConfiguredError`` the first time somebody opens both its gates, long after
    the offline suite went green.

    ``keep`` is the "unless the caller asks for it" half of the isolation rule. Every documented
    variable is cleared, then each name in ``keep`` is restored from the real environment - a live
    run needs its model id, and an offline run needs neither it nor anything else.

    ``client`` replaces the built provider on the returned session. The session is a frozen
    dataclass, so this is a ``replace`` rather than a mutation, and the tool registry, the bus and
    the connection are still the ones ``open_session`` composed. There is no second composition
    root here and there must never be one.
    """
    home.mkdir(parents=True, exist_ok=True)
    # Clearing the environment is only one arm of ``config.load``. The other is the ``.env`` file,
    # which ``_dotenv_values`` reads from the working directory - so the harness is chdir'd into
    # the empty sandbox home, and a stray ``.env`` in the repository root can no longer leak the
    # operator's live model into a run that is supposed to be deterministic (finding 7).
    monkeypatch.chdir(home)
    inherited = {name: os.environ.get(name) for name in DOCUMENTED_ENV_VARS}
    for name in DOCUMENTED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in keep:
        value = inherited.get(name)
        if value is not None:
            monkeypatch.setenv(name, value)

    resolved = ACTIVE_PROVIDER if provider is None else provider
    if resolved is not None:
        # Set as well as passed: ``judge_model`` and anything else that calls ``config.load`` for
        # itself sees the same provider this session was built on, rather than an empty variable.
        monkeypatch.setenv("HARNESSED_PROVIDER", resolved)
        key_var = PROVIDER_KEY_VARS.get(resolved)
        key = None if key_var is None else inherited.get(key_var)
        if key_var is not None and key is not None:
            monkeypatch.setenv(key_var, key)
    monkeypatch.setenv("HARNESSED_HOME", str(home.resolve()))
    monkeypatch.setenv("HARNESSED_MAX_ITERATIONS", str(max_iterations))

    session = open_session(
        provider=resolved,
        agents_path=home / "agents.toml" if agents_path is None else agents_path,
    )
    return session if client is None else replace(session, provider=client)


def ask(session: Session, prompt: str) -> LoopResult:
    """One prompt through the run the product actually does.

    The same call in both tiers, so the only difference between an offline case and a live one is
    which provider is behind the seam.

    **It composes nothing of its own (ticket 044).** It used to build its own ``ChatRequest`` with
    a bare system prompt, which was identical to what the command did until ticket 041 attached
    the retrieval gate, the token ceiling and the episode write-back to the run path. From that
    commit until this one, every case in this suite measured a run no operator could trigger, and
    nothing noticed because the difference was a default argument. There is one composition now
    and this calls it.
    """
    return ask_with_memory(session, prompt).result


def ask_with_memory(session: Session, prompt: str) -> MemoryRun:
    """The same run, with the assembled retrieval handed back as well as the reply.

    For the cases that grade what the model was *given* rather than what it said - whether the
    retrieved context carried the fact the task needed, and what it cost against the ceiling.
    """
    return solve_with_memory(session, prompt, max_tokens=MAX_TOKENS)


# ---- the dataset ---------------------------------------------------------------------------------


class DatasetError(RuntimeError):
    """``dataset.jsonl`` is malformed. Raised at load, which is collection, never mid-run.

    Outside the ``HarnessError`` tree for the same reason as the other two errors in this suite.
    """


@dataclass(frozen=True, slots=True)
class Case:
    """One line of ``dataset.jsonl``: what to ask, and what a correct answer looks like."""

    id: str
    input: str
    expect_tool: str | None = None
    expect_in_args: tuple[str, ...] = ()
    expect_min_tool_calls: int = 0
    setup_fact: Mapping[str, str] | None = None


def parse_dataset(text: str) -> tuple[Case, ...]:
    """Every case in ``text``, validated, or :class:`DatasetError` naming the line that is wrong.

    Validation is total rather than best-effort: a line the loader skipped would be a case the
    live tier silently stopped running, and a suite that quietly shrinks is worse than one that
    fails.
    """
    cases: list[Case] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        cases.append(_case(_object(line, number), number, seen))
    if not cases:
        raise DatasetError(f"{DATASET_PATH.name} declares no cases")
    return tuple(cases)


def load_dataset() -> tuple[Case, ...]:
    """The committed dataset, validated. Called at module scope, so a bad line fails collection."""
    return parse_dataset(DATASET_PATH.read_text(encoding="utf-8"))


def _object(line: str, number: int) -> Mapping[str, Any]:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"line {number} is not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise DatasetError(f"line {number} is not a JSON object")
    return parsed


def _case(raw: Mapping[str, Any], number: int, seen: set[str]) -> Case:
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise DatasetError(f"line {number} has no id")
    if case_id in seen:
        raise DatasetError(f"line {number} repeats the id {case_id!r}; ids must be unique")
    seen.add(case_id)

    prompt = raw.get("input")
    if not isinstance(prompt, str) or not prompt:
        raise DatasetError(f"line {number} ({case_id}) has no input")

    expect_tool = raw.get("expect_tool")
    if expect_tool is not None and (not isinstance(expect_tool, str) or not expect_tool):
        raise DatasetError(
            f"line {number} ({case_id}) has an expect_tool that is neither a tool name nor null"
        )

    return Case(
        id=case_id,
        input=prompt,
        expect_tool=expect_tool,
        expect_in_args=tuple(_needles(raw, number, case_id)),
        expect_min_tool_calls=_min_calls(raw, number, case_id),
        setup_fact=_setup_fact(raw, number, case_id),
    )


def _needles(raw: Mapping[str, Any], number: int, case_id: str) -> Iterator[str]:
    needles = raw.get("expect_in_args", [])
    if not isinstance(needles, list) or not all(isinstance(item, str) for item in needles):
        raise DatasetError(f"line {number} ({case_id}) expect_in_args must be a list of strings")
    yield from needles


def _min_calls(raw: Mapping[str, Any], number: int, case_id: str) -> int:
    minimum = raw.get("expect_min_tool_calls", 0)
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
        raise DatasetError(
            f"line {number} ({case_id}) expect_min_tool_calls must be a non-negative integer"
        )
    return minimum


def _setup_fact(raw: Mapping[str, Any], number: int, case_id: str) -> Mapping[str, str] | None:
    fact = raw.get("setup_fact")
    if fact is None:
        return None
    if not isinstance(fact, dict) or set(fact) != {"path", "content"}:
        raise DatasetError(
            f"line {number} ({case_id}) setup_fact must be an object with path and content"
        )
    return {"path": str(fact["path"]), "content": str(fact["content"])}
