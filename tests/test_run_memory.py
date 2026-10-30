"""Ticket 041: the run path uses the memory it has, and writes back what happened.

FR-11 and FR-12 are asserted at module level in ``test_retrieval.py`` and
``test_retrieval_gate.py`` - assembly under a ceiling, the gate consulted once, fail-open. Both
were true of the modules and false of the product: ``harnessed run`` never called either one, so
``--token-budget`` resolved to a value nothing read and the retrieval layer sat between three
stores and nobody.

This file asserts the wiring rather than the modules. Everything here runs through the real CLI
against the committed fixture corpus, because a test that called ``assemble`` itself would pass
today, and today is exactly the state this ticket exists to change.

The prompt assertions matter as much as the memory ones. ``Session.system()`` is the prompt the
flywheel's baseline measurement builds, and ``FakeProvider`` selects fixtures on system-prompt
text, so retrieval on the eval path would make the FR-25 proof depend on database contents. The
wiring lives in ``run()`` and the byte-identical assertion below is what keeps it there.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessed import config
from harnessed.cli import run_cmd
from harnessed.cli.app import app
from harnessed.errors import DecisionUnreachable, ProviderError
from harnessed.events.bus import EventBus
from harnessed.loop.engine import LoopResult
from harnessed.memory import db, gate, procedural, retrieval, semantic
from harnessed.providers.fake import FakeProvider
from harnessed.providers.jev import Choice, Decisions, Noul, Question
from harnessed.providers.types import ChatRequest
from harnessed.tools import default_registry

runner = CliRunner()

PROMPT = "ping"
REPLY_FIXTURE = "pong"

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "041"
EVIDENCE_166 = REPO_ROOT / "evidence" / "166"
EVIDENCE_169 = REPO_ROOT / "evidence" / "169"
EVIDENCE_170 = REPO_ROOT / "evidence" / "170"


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


def capture_166(name: str, text: str) -> Path:
    EVIDENCE_166.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_166 / name
    path.write_text(text, encoding="utf-8")
    return path


def capture_169(name: str, text: str) -> Path:
    EVIDENCE_169.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_169 / name
    path.write_text(text, encoding="utf-8")
    return path


def capture_170(name: str, text: str) -> Path:
    EVIDENCE_170.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_170 / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against the throwaway home, opened the way the CLI opens it."""
    connection = db.connect()
    yield connection
    connection.close()


class _AlwaysFallbackDecider:
    """A Decider whose own call always fails a `DecisionError` (decision 59), forcing a routed
    session onto the fallback path - the v4 gate-then-loop, unchanged - without ever reaching the
    network. Ticket 179 deleted the flag that used to reach the same v4 path by turning routing
    off entirely; these tests want the v4 gate specifically, not "no Decider built"."""

    def decide(self, state: str, questions: object) -> object:
        raise DecisionUnreachable("stubbed: no live router in this test")


@pytest.fixture
def seen_requests(monkeypatch: pytest.MonkeyPatch) -> list[ChatRequest]:
    """Capture what the loop was handed, without stopping it running.

    ``run_loop`` is patched in ``run_cmd``'s namespace rather than in ``loop.engine``, so only
    the CLI's call is observed and the eval and improve paths keep the real one.
    """
    captured: list[ChatRequest] = []
    real = run_cmd.run_loop

    def spy(model: object, registry: object, request: ChatRequest, **kwargs: object) -> LoopResult:
        captured.append(request)
        return real(model, registry, request, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(run_cmd, "run_loop", spy)
    return captured


def episodes(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Every episode in the store, oldest first. There is no ``episodic.all``, deliberately."""
    rows = connection.execute(
        "SELECT run_id, kind, text FROM mem_episodic ORDER BY rowid"
    ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def event_names(connection: sqlite3.Connection) -> list[str]:
    """Every event name published, in sequence order."""
    rows = connection.execute("SELECT name FROM events ORDER BY run_id, seq").fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# 1. the gate is consulted, exactly once, on the run path
# ---------------------------------------------------------------------------


def test_the_run_path_consults_the_gate_exactly_once(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-12 says at most once per run. A counting spy, not an at-least-once assertion."""
    calls: list[dict[str, object]] = []
    real = gate.assemble_if_gated

    def spy(model: object, connection: object, query: str, **kwargs: object) -> object:
        calls.append({"query": query, **kwargs})
        return real(model, connection, query, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(run_cmd.gate, "assemble_if_gated", spy)
    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["query"] == PROMPT
    assert calls[0]["budget"] == 4000
    assert str(calls[0]["run_id"]).startswith("run_")
    capture(
        "gate-consulted-once.txt",
        f"one `harnessed run {PROMPT}`\ngate consultations: {len(calls)}\ncall: {calls[0]}\n",
    )


def test_the_token_budget_flag_reaches_assembly(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--token-budget`` resolved to a value nothing read. It reaches the ceiling now."""
    budgets: list[object] = []
    real = gate.assemble_if_gated

    def spy(model: object, connection: object, query: str, **kwargs: object) -> object:
        budgets.append(kwargs["budget"])
        return real(model, connection, query, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(run_cmd.gate, "assemble_if_gated", spy)
    result = runner.invoke(app, ["run", PROMPT, "--token-budget", "321"])

    assert result.exit_code == 0, result.output
    assert budgets == [321]


def test_a_zero_budget_assembles_nothing_and_the_run_still_completes(
    clean_env: None, conn: sqlite3.Connection, seen_requests: list[ChatRequest]
) -> None:
    """Zero is the way to say "assemble nothing", and it is not an error."""
    semantic.put(conn, "the harness stores one fact per row", source="test")

    result = runner.invoke(app, ["run", PROMPT, "--token-budget", "0"])

    assert result.exit_code == 0, result.output
    assert "# Facts" not in seen_requests[0].messages[0].content
    assert "# Facts" not in seen_requests[0].system


def test_a_declining_gate_assembles_nothing_at_all(
    clean_env: None,
    conn: sqlite3.Connection,
    seen_requests: list[ChatRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declined run assembles none, not a shorter one: ``assemble`` is never called."""
    assemblies: list[object] = []

    def counting_assemble(*args: object, **kwargs: object) -> retrieval.Assembled:
        assemblies.append(args)
        raise AssertionError("assemble must not run when the gate declines")

    monkeypatch.setattr(
        run_cmd.gate,
        "decide",
        lambda *a, **k: gate.Decision(retrieve=False, answer="no"),
    )
    monkeypatch.setattr(retrieval, "assemble", counting_assemble)
    semantic.put(conn, "a fact the declined run must never see", source="test")

    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    assert assemblies == []
    assert "# Facts" not in seen_requests[0].messages[0].content
    assert "# Facts" not in seen_requests[0].system


def test_a_gate_whose_provider_raises_still_retrieves(
    clean_env: None,
    conn: sqlite3.Connection,
    seen_requests: list[ChatRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open belongs to ``gate.decide``. The CLI adds no second error path.

    The failure is injected at the provider, not at the gate: a test that patched ``decide``
    would assert that a stub returns what a stub was told to return. Only the gate's own request
    raises - the loop's provider is the real FakeProvider - so this is the fail-open path end to
    end, from a broken small-model call to a prompt that still carries the fact.

    The router's own ``needs_memory`` answer for this fixture (0.1, below the threshold) would
    otherwise skip retrieval and never call the v4 gate this test means to break at all; a forced
    fallback (ticket 179's ``_AlwaysFallbackDecider``, replacing ticket 169's now-deleted
    ``HARNESSED_ROUTER=off``) is what still calls it on every message (decision 59).
    """
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings: _AlwaysFallbackDecider())
    fact = "ping is answered from an unusual fixture about tessellation"
    semantic.put(conn, fact, source="test")

    real_build = run_cmd.build_provider

    def breaking_gate_provider(settings: object) -> object:
        underlying = real_build(settings)  # type: ignore[arg-type]

        class OnlyTheGateFails:
            def complete(self, request: ChatRequest) -> object:
                if request.metadata.get("scope") == gate.GATE_SCOPE:
                    raise ProviderError("the gate's endpoint is down")
                return underlying.complete(request)

        return OnlyTheGateFails()

    monkeypatch.setattr(run_cmd, "build_provider", breaking_gate_provider)

    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    assert fact in seen_requests[0].messages[0].content
    assert fact not in seen_requests[0].system
    assert "retrieval.gate.failed_open" in event_names(db.connect())


# ---------------------------------------------------------------------------
# 2. what the assembled context does to the prompt
# ---------------------------------------------------------------------------


def test_empty_stores_leave_the_prompt_byte_identical(
    clean_env: None, conn: sqlite3.Connection, seen_requests: list[ChatRequest]
) -> None:
    """An empty retrieval must be indistinguishable from no retrieval at all."""
    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    expected = run_cmd.build_system_prompt(
        run_cmd.BASE_INSTRUCTION, tuple(p.text for p in procedural.active(db.connect()))
    )
    assert seen_requests[0].system == expected
    assert seen_requests[0].messages[0].content == PROMPT
    capture(
        "empty-stores-identical-prompt.txt",
        "fresh .harnessed/, nothing in any store\n"
        f"prompt with retrieval wired:    {seen_requests[0].system!r}\n"
        f"prompt built the pre-041 way:   {expected!r}\n"
        f"equal: {seen_requests[0].system == expected}\n",
    )


def test_a_matching_fact_rides_in_the_user_turn_and_the_system_prompt_is_procedures_only(
    clean_env: None,
    conn: sqlite3.Connection,
    seen_requests: list[ChatRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrieved context leads the user turn (ticket 083); the system prompt is the procedures.

    A ``_NeedsMemoryDecider`` at 0.85 (ticket 169): ``ping``'s own committed fixture answers
    ``needs_memory`` at 0.1, below the threshold, which would skip retrieval before this ever
    reaches the user turn - routing stays on so the system prompt is still ``BASE_INSTRUCTION``.
    """
    monkeypatch.setattr(
        run_cmd, "build_decider", lambda settings: _NeedsMemoryDecider(needs_memory="0.85")
    )
    fact = "ping is answered with pong in the committed corpus"
    semantic.put(conn, fact, source="test")
    stored = procedural.put(conn, "Answer in one word.", source="test")
    procedural.activate(conn, stored.proc_id, cand_id=None)

    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    system = seen_requests[0].system
    user = seen_requests[0].messages[0].content
    assert fact not in system
    # The base instruction, then the procedure envelope, and nothing retrieved between them.
    assert system.startswith(run_cmd.BASE_INSTRUCTION)
    assert system[len(run_cmd.BASE_INSTRUCTION) :].lstrip().startswith("<procedure>")
    assert fact in user
    assert user.index(fact) < user.index(PROMPT, user.index("</retrieved_context>"))


def test_an_active_procedure_appears_in_the_prompt_exactly_once(
    clean_env: None, conn: sqlite3.Connection, seen_requests: list[ChatRequest]
) -> None:
    """Procedural memory bypasses retrieval - the diagram's direct arrow - so it is not doubled.

    ``retrieval._candidates`` puts procedures first in its own priority order, which is right for
    a caller that has no other way to see them. ``run()`` already loads them wholesale through
    ``Session.system()``, so assembling them again would put the same sentence in the prompt
    twice and charge the budget for it.
    """
    text = "Prefer the shortest correct answer."
    stored = procedural.put(conn, text, source="test")
    procedural.activate(conn, stored.proc_id, cand_id=None)
    semantic.put(conn, "an unrelated fact that keeps retrieval non-empty", source="test")

    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    assert seen_requests[0].system.count(text) == 1


def test_assemble_defaults_to_every_kind_so_no_existing_caller_changed(
    conn: sqlite3.Connection,
) -> None:
    """The ``kinds`` argument is opt-in. Called without it, assembly is what it always was."""
    stored = procedural.put(conn, "a procedure", source="test")
    procedural.activate(conn, stored.proc_id, cand_id=None)

    every = retrieval.assemble(conn, "a procedure", budget=4000)
    facts_only = retrieval.assemble(conn, "a procedure", budget=4000, kinds=("fact", "episode"))

    assert "# Procedures" in every.text
    assert "# Procedures" not in facts_only.text


# ---------------------------------------------------------------------------
# 2b. ticket 166 (FR-105): a routed Groq message writes the same run_messages rows v4 does
# ---------------------------------------------------------------------------


def test_a_routed_message_writes_run_messages_like_v4_but_its_retrieval_now_diverges(
    clean_env: None,
    conn: sqlite3.Connection,
    seen_requests: list[ChatRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 60 held through ticket 168: the procedures a routed Groq route loads and the shape
    of what it writes to ``run_messages`` still match v4's for the same fixture -
    ``loop/engine.py`` runs unchanged (0 diff, ``scope_out`` for this ticket).

    Ticket 169 (decision 7 as amended) is what first makes the *retrieval* itself diverge: the
    router's own ``needs_memory`` for ``ping`` is 0.1, below ``NEEDS_MEMORY_THRESHOLD``, so the
    routed run skips retrieval and makes **0** ``_gate`` calls, while a forced fallback
    (``_AlwaysFallbackDecider``, replacing ticket 179's now-deleted ``HARNESSED_ROUTER=off``) still
    runs the v4 path with its own gate, which answers ``yes`` unconditionally for every fixture,
    and still retrieves the fact. That the two now legitimately disagree here is exactly what
    FR-110 changes; before ticket 169 they always agreed, because the v4 gate was the only thing
    either path ever asked. Ticket 179 also deleted the v4 base instruction that used to also
    split the two requests' ``system`` text - both now carry ``BASE_INSTRUCTION`` unconditionally.
    """
    fact = "ping is answered with pong in the committed corpus"
    semantic.put(conn, fact, source="test")
    stored = procedural.put(conn, "Answer in one word.", source="test")
    procedural.activate(conn, stored.proc_id, cand_id=None)

    routed = runner.invoke(app, ["run", "--json", PROMPT])
    assert routed.exit_code == 0, routed.output
    routed_run_id = json.loads(routed.stdout)["run_key"]

    monkeypatch.setattr(run_cmd, "build_decider", lambda settings: _AlwaysFallbackDecider())
    fallback = runner.invoke(app, ["run", "--json", PROMPT])
    assert fallback.exit_code == 0, fallback.output
    fallback_run_id = json.loads(fallback.stdout)["run_key"]

    assert len(seen_requests) == 2, "each invocation must reach the loop exactly once"
    routed_request, fallback_request = seen_requests
    assert routed_request.system == fallback_request.system, "179 deleted the v4 base instruction"
    assert routed_request.messages[0].content == PROMPT, "0.1 is below the 0.5 threshold: skip"
    assert fact in fallback_request.messages[0].content, "v4's own gate still answers yes"
    assert (
        routed_request.messages != fallback_request.messages
    ), "169 is what first splits these too"

    def message_rows(run_id: str) -> list[tuple[object, ...]]:
        return conn.execute(
            "SELECT role, content, tool_calls, tool_results FROM run_messages "
            "WHERE run_id = ? ORDER BY idx",
            (run_id,),
        ).fetchall()

    routed_rows = message_rows(routed_run_id)
    fallback_rows = message_rows(fallback_run_id)
    assert routed_rows, "the routed run wrote no message at all"
    assert [row[0] for row in routed_rows] == [
        row[0] for row in fallback_rows
    ], "same roles in the same order - only the retrieved content now differs"
    assert routed_rows != fallback_rows

    (outcome,) = conn.execute(
        "SELECT outcome FROM decisions WHERE run_id = ?", (routed_run_id,)
    ).fetchone()
    assert outcome == "routed", "the first run must actually have gone through the router"

    capture_166(
        "routed-run-messages-match-v4.txt",
        "`harnessed run --json ping`, once routed (a Decider chose groq_chat), once forced onto "
        "the fallback path (_AlwaysFallbackDecider)\n\n"
        f"routed run_messages:   {routed_rows}\n"
        f"fallback run_messages: {fallback_rows}\n"
        f"same roles: {[row[0] for row in routed_rows] == [row[0] for row in fallback_rows]}\n",
    )
    capture_169(
        "routed-vs-unrouted-retrieval-diverges.txt",
        "`harnessed run --json ping`, once routed (needs_memory 0.1 < 0.5: skip, 0 _gate calls), "
        "once forced onto the fallback path (v4's own gate still answers yes, unchanged)\n\n"
        f"routed user turn:   {routed_request.messages[0].content!r}\n"
        f"fallback user turn: {fallback_request.messages[0].content!r}\n",
    )


@dataclass
class _NeedsMemoryDecider:
    """A Decider double that always routes ``groq_chat`` at 0.93, naming only the
    ``needs_memory`` probability a test cares about (ticket 169) and, since ticket 170,
    ``memory_kind`` too."""

    needs_memory: str
    memory_kind: str = "semantic"
    calls: int = field(default=0, init=False)

    def decide(self, state: str, questions: tuple[Question, ...]) -> Decisions:
        self.calls += 1
        return Decisions(
            answers={
                "route": Choice(
                    value="groq_chat",
                    confidence=Decimal("0.93"),
                    probabilities={"groq_chat": Decimal("0.93")},
                ),
                "needs_memory": Noul(probability=Decimal(self.needs_memory)),
                "memory_kind": Choice(
                    value=self.memory_kind,
                    confidence=Decimal("0.6"),
                    probabilities={self.memory_kind: Decimal("0.6")},
                ),
            },
            id="gen-dec-test",
            model="typesafe/jev-1.13-20260917",
            usage={"input_tokens": 420, "output_tokens": 22},
            cost="0.0000158",
        )


def test_a_routed_message_with_high_needs_memory_makes_zero_gate_calls_and_still_retrieves(
    clean_env: None,
    conn: sqlite3.Connection,
    seen_requests: list[ChatRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1's other half: at 0.85 (above ``NEEDS_MEMORY_THRESHOLD``) a routed message retrieves,
    and still makes 0 calls to the gate's own provider - the router's answer is trusted, never
    parsed from a second request.
    """
    fact = "ping is answered with pong according to this recorded fact"
    semantic.put(conn, fact, source="test")
    decider = _NeedsMemoryDecider(needs_memory="0.85")
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings: decider)

    gate_requests: list[ChatRequest] = []
    real_build_provider = run_cmd.build_provider

    def counting_provider(settings: object) -> object:
        underlying = real_build_provider(settings)  # type: ignore[arg-type]

        class CountTheGate:
            def complete(self, request: ChatRequest) -> object:
                if request.metadata.get("scope") == gate.GATE_SCOPE:
                    gate_requests.append(request)
                return underlying.complete(request)

        return CountTheGate()

    monkeypatch.setattr(run_cmd, "build_provider", counting_provider)

    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    assert decider.calls == 1
    assert gate_requests == []
    assert fact in seen_requests[0].messages[0].content

    capture_169(
        "high-needs-memory-zero-gate-calls.txt",
        "`harnessed run ping` with a Decider naming needs_memory=0.85\n\n"
        f"gate provider calls: {len(gate_requests)}\n"
        f"user turn:           {seen_requests[0].messages[0].content!r}\n",
    )


def test_a_routed_message_with_low_needs_memory_makes_zero_gate_calls_and_skips(
    clean_env: None,
    conn: sqlite3.Connection,
    seen_requests: list[ChatRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1's first half, isolated from the ``ping`` fixture's own fixed 0.1: at 0.20 a routed
    message skips retrieval, and makes 0 calls to the gate's own provider either way."""
    fact = "ping is answered with pong according to this recorded fact"
    semantic.put(conn, fact, source="test")
    decider = _NeedsMemoryDecider(needs_memory="0.20")
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings: decider)

    gate_requests: list[ChatRequest] = []
    real_build_provider = run_cmd.build_provider

    def counting_provider(settings: object) -> object:
        underlying = real_build_provider(settings)  # type: ignore[arg-type]

        class CountTheGate:
            def complete(self, request: ChatRequest) -> object:
                if request.metadata.get("scope") == gate.GATE_SCOPE:
                    gate_requests.append(request)
                return underlying.complete(request)

        return CountTheGate()

    monkeypatch.setattr(run_cmd, "build_provider", counting_provider)

    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    assert decider.calls == 1
    assert gate_requests == []
    assert fact not in seen_requests[0].messages[0].content
    assert seen_requests[0].messages[0].content == PROMPT


# ---------------------------------------------------------------------------
# 2c. ticket 170 (FR-111): memory_kind scopes which stores a routed retrieval searches
# ---------------------------------------------------------------------------


def test_a_semantic_kind_retrieves_the_fact_wrapped_exactly_like_v4(
    clean_env: None,
    conn: sqlite3.Connection,
    seen_requests: list[ChatRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 (the `semantic` third) and AC2 together: `semantic` narrows retrieval to facts, and
    the wrapped user turn is `loop.prompt.build_user_turn`'s own bytes - unchanged by this ticket,
    scope_out - not a second, narrower format."""
    monkeypatch.setattr(
        run_cmd,
        "build_decider",
        lambda settings: _NeedsMemoryDecider(needs_memory="0.85", memory_kind="semantic"),
    )
    fact = "ping is answered with pong according to this recorded fact"
    semantic.put(conn, fact, source="test")

    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    user_turn = seen_requests[0].messages[0].content
    assert fact in user_turn
    expected = run_cmd.build_user_turn(
        retrieval.assemble(conn, PROMPT, budget=4000, kinds=("fact",)).text, PROMPT
    )
    assert user_turn == expected
    capture_170(
        "semantic-kind-retrieves-fact.txt",
        f"memory_kind=semantic\nuser turn: {user_turn!r}\nexpected (v4 wrapping): {expected!r}\n",
    )


def test_an_episodic_kind_does_not_search_the_fact_store(
    clean_env: None,
    conn: sqlite3.Connection,
    seen_requests: list[ChatRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 (the `episodic` third, its negative half): a fact sitting in the store is invisible to
    an `episodic` answer - `retrieval.assemble` was never asked for the `fact` kind at all."""
    monkeypatch.setattr(
        run_cmd,
        "build_decider",
        lambda settings: _NeedsMemoryDecider(needs_memory="0.85", memory_kind="episodic"),
    )
    fact = "ping is answered with pong according to this recorded fact"
    semantic.put(conn, fact, source="test")

    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    user_turn = seen_requests[0].messages[0].content
    assert fact not in user_turn
    # Nothing else is running yet in this brand-new run (its own "loop" episode does not exist
    # until this message finishes), so an empty assembly leaves the task byte-identical (AC2).
    assert user_turn == PROMPT


def test_a_procedural_kind_retrieves_nothing_extra_even_with_a_fact_in_store(
    clean_env: None,
    conn: sqlite3.Connection,
    seen_requests: list[ChatRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 (the `procedural` third): procedures already load wholesale into the system prompt
    (decision 7), so `procedural` narrows retrieval to nothing rather than to a `procedure`
    search - the same empty assembly, byte-identical task, as the `episodic` negative above."""
    monkeypatch.setattr(
        run_cmd,
        "build_decider",
        lambda settings: _NeedsMemoryDecider(needs_memory="0.85", memory_kind="procedural"),
    )
    fact = "ping is answered with pong according to this recorded fact"
    semantic.put(conn, fact, source="test")
    stored = procedural.put(conn, "Answer in one word.", source="test")
    procedural.activate(conn, stored.proc_id, cand_id=None)

    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code == 0, result.output
    user_turn = seen_requests[0].messages[0].content
    assert fact not in user_turn
    assert user_turn == PROMPT
    capture_170(
        "procedural-kind-retrieves-nothing-extra.txt",
        f"memory_kind=procedural\nuser turn: {user_turn!r} (byte-identical to the bare task)\n",
    )


def test_an_episodic_kind_retrieves_the_prior_messages_own_episode(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 (the `episodic` third, its positive half): a follow-up on the same run reads back the
    first message's own written-back ``loop`` episode - ``episodic.for_run`` offers this run's
    episodes wholesale, no search - while an unrelated fact in the store stays invisible, because
    `episodic` never asks for the `fact` kind at all (built directly on a ``Session``, the same
    shape ticket 167's follow-up test uses, so two real ``FakeProvider`` iterations of the
    committed ``ping`` fixture serve both messages).
    """
    captured: list[ChatRequest] = []
    real_run_loop = run_cmd.run_loop

    def spy(model: object, registry: object, request: ChatRequest, **kwargs: object) -> object:
        captured.append(request)
        return real_run_loop(model, registry, request, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(run_cmd, "run_loop", spy)

    settings = config.load(provider="fake")
    bus = EventBus(conn=conn)
    decider = _NeedsMemoryDecider(needs_memory="0.85", memory_kind="episodic")
    session = run_cmd.Session(
        settings=settings,
        conn=conn,
        bus=bus,
        tools=default_registry(settings.home),
        provider=FakeProvider(settings),
        decider=decider,
    )
    fact = "an unrelated fact that episodic retrieval must never see"
    semantic.put(conn, fact, source="test")

    first = run_cmd.solve_with_memory(session, PROMPT)
    assert first.result is not None
    first_reply = first.result.reply

    second = run_cmd.continue_session(session, first.run_id, "ping again")
    assert second.result is not None
    assert len(captured) == 2
    second_user_turn = captured[-1].messages[-1].content

    assert first_reply in second_user_turn, "the first message's own episode came back"
    assert PROMPT in second_user_turn
    assert fact not in second_user_turn, "episodic never asks assemble for the fact kind"
    capture_170(
        "episodic-kind-retrieves-prior-episode.txt",
        f"first reply={first_reply!r}\nsecond user turn={second_user_turn!r}\n"
        f"unrelated fact present: {fact in second_user_turn}\n",
    )


# ---------------------------------------------------------------------------
# 3. the write-back: the loop the diagram draws actually closes
# ---------------------------------------------------------------------------


def test_a_finished_run_writes_exactly_one_loop_episode(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """The write-back arrow. Kind pairs with ``acp.delegate.EPISODE_KIND``; there is no third."""
    result = runner.invoke(app, ["run", PROMPT])
    assert result.exit_code == 0, result.output

    written = episodes(db.connect())
    assert len(written) == 1
    run_id, kind, text = written[0]
    assert kind == run_cmd.EPISODE_KIND == "loop"
    assert run_id.startswith("run_")
    assert PROMPT in text
    assert REPLY_FIXTURE in text


def test_a_failed_run_writes_no_episode(
    clean_env: None, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An episode records what happened, and a run that raised did not happen."""

    def raising(*args: object, **kwargs: object) -> LoopResult:
        raise ProviderError("the endpoint returned 503")

    monkeypatch.setattr(run_cmd, "run_loop", raising)
    result = runner.invoke(app, ["run", PROMPT])

    assert result.exit_code != 0
    assert episodes(db.connect()) == []


def test_the_run_id_is_printed_and_the_episode_is_readable_by_it(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """``harnessed memory episodes <id>`` is only usable if the run told you the id."""
    ran = runner.invoke(app, ["run", PROMPT])
    assert ran.exit_code == 0, ran.output

    run_id = episodes(db.connect())[0][0]
    assert run_id in ran.output

    read = runner.invoke(app, ["memory", "episodes", run_id])
    assert read.exit_code == 0, read.output
    assert run_cmd.EPISODE_KIND in read.output
    capture(
        "episode-round-trip.txt",
        f"$ harnessed run {PROMPT}\n{ran.output}\n"
        f"$ harnessed memory episodes {run_id}\n{read.output}",
    )


def test_the_run_publishes_its_own_start_and_finish(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """Two frozen names that had no publisher. This is the seam v2's SSE gateway attaches to."""
    result = runner.invoke(app, ["run", PROMPT])
    assert result.exit_code == 0, result.output

    names = event_names(db.connect())
    assert names[0] == run_cmd.RUN_STARTED
    assert names[-1] == run_cmd.RUN_FINISHED


# ---------------------------------------------------------------------------
# 4. the journal records, and the run leaves a trace (ticket 045)
# ---------------------------------------------------------------------------


def turns_for(connection: sqlite3.Connection, run_id: str) -> list[tuple[int, str]]:
    """Every journalled turn of one run, oldest first."""
    rows = connection.execute(
        "SELECT iteration, response FROM turns WHERE run_id = ? ORDER BY iteration", (run_id,)
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def test_a_run_journals_every_turn_it_takes(clean_env: None, conn: sqlite3.Connection) -> None:
    """FR-7, executed rather than unit-tested.

    ``run_loop`` has taken a ``journal`` parameter since ticket 013 and no caller in ``src/`` has
    ever passed anything but ``NO_JOURNAL``. Counted at commit 42d5bb4: ``turns`` held zero rows
    and ``tool_results`` held zero rows, across every run the harness had ever done. The ledger
    that makes a run resumable had never recorded a single turn.
    """
    result = runner.invoke(app, ["run", PROMPT])
    assert result.exit_code == 0, result.output

    live = db.connect()
    run_id = episodes(live)[0][0]
    assert turns_for(live, run_id), "the run journalled nothing at all"


def test_a_resumed_run_replays_its_journalled_turns(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """The claim FR-7 actually makes: a resumed run does not ask the provider again.

    Asserted with a counting provider rather than by reading the ledger, because a journal that
    records perfectly and is never consulted looks identical from the table's side.
    """
    session = run_cmd.open_session()
    first = run_cmd.solve_with_memory(session, PROMPT)
    assert turns_for(session.conn, first.run_id)

    # The same bus, not a fresh one keyed to the same run: ``events`` is keyed (run_id, seq) and
    # a new bus restarts seq at 1, which collides rather than resumes.
    counted = _CountingProvider(session.provider)
    run_cmd.solve_with_memory(replace(session, provider=counted), PROMPT)

    assert counted.loop_calls == 0, (
        f"the resumed run made {counted.loop_calls} loop call(s). A journalled turn is supposed "
        f"to be replayed, which is the whole of what FR-7 claims."
    )


class _CountingProvider:
    """Counts what the loop asked for, leaving the gate's own request out of the total."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.loop_calls = 0

    def complete(self, request: ChatRequest) -> object:
        if request.metadata.get("scope") != "_gate":
            self.loop_calls += 1
        return self._inner.complete(request)  # type: ignore[attr-defined]


def test_a_finished_run_writes_a_trace_a_person_can_read(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """The artefact this whole ticket is for: one file, one object per iteration."""
    result = runner.invoke(app, ["run", PROMPT])
    assert result.exit_code == 0, result.output

    run_id = episodes(db.connect())[0][0]
    path = Path(os.environ["HARNESSED_HOME"]) / "evals" / f"{run_id}.trace.jsonl"
    assert path.exists(), f"no trace at {path}"

    objects = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert objects, "the trace file is empty"
    assert objects[0]["iteration"] == 1
    # The request is carried as the string the journal stored, not decoded: translate.py is the
    # one module allowed to parse model output and a source scan pins the decoding call there.
    # What matters is that the system prompt reached the file, and a string can say so.
    assert isinstance(objects[0]["request"], str)
    assert "system" in objects[0]["request"]
    assert "content" in objects[0]["response"]


def test_the_trace_says_how_each_retrieved_item_arrived(
    clean_env: None, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trace that flattens retrieval into one string cannot answer *why did it see that*.

    Since ticket 047 a fact reaches the prompt by one of two routes - it matched the search, or
    something that matched linked to it - and ticket 048 changed which facts match at all. The
    kind is what makes the difference legible.

    A forced fallback (``_AlwaysFallbackDecider``, replacing ticket 179's now-deleted
    ``HARNESSED_ROUTER=off``, ticket 169): ``ping``'s own ``needs_memory`` (0.1) is below the
    threshold and would skip retrieval before there is anything to trace - the v4 path a fallback
    runs is what still asks its own gate.
    """
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings: _AlwaysFallbackDecider())
    target = semantic.put(conn, "the deployment manifest lives in the platform repository", "t")
    semantic.put(conn, "ping is answered with pong by the corpus", "t", links=(target.mem_id,))

    result = runner.invoke(app, ["run", PROMPT])
    assert result.exit_code == 0, result.output

    run_id = episodes(db.connect())[0][0]
    path = Path(os.environ["HARNESSED_HOME"]) / "evals" / f"{run_id}.trace.jsonl"
    objects = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    retrieval_block = objects[0]["retrieval"]
    assert retrieval_block["budget"] == 4000
    kinds = {item["kind"] for item in retrieval_block["items"]}
    assert {"fact", "linked"} <= kinds, f"the trace lost how each item arrived: {kinds}"
