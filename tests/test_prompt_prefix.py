"""Ticket 083: retrieved memory does not break the cacheable prefix (FR-11, FR-17).

Groq and DeepSeek cache only an exact prefix that starts at the first token. Until this ticket,
``solve_with_memory`` handed this run's retrieval to ``Session.system`` as the base instruction, so
every run's system prompt began with different text and no two runs shared a prefix at all.

Architecture section 7 moves the retrieval into the user turn, as a ``<retrieved_context>`` block
ahead of the task. The system prompt then holds only the active procedures, which change only when
a candidate is promoted, and is byte-identical from run to run.

The live check at the bottom is the measurement the change exists for. It is marked ``live``, so
the default selection never reaches the network.
"""

from __future__ import annotations

import ast
import inspect
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

from harnessed import shipped
from harnessed.cli import run_cmd
from harnessed.loop import prompt
from harnessed.loop.engine import LoopResult
from harnessed.loop.prompt import BASE_INSTRUCTION, SYNTHESIS_INSTRUCTION, build_system_prompt
from harnessed.memory import db, procedural, retrieval, semantic
from harnessed.providers import fake
from harnessed.providers.fake import FakeProvider
from harnessed.providers.jev import Choice, Decisions, Noul, Question
from harnessed.providers.types import ChatRequest, ChatResponse, Message

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_ROOT = REPO_ROOT / "src" / "harnessed" / "cli"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "083"
EVIDENCE_166 = REPO_ROOT / "evidence" / "166"

TASK = "ping"
FIRST_FACT = "ping is answered with pong in the committed corpus"
SECOND_FACT = "ping travels over loopback when the gateway serves it"
PROCEDURE = "Answer in one word."


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


def capture_166(name: str, text: str) -> Path:
    EVIDENCE_166.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_166 / name
    path.write_text(text, encoding="utf-8")
    return path


def build_user_turn(context: str, task: str) -> str:
    """Imported late so a missing seam is a red test, not a collection error."""
    from harnessed.loop import prompt

    return prompt.build_user_turn(context, task)  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def conn(clean_env: None, tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against the throwaway home, opened the way the CLI opens it."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def seen_requests(monkeypatch: pytest.MonkeyPatch) -> list[ChatRequest]:
    """What the loop was handed on the run path, without stopping it running."""
    captured: list[ChatRequest] = []
    real = run_cmd.run_loop

    def spy(model: object, registry: object, request: ChatRequest, **kwargs: object) -> LoopResult:
        captured.append(request)
        return real(model, registry, request, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(run_cmd, "run_loop", spy)
    return captured


def active_procedures(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(p.text for p in procedural.active(connection))


# ---------------------------------------------------------------------------
# 1. the seam: a pure function beside build_system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("context", ["", "   ", "\n\n\t"])
def test_an_empty_context_returns_the_task_unchanged(context: str) -> None:
    """An unretrieved request must be byte-identical to today's, user message included."""
    assert build_user_turn(context, TASK) == TASK


def test_a_context_is_a_tagged_block_ahead_of_the_task() -> None:
    """The exact shape architecture section 7 names, so a reader can find it in a transcript."""
    turn = build_user_turn("# Facts\n- one fact", TASK)

    assert turn == f"<retrieved_context>\n# Facts\n- one fact\n</retrieved_context>\n\n{TASK}"


# ---------------------------------------------------------------------------
# 2. two retrieved runs share the whole system prompt
# ---------------------------------------------------------------------------


def test_two_runs_with_different_memory_share_a_byte_identical_system_prompt(
    conn: sqlite3.Connection, seen_requests: list[ChatRequest]
) -> None:
    """The criterion the ticket exists for, asserted on the requests the loop was handed."""
    stored = procedural.put(conn, PROCEDURE, source="test")
    procedural.activate(conn, stored.proc_id, cand_id=None)

    semantic.put(conn, FIRST_FACT, source="test")
    first = run_cmd.solve_with_memory(run_cmd.open_session(), TASK)
    semantic.put(conn, SECOND_FACT, source="test")
    second = run_cmd.solve_with_memory(run_cmd.open_session(), TASK)

    assert first.assembled.text != second.assembled.text, "the two runs retrieved the same memory"
    one, two = seen_requests[0], seen_requests[1]
    assert (
        one.system == two.system == build_system_prompt(BASE_INSTRUCTION, active_procedures(conn))
    ), "bare open_session() never routes; its system prompt is BASE_INSTRUCTION regardless (179)"
    for request, run in ((one, first), (two, second)):
        user = request.messages[0].content
        assert run.assembled.text in user
        assert run.assembled.text not in request.system
        assert FIRST_FACT not in request.system
        assert user.endswith(TASK)
    assert SECOND_FACT not in one.messages[0].content
    assert SECOND_FACT in two.messages[0].content

    capture(
        "prefix.txt",
        "two `harnessed run ping` requests, different retrieved memory, same procedures\n\n"
        f"run 1 system prompt: {one.system!r}\n"
        f"run 2 system prompt: {two.system!r}\n"
        f"system prompts byte-identical: {one.system == two.system}\n\n"
        f"run 1 user turn:\n{one.messages[0].content}\n\n"
        f"run 2 user turn:\n{two.messages[0].content}\n",
    )


@dataclass
class _RetrievingDecider:
    """Routes ``groq_chat`` above its floor, with ``needs_memory`` above
    :data:`~harnessed.router.questions.NEEDS_MEMORY_THRESHOLD` and ``memory_kind="semantic"``
    (ticket 170), so a routed run below actually retrieves - unlike the committed ``ping``
    corpus's own fixed ``needs_memory=0.1`` (ticket 169)."""

    def decide(self, state: str, questions: tuple[Question, ...]) -> Decisions:
        return Decisions(
            answers={
                "route": Choice(
                    value="groq_chat",
                    confidence=Decimal("0.93"),
                    probabilities={"groq_chat": Decimal("0.93")},
                ),
                "needs_memory": Noul(probability=Decimal("0.9")),
                "memory_kind": Choice(
                    value="semantic",
                    confidence=Decimal("0.6"),
                    probabilities={"semantic": Decimal("0.6")},
                ),
            },
            id="gen-dec-test-170",
            model="typesafe/jev-1.13-20260917",
            usage={"input_tokens": 420, "output_tokens": 22},
            cost="0.0000158",
        )


def test_two_routed_runs_with_different_memory_share_a_byte_identical_system_prompt(
    conn: sqlite3.Connection, seen_requests: list[ChatRequest], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 166 (FR-105): the ticket-083 proof above, run on the routed path.

    ``open_session(routed=True)`` puts a Decider in front of the message; decision 60 says that
    once it lands on a Groq route, the loop it hands off to is v4's, byte for byte - so this is
    the same assertion, changing only which session opened the two runs.

    ``build_decider`` is stubbed to :class:`_RetrievingDecider` (pre-existing failure this ticket
    found and fixed, confirmed via ``git stash`` against the base commit): the committed ``ping``
    router fixture's own ``needs_memory=0.1`` is below threshold (ticket 169), so the real
    corpus-backed Decider skips retrieval on both calls and the two (empty) assemblies are never
    unequal - not a claim about the routed path, only about a fixture this test never meant to
    pin.
    """
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings: _RetrievingDecider())
    stored = procedural.put(conn, PROCEDURE, source="test")
    procedural.activate(conn, stored.proc_id, cand_id=None)

    semantic.put(conn, FIRST_FACT, source="test")
    first = run_cmd.solve_with_memory(run_cmd.open_session(routed=True), TASK)
    semantic.put(conn, SECOND_FACT, source="test")
    second = run_cmd.solve_with_memory(run_cmd.open_session(routed=True), TASK)

    assert first.assembled.text != second.assembled.text, "the two runs retrieved the same memory"
    one, two = seen_requests[0], seen_requests[1]
    assert (
        one.system == two.system == build_system_prompt(BASE_INSTRUCTION, active_procedures(conn))
    )
    for request, run in ((one, first), (two, second)):
        user = request.messages[0].content
        assert run.assembled.text in user
        assert run.assembled.text not in request.system
        assert FIRST_FACT not in request.system
        assert user.endswith(TASK)
    assert SECOND_FACT not in one.messages[0].content
    assert SECOND_FACT in two.messages[0].content

    outcomes = [
        row[0]
        for row in conn.execute(
            "SELECT outcome FROM decisions WHERE run_id IN (?, ?) ORDER BY run_id",
            (first.run_id, second.run_id),
        ).fetchall()
    ]
    assert outcomes == ["routed", "routed"], "both messages must actually take the routed path"

    capture_166(
        "prefix-routed.txt",
        "two `harnessed run ping` requests, both dispatched through a Decider (outcome=routed), "
        "different retrieved memory, same procedures\n\n"
        f"run 1 system prompt: {one.system!r}\n"
        f"run 2 system prompt: {two.system!r}\n"
        f"system prompts byte-identical: {one.system == two.system}\n\n"
        f"decisions.outcome for both runs: {outcomes}\n\n"
        f"run 1 user turn:\n{one.messages[0].content}\n\n"
        f"run 2 user turn:\n{two.messages[0].content}\n",
    )


def test_the_episode_records_the_raw_task_never_the_context_block(
    conn: sqlite3.Connection,
) -> None:
    """Retrieved memory must not compound into later episodes and come back as if it were new."""
    semantic.put(conn, FIRST_FACT, source="test")
    run = run_cmd.solve_with_memory(run_cmd.open_session(), TASK)

    assert run.assembled.text, "nothing was retrieved, so this test proves nothing"
    (text,) = (
        row[0]
        for row in conn.execute(
            "SELECT text FROM mem_episodic WHERE run_id = ?", (run.run_id,)
        ).fetchall()
    )
    assert text.startswith(f"prompt: {TASK}\n")
    assert "<retrieved_context>" not in text
    assert FIRST_FACT not in text


# ---------------------------------------------------------------------------
# 3. the unretrieved run, and the eval and improve paths, are unchanged
# ---------------------------------------------------------------------------


def test_an_unretrieved_run_sends_exactly_the_request_it_always_sent(
    conn: sqlite3.Connection, seen_requests: list[ChatRequest]
) -> None:
    """Empty stores: the system prompt and the user message are both what they were before."""
    stored = procedural.put(conn, PROCEDURE, source="test")
    procedural.activate(conn, stored.proc_id, cand_id=None)

    run = run_cmd.solve_with_memory(run_cmd.open_session(), TASK)

    assert run.assembled.text == ""
    (request,) = seen_requests
    assert request.system == build_system_prompt(BASE_INSTRUCTION, active_procedures(conn))
    assert len(request.messages) == 1
    assert request.messages[0].content == TASK


def test_session_system_takes_no_context() -> None:
    """There is no argument left to pass retrieval through, so no caller can put it back."""
    parameters = list(inspect.signature(run_cmd.Session.system).parameters)

    assert parameters == ["self"]


def test_every_cli_call_to_system_passes_nothing() -> None:
    """Scanned over ``cli/`` as a whole: eval, gate and run all build the same prefix."""
    offenders: list[str] = []
    for path in sorted(CLI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "system"
                and (node.args or node.keywords)
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == []


# ---------------------------------------------------------------------------
# 4. the memory still reaches the model, under the budget, and is still reported
# ---------------------------------------------------------------------------


def test_retrieved_memory_rides_under_the_budget_and_the_event_reports_it(
    conn: sqlite3.Connection, seen_requests: list[ChatRequest]
) -> None:
    """FR-11's ceiling and its event are untouched by where the text lands."""
    semantic.put(conn, FIRST_FACT, source="test")
    session = run_cmd.open_session()

    run = run_cmd.solve_with_memory(session, TASK)

    assert run.assembled.text
    assert run.assembled.tokens <= run.assembled.budget == session.settings.retrieval_token_budget
    assert FIRST_FACT in seen_requests[0].messages[0].content
    (tokens,) = (
        row[0]
        for row in conn.execute(
            "SELECT json_extract(payload, '$.tokens') FROM events WHERE run_id = ? AND name = ?",
            (run.run_id, retrieval.ASSEMBLED),
        ).fetchall()
    )
    assert tokens == run.assembled.tokens


# ---------------------------------------------------------------------------
# 5. live: the second of two runs reads its prefix from the provider's cache
# ---------------------------------------------------------------------------

#: Which provider the live check measures. Groq by default, overridable because Groq's free tier
#: returns no cache field at all (three identical 1,469-token requests, `prompt_tokens_details:
#: null`, 2026-09-13), so the measurement has to be taken somewhere that reports one. DeepSeek
#: caches automatically for every account and reports `prompt_cache_hit_tokens`.
LIVE_PROVIDER = os.environ.get("HARNESSED_LIVE_PROVIDER", "groq")
LIVE_MAX_TOKENS = 768

#: Groq's minimum cacheable prefix is 128 to 1024 tokens by model, so a one-line procedure could
#: sit under it and report zero for a reason that has nothing to do with placement. This is a
#: procedure set of the length a harness accumulates after some promotions.
LONG_PROCEDURES = tuple(
    f"Rule {n}: when a question names a network probe such as ping, answer with the single word "
    f"the probe expects, never explain the protocol, never add punctuation beyond what the word "
    f"itself needs, and never repeat the question back to the operator in any form."
    for n in range(1, 41)
)


class _UsageSpy:
    """Records the loop's responses, leaving the gate's own small-model request out."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.responses: list[ChatResponse] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        response: ChatResponse = self._inner.complete(request)  # type: ignore[attr-defined]
        if request.metadata.get("scope") != "_gate":
            self.responses.append(response)
        return response


@pytest.mark.live
def test_the_second_retrieved_run_reads_cached_input_tokens(
    tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 082's counter, two consecutive runs, different memory, one caching provider."""
    # Deleting HARNESSED_MODEL does not reach the provider's default: config.py also reads .env,
    # whose model then wins. A run on a provider other than the operator's configured one therefore
    # names its model explicitly, through HARNESSED_LIVE_MODEL.
    live_model = os.environ.get("HARNESSED_LIVE_MODEL")
    if live_model:
        monkeypatch.setenv("HARNESSED_MODEL", live_model)
    else:
        monkeypatch.delenv("HARNESSED_MODEL", raising=False)
    monkeypatch.delenv("HARNESSED_BASE_URL", raising=False)
    monkeypatch.setenv("HARNESSED_PROVIDER", LIVE_PROVIDER)
    first_session = run_cmd.open_session()
    if first_session.settings.api_key_for(LIVE_PROVIDER) is None:
        pytest.skip(f"no live key for {LIVE_PROVIDER}")
    # The active set is a singleton, so the long set rides in as one procedure.
    one = procedural.put(first_session.conn, "\n".join(LONG_PROCEDURES), source="test")
    procedural.activate(first_session.conn, one.proc_id, cand_id=None)

    lines: list[str] = []
    for fact in (FIRST_FACT, SECOND_FACT):
        semantic.put(first_session.conn, fact, source="test")
        session = run_cmd.open_session()
        spy = _UsageSpy(session.provider)
        run_cmd.solve_with_memory(
            replace(session, provider=spy),  # type: ignore[arg-type]
            TASK,
            max_tokens=LIVE_MAX_TOKENS,
        )
        usage = spy.responses[0].usage
        lines.append(
            f"{fact!r}: input_tokens={usage.input_tokens} "
            f"cached_input_tokens={usage.cached_input_tokens} system_chars={len(session.system())}"
        )

    capture(
        "prefix-live.txt",
        f"two consecutive live runs on {LIVE_PROVIDER}, different retrieved memory\n"
        + "\n".join(lines)
        + "\n",
    )
    second_cached = int(lines[1].split("cached_input_tokens=")[1].split()[0])
    assert second_cached > 0, "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. FakeProvider keys a retrieved run on its bare task
# ---------------------------------------------------------------------------


def _request(content: str, **metadata: str) -> ChatRequest:
    return ChatRequest(
        model="fake", messages=(Message(role="user", content=content),), metadata=metadata
    )


def test_the_fake_scope_of_a_retrieved_turn_is_the_scope_of_the_bare_task() -> None:
    """The context block moves nothing: the same task reaches the same fixture directory."""
    turn = build_user_turn("# Facts\n- ping is answered with pong", TASK)

    assert turn != TASK
    assert FakeProvider.scope_of(_request(turn)) == FakeProvider.scope_of(_request(TASK))


def test_an_explicit_scope_still_wins_over_a_retrieved_turn() -> None:
    turn = build_user_turn("# Facts\n- one fact", TASK)

    assert FakeProvider.scope_of(_request(turn, scope="_gate")) == "_gate"


def test_a_context_block_that_is_not_leading_is_part_of_the_task() -> None:
    """Only a leading block is stripped; a task that quotes the tag later keeps its own scope."""
    quoted = f"explain {prompt.CONTEXT_OPEN}x{prompt.CONTEXT_CLOSE}"

    assert FakeProvider.scope_of(_request(quoted)) != FakeProvider.scope_of(_request("explain "))


def test_the_fake_markers_are_the_prompt_markers() -> None:
    """``providers/`` may not import ``loop/``, so the literals are pinned equal here instead."""
    assert fake.CONTEXT_OPEN == prompt.CONTEXT_OPEN
    assert fake.CONTEXT_CLOSE == prompt.CONTEXT_CLOSE


@pytest.mark.parametrize("instruction", [BASE_INSTRUCTION, SYNTHESIS_INSTRUCTION])
def test_the_base_instruction_satisfies_no_fake_provider_trait(instruction: str) -> None:
    """A base that matched a fixture predicate would make every candidate look promoted (FR-25).

    The trait strings are read off the committed fixture indexes, so a trait added later is
    checked without editing this test. Ticket 179 deleted the v4 constant this test used to
    parametrize over too; ``BASE_INSTRUCTION`` is the one base instruction left to pin.
    """

    fixtures = shipped.fake_wire()
    traits: list[str] = []
    for index in fixtures.rglob("_index.yaml"):
        in_list = False
        for line in index.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("system_contains_all:", "system_matches:")):
                in_list = True
                continue
            if in_list and stripped.startswith("- "):
                traits.append(stripped[2:].strip().strip('"').strip("'"))
            else:
                in_list = False
    assert traits, "no trait strings found, so this test proves nothing"

    folded = " ".join(instruction.split()).casefold()
    matched = [t for t in traits if t.casefold() in folded]
    assert matched == []


@pytest.mark.parametrize("instruction", [BASE_INSTRUCTION, SYNTHESIS_INSTRUCTION])
def test_base_instruction_has_no_mention_of_delegation(instruction: str) -> None:
    """Ticket 168, AC1: with routing on, ``BASE_INSTRUCTION`` carries 0 mentions of delegation.

    A Groq route delegates from code (decision 60); the model is never given the tool at all, so
    the sentence telling it to call one would be a dangling instruction naming a tool that
    ``routed_registry`` never registers.
    """
    from harnessed.tools.builtin.delegate import TOOL_NAME

    folded = instruction.casefold()
    assert TOOL_NAME not in instruction
    assert "delegat" not in folded
    assert "external agent" not in folded


def test_the_synthesis_instruction_is_a_second_static_prefix() -> None:
    """Ticket 199: the chain's closing pass has its own static base, never the task-doing one."""
    first = BASE_INSTRUCTION.split(". ", 1)[0]
    assert SYNTHESIS_INSTRUCTION != BASE_INSTRUCTION
    assert SYNTHESIS_INSTRUCTION.startswith(first + ". ")
    for name in ("retrieved_context", "fs_read", "fs_write"):
        assert name in SYNTHESIS_INSTRUCTION
    assert "cannot access" in SYNTHESIS_INSTRUCTION, "it forbids saying what it cannot access"
    assert "You have no web access" not in SYNTHESIS_INSTRUCTION
