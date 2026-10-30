"""Ticket 160 (FR-101, FR-94's scrub half): the router asks once, with a digested state.

Four modules, each tested at the seam it owns: ``questions.py`` (the fixed vocabulary),
``state.py`` (the digests, the scrub, the truncation rule), ``route.py`` (one Decider call, then
the verdict) and ``cli/routed.py`` (the dispatch that either does nothing or raises).

Ticket 161 (FR-108, decision 61) added the writes: ``route.decide`` now takes the message's
``announced`` event, its raw ``task`` and the configured router ``model``, and writes one
``decisions`` row and its ``decision_options`` rows - the ``# ---- route.py`` and
``# ---- decisions/decision_options`` sections below - before it returns, whichever branch
answers.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from tests.support.raising_provider import RaisingProvider
from tests.support.scoped_decider import ScopedFakeDecider
from typer.testing import CliRunner

from harnessed import control
from harnessed.acp import ledger
from harnessed.acp.config import AgentSpec
from harnessed.cli import routed
from harnessed.cli.app import app as cli_app
from harnessed.cli.run_cmd import Session
from harnessed.errors import DecisionRefused, DecisionTimeout, DecisionUnreachable
from harnessed.events.bus import Event, EventBus
from harnessed.loop.prompt import BASE_INSTRUCTION, SYNTHESIS_INSTRUCTION, build_system_prompt
from harnessed.memory import db, episodic, procedural, redact, semantic
from harnessed.memory.budget import count_tokens
from harnessed.providers.fake_decider import FakeDecider
from harnessed.providers.jev import Choice, Decider, Decisions, Noul, Question
from harnessed.router import decisions as decisions_store
from harnessed.router import questions, route, state
from harnessed.tools import default_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "160"
EVIDENCE_161 = REPO_ROOT / "evidence" / "161"
EVIDENCE_162 = REPO_ROOT / "evidence" / "162"
EVIDENCE_163 = REPO_ROOT / "evidence" / "163"
EVIDENCE_164 = REPO_ROOT / "evidence" / "164"
EVIDENCE_168 = REPO_ROOT / "evidence" / "168"
EVIDENCE_169 = REPO_ROOT / "evidence" / "169"
EVIDENCE_170 = REPO_ROOT / "evidence" / "170"
EVIDENCE_179 = REPO_ROOT / "evidence" / "179"

#: The router model every direct ``route.decide`` test names, matching the fake corpus's own.
MODEL = "typesafe/jev-1.13-20260917"


def capture_161(name: str, text: str) -> Path:
    EVIDENCE_161.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_161 / name
    path.write_text(text, encoding="utf-8")
    return path


def capture_162(name: str, text: str) -> Path:
    EVIDENCE_162.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_162 / name
    path.write_text(text, encoding="utf-8")
    return path


def capture_163(name: str, text: str) -> Path:
    EVIDENCE_163.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_163 / name
    path.write_text(text, encoding="utf-8")
    return path


def capture_164(name: str, text: str) -> Path:
    EVIDENCE_164.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_164 / name
    path.write_text(text, encoding="utf-8")
    return path


def capture_168(name: str, text: str) -> Path:
    EVIDENCE_168.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_168 / name
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


def capture_179(name: str, text: str) -> Path:
    EVIDENCE_179.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_179 / name
    path.write_text(text, encoding="utf-8")
    return path


def _announced(run_id: str = "run_x", seq: int = 1) -> Event:
    """A bare ``run.started`` event, exactly what ``run_cmd.announce`` hands ``route.decide``."""
    return Event(name="run.started", run_id=run_id, seq=seq, ts="2026-01-01T00:00:00+00:00")


def capture(name: str, text: str) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


# ---- questions.py: the fixed vocabulary, together (AC2) ------------------------------------------


def test_the_three_questions_are_asked_together_in_order() -> None:
    names_and_kinds = [(q.name, q.kind) for q in questions.QUESTIONS]
    assert names_and_kinds == [
        ("route", "choice"),
        ("needs_memory", "noul"),
        ("memory_kind", "choice"),
    ]


def test_the_route_options_are_the_fixed_three_in_order() -> None:
    """Registry options are 163's; this ticket's whole vocabulary is these three."""
    route_question = next(q for q in questions.QUESTIONS if q.name == "route")
    assert route_question.options == ("groq_chat", "groq_memory", "needs_human")


def test_the_memory_kind_options_are_the_three_memory_stores() -> None:
    memory_kind_question = next(q for q in questions.QUESTIONS if q.name == "memory_kind")
    assert memory_kind_question.options == ("semantic", "procedural", "episodic")


# ---- questions.py: memory_kind maps onto retrieval.assemble's own kinds (ticket 170, FR-111) ----


def test_semantic_maps_to_facts_only() -> None:
    assert questions.retrieval_kinds_for("semantic") == ("fact",)


def test_episodic_maps_to_episodes_only() -> None:
    assert questions.retrieval_kinds_for("episodic") == ("episode",)


def test_procedural_retrieves_nothing_extra() -> None:
    """Procedures already load wholesale into the system prompt (decision 7), so the `procedural`
    answer narrows retrieval to nothing rather than to the `procedure` kind."""
    assert questions.retrieval_kinds_for("procedural") == ()


def test_an_unanswered_memory_kind_falls_back_to_the_pre_170_default() -> None:
    """`None` (the ticket 169 shape a `Verdict` with no answer at all could still carry) maps to
    :data:`questions.DEFAULT_RETRIEVAL_KINDS`, the same `("fact", "episode")` `_router_memory`
    always used before this ticket."""
    assert questions.retrieval_kinds_for(None) == questions.DEFAULT_RETRIEVAL_KINDS
    assert questions.DEFAULT_RETRIEVAL_KINDS == ("fact", "episode")


def test_groq_routes_share_the_050_floor() -> None:
    assert questions.FLOORS == {
        "groq_chat": Decimal("0.50"),
        "groq_memory": Decimal("0.50"),
    }


# ---- questions.py: the route options are the registry (ticket 163, FR-102) -----------------------


def _spec(name: str, *, use_for: str, not_for: str) -> AgentSpec:
    """An installed agent: ``command`` is this interpreter, which always resolves."""
    return AgentSpec(name=name, command=sys.executable, use_for=use_for, not_for=not_for)


def test_two_installed_entries_sit_between_the_groq_pair_and_needs_human() -> None:
    agents = {
        "agent-a": _spec("agent-a", use_for="general coding tasks", not_for="anything urgent"),
        "agent-b": _spec(
            "agent-b", use_for="tasks better suited to a second agent", not_for="anything urgent"
        ),
    }
    question = questions.build_route_question(agents)
    capture_163("two-installed.txt", "\n".join(question.options))
    assert question.options == (
        "groq_chat",
        "groq_memory",
        "agent-a",
        "agent-b",
        "needs_human",
    )
    descriptions = questions.route_descriptions(agents)
    assert descriptions["agent-a"] == "general coding tasks Not for: anything urgent"
    assert (
        descriptions["agent-b"] == "tasks better suited to a second agent Not for: anything urgent"
    )


def test_a_stub_entry_added_to_agentstoml_grows_the_options_by_exactly_one() -> None:
    before = questions.route_options({})
    capture_163("before-stub.txt", "\n".join(before))
    agents = {"stub": _spec("stub", use_for="a stub", not_for="nothing")}
    after = questions.route_options(agents)
    capture_163("after-stub.txt", "\n".join(after))
    assert len(after) == len(before) + 1
    assert after == (*before[:-1], "stub", before[-1])


def test_an_entry_whose_command_does_not_resolve_is_not_an_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    agents = {
        "not-installed": AgentSpec(
            name="not-installed",
            command="there-is-no-such-agent-cli",
            use_for="x",
            not_for="y",
        )
    }
    options = questions.route_options(agents)
    capture_163("uninstalled.txt", "\n".join(options))
    assert options == ("groq_chat", "groq_memory", "needs_human")


# ---- state.py: the digests, pinned (AC3) ---------------------------------------------------------


def test_a_recent_refusal_adds_one_line_after_the_session_digest(
    conn: sqlite3.Connection,
) -> None:
    history = (("user", "hello"),)
    bare = state.build_state(conn, "ping", history)
    refused = state.build_state(conn, "ping", history, refusing=("x",))
    assert refused.text == bare.text + "\nRecently refusing: x (out of usage)"
    assert state.build_state(conn, "ping", history, refusing=()).text == bare.text


def test_dispatch_reads_the_ledger_for_refusals_inside_the_window(
    conn: sqlite3.Connection,
) -> None:
    key = ledger.task_hash(agent="x", cwd="/w", task="t", acceptance="a")
    ledger.begin(conn, key, agent="x", cwd="/w", task="t", acceptance="a", attempts=1)
    ledger.settle(conn, key, status=ledger.FAILED, attempts=1, failure="quota_refused: out")
    assert routed.refusing_now(conn) == ("x",)
    conn.execute("UPDATE delegations SET finished_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    assert routed.refusing_now(conn) == ()


def test_the_operator_digest_names_no_operator_when_none_is_recorded(
    conn: sqlite3.Connection,
) -> None:
    built = state.build_state(conn, "what time is it in Lagos?")
    capture("state-no-operator.txt", built.text)
    assert "The operator's name is not recorded." in built.text
    assert "Memory holds 0 facts, 0 past sessions, 0 procedures." in built.text
    assert built.text.startswith("Task: what time is it in Lagos?")
    assert built.text.endswith("This session so far:\n")
    assert built.truncated is False


def test_the_operator_digest_names_the_newest_three_tagged_facts(conn: sqlite3.Connection) -> None:
    for index in range(4):
        semantic.put(conn, f"The operator is called Vinci, note {index}", "test", tags=["operator"])
    semantic.put(conn, "an untagged fact that must not appear", "test")

    built = state.build_state(conn, "who am I?")
    capture("state-operator-digest.txt", built.text)
    assert "note 3" in built.text
    assert "note 2" in built.text
    assert "note 1" in built.text
    assert "note 0" not in built.text, "only the newest three, not every tagged fact"
    assert "untagged fact" not in built.text


def test_the_session_digest_is_empty_on_a_first_message(conn: sqlite3.Connection) -> None:
    built = state.build_state(conn, "ping")
    assert built.text.split("This session so far:\n")[1] == ""


def test_the_session_digest_holds_the_last_six_messages_stripped_and_capped(
    conn: sqlite3.Connection,
) -> None:
    history = tuple(
        (role, f"message {index}")
        for index in range(8)
        for role in (("user",) if index % 2 == 0 else ("assistant",))
    )
    # Eight alternating turns; only the newest six may appear.
    built = state.build_state(conn, "ping", history)
    assert "message 0" not in built.text
    assert "message 1" not in built.text
    for index in range(2, 8):
        assert f"message {index}" in built.text

    context_wrapped = (("user", "<retrieved_context>\nold stuff\n</retrieved_context>\n\nasked"),)
    stripped = state.build_state(conn, "ping", context_wrapped)
    assert "old stuff" not in stripped.text
    assert "asked" in stripped.text

    long_message = (("user", "x" * 500),)
    capped = state.build_state(conn, "ping", long_message)
    assert "x" * 500 not in capped.text
    assert "x" * state.SESSION_MESSAGE_CAP in capped.text


# ---- state.py: everything is scrubbed (AC4, FR-94) -----------------------------------------------

SECRET = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDDEEEEFFFF"


def test_a_seeded_key_in_the_task_reaches_the_state_as_redacted(conn: sqlite3.Connection) -> None:
    built = state.build_state(conn, f"remember this key {SECRET}")
    marker = redact.token("anthropic_key")
    capture("state-scrubbed.txt", built.text)
    assert marker in built.text
    assert SECRET not in built.text


# ---- state.py: the named truncation rule (AC5) ---------------------------------------------------


def test_an_oversized_state_drops_session_lines_before_it_cuts_the_task(
    conn: sqlite3.Connection,
) -> None:
    history = tuple(("user", f"turn {index} " + "word " * 40) for index in range(6))
    tiny_budget = count_tokens(state.build_state(conn, "short task", ()).text) + 5
    built = state.build_state(conn, "short task", history, budget=tiny_budget)
    capture(
        "state-truncated.txt",
        f"budget={tiny_budget}\ntokens={count_tokens(built.text)}\ntruncated={built.truncated}\n"
        f"{built.text}\n",
    )
    assert built.truncated is True
    assert count_tokens(built.text) <= tiny_budget


def test_a_tiny_budget_tail_cuts_the_task(conn: sqlite3.Connection) -> None:
    task = "explain " + "word " * 5000
    frame_tokens = count_tokens(state.build_state(conn, "", ()).text)
    budget = frame_tokens + 10
    built = state.build_state(conn, task, budget=budget)
    assert built.truncated is True
    assert count_tokens(built.text) <= budget
    assert built.text.startswith("Task: explain word word")
    assert not built.text.startswith(f"Task: {task}")


def test_truncation_is_deterministic(conn: sqlite3.Connection) -> None:
    task = "explain " + "word " * 400
    first = state.build_state(conn, task, budget=200)
    second = state.build_state(conn, task, budget=200)
    assert first == second


# ---- route.py: one Decider call, then the verdict (decision 59) ----------------------------------


@dataclass
class _StubDecider:
    """A Decider double that answers exactly what a test hands it, or raises what it hands it."""

    decisions: Decisions | None = None
    raises: Exception | None = None
    calls: int = 0

    def decide(self, state_text: str, asked: tuple[object, ...]) -> Decisions:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        assert self.decisions is not None
        return self.decisions


def _decisions(
    route_value: str,
    confidence: str,
    needs_memory: str = "0.2",
    *,
    memory_kind: str = "semantic",
    cost: str = "0.0000158",
    input_tokens: int = 420,
    output_tokens: int = 22,
) -> Decisions:
    return Decisions(
        answers={
            "route": Choice(
                value=route_value,
                confidence=Decimal(confidence),
                probabilities={route_value: Decimal(confidence)},
            ),
            "needs_memory": Noul(probability=Decimal(needs_memory)),
            "memory_kind": Choice(
                value=memory_kind,
                confidence=Decimal("0.6"),
                probabilities={memory_kind: Decimal("0.6")},
            ),
        },
        id="gen-dec-test-1",
        model=MODEL,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost},
        cost=cost,
    )


def test_a_groq_route_above_its_floor_is_routed(conn: sqlite3.Connection) -> None:
    stub = _StubDecider(decisions=_decisions("groq_chat", "0.93"))
    verdict = route.decide(
        stub, "Task: ping\n", conn=conn, announced=_announced(), task="ping", model=MODEL
    )
    assert stub.calls == 1
    assert verdict.outcome == "routed"
    assert verdict.handler == "groq"
    assert verdict.route == "groq_chat"
    assert verdict.stop_reason is None


def test_below_the_floor_is_an_uncertain_stop(conn: sqlite3.Connection) -> None:
    """The spike's own miss: groq_memory at 0.43 under the 0.50 floor (decision 59, FR-103)."""
    stub = _StubDecider(decisions=_decisions("groq_memory", "0.43"))
    verdict = route.decide(
        stub, "Task: ping\n", conn=conn, announced=_announced(), task="ping", model=MODEL
    )
    assert verdict.outcome == "uncertain"
    assert verdict.stop_reason == "below_floor"
    assert verdict.floor == Decimal("0.50")


def test_needs_human_is_an_uncertain_stop_with_no_floor_check(conn: sqlite3.Connection) -> None:
    stub = _StubDecider(decisions=_decisions("needs_human", "0.99"))
    verdict = route.decide(
        stub,
        "Task: destroy production\n",
        conn=conn,
        announced=_announced(),
        task="destroy production",
        model=MODEL,
    )
    assert verdict.outcome == "uncertain"
    assert verdict.handler == "human"
    assert verdict.stop_reason == "needs_human"
    assert verdict.floor is None


# ---- AC3: an agent route's floor - 0.80 by default, overridden per entry -------------------------


def test_an_agent_route_at_0_79_is_below_its_default_0_80_floor(conn: sqlite3.Connection) -> None:
    stub = _StubDecider(decisions=_decisions("agent-a", "0.79"))
    verdict = route.decide(
        stub, "Task: build\n", conn=conn, announced=_announced(), task="build", model=MODEL
    )
    assert verdict.outcome == "uncertain"
    assert verdict.stop_reason == "below_floor"
    assert verdict.floor == Decimal("0.80")


def test_an_agent_route_at_its_default_floor_is_routed(conn: sqlite3.Connection) -> None:
    stub = _StubDecider(decisions=_decisions("agent-a", "0.80"))
    verdict = route.decide(
        stub, "Task: build\n", conn=conn, announced=_announced(), task="build", model=MODEL
    )
    assert verdict.outcome == "routed"
    assert verdict.floor == Decimal("0.80")


def test_an_entry_with_a_floor_override_of_0_9_stops_at_0_85(conn: sqlite3.Connection) -> None:
    spec = AgentSpec(name="agent-a", command="agent-a", floor=0.9)
    stub = _StubDecider(decisions=_decisions("agent-a", "0.85"))
    verdict = route.decide(
        stub,
        "Task: build\n",
        conn=conn,
        announced=_announced(),
        task="build",
        model=MODEL,
        agents={"agent-a": spec},
    )
    assert verdict.outcome == "uncertain"
    assert verdict.stop_reason == "below_floor"
    assert verdict.floor == Decimal("0.9")
    capture_164(
        "agent-route-floor-override-stop.txt",
        "ticket 164 - agent-a at confidence 0.85 with a floor override of 0.9 (default 0.80)\n"
        f"outcome: {verdict.outcome}\nstop_reason: {verdict.stop_reason}\nfloor: {verdict.floor}\n",
    )


@pytest.mark.parametrize(
    ("raised", "cause"),
    [
        (DecisionUnreachable("gone"), "unreachable"),
        (DecisionTimeout("slow"), "timeout"),
        (DecisionRefused("bad"), "refused"),
    ],
)
def test_every_decider_failure_is_a_fallback_verdict(
    raised: Exception, cause: str, conn: sqlite3.Connection
) -> None:
    stub = _StubDecider(raises=raised)
    verdict = route.decide(
        stub, "Task: ping\n", conn=conn, announced=_announced(), task="ping", model=MODEL
    )
    assert verdict.outcome == "fallback"
    assert verdict.fallback_cause == cause
    assert verdict.decisions is None


def test_state_truncated_rides_onto_the_verdict(conn: sqlite3.Connection) -> None:
    stub = _StubDecider(decisions=_decisions("groq_chat", "0.9"))
    verdict = route.decide(
        stub,
        "Task: ping\n",
        conn=conn,
        announced=_announced(),
        task="ping",
        model=MODEL,
        state_truncated=True,
    )
    assert verdict.state_truncated is True


# ---- route.py: the decisions row is written before decide() returns (AC2, MUST rule 6) ----------


def test_the_decision_row_exists_before_decide_returns_and_after_a_crash_right_after(
    conn: sqlite3.Connection,
) -> None:
    """The row is on disk the instant ``decide`` hands back a verdict - simulated here by reading
    it back through a second, independent connection before anything else runs, and then treating
    a crash right after ``decide`` returns as the ordinary case a resumed session reads from."""
    stub = _StubDecider(decisions=_decisions("groq_chat", "0.93"))
    event = _announced(run_id="run_crash")
    verdict = route.decide(
        stub, "Task: ping\n", conn=conn, announced=event, task="ping", model=MODEL
    )
    assert verdict.outcome == "routed"

    class CrashAfterDecision(RuntimeError):
        """Stands in for the process ending before the handler (Groq chat) ever starts."""

    def handler_start() -> None:
        raise CrashAfterDecision("nothing past this point ever ran")

    with pytest.raises(CrashAfterDecision):
        handler_start()

    reader = db.connect()
    try:
        row = reader.execute(
            "SELECT message_seq, hop, outcome, route, handler FROM decisions "
            "WHERE run_id = ? AND idx = 0",
            (event.run_id,),
        ).fetchone()
        options = reader.execute(
            "SELECT question, option FROM decision_options WHERE run_id = ? AND idx = 0 "
            "ORDER BY question, position",
            (event.run_id,),
        ).fetchall()
    finally:
        reader.close()
    assert row == (event.seq, 0, "routed", "groq_chat", "groq")
    assert options == [("memory_kind", "semantic"), ("route", "groq_chat")]
    capture_161(
        "decision-row-survives-a-crash.txt",
        "ticket 161 - a session reads the decisions row after decide() returns and the process "
        f"then dies before the handler starts\nrow: {row}\noptions: {options}\n",
    )


def test_a_fallback_writes_a_row_with_no_options_and_no_cost(conn: sqlite3.Connection) -> None:
    stub = _StubDecider(raises=DecisionTimeout("slow"))
    event = _announced(run_id="run_fallback")
    route.decide(stub, "Task: ping\n", conn=conn, announced=event, task="ping", model=MODEL)
    row = conn.execute(
        "SELECT outcome, route, handler, fallback_cause, model, cost, cost_nanos, decision_id "
        "FROM decisions WHERE run_id = ?",
        (event.run_id,),
    ).fetchone()
    assert row == ("fallback", None, None, "timeout", MODEL, None, None, None)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM decision_options WHERE run_id = ?", (event.run_id,)
        ).fetchone()[0]
        == 0
    )


# ---- route.py: loop.route.decided is published once per decision (ticket 162, FR-109) -----------


def test_a_routed_decision_publishes_once_with_the_eight_keys_as_strings(
    conn: sqlite3.Connection,
) -> None:
    published: list[tuple[str, dict[str, object]]] = []
    stub = _StubDecider(decisions=_decisions("groq_chat", "0.93"))
    event = _announced(run_id="run_published")
    verdict = route.decide(
        stub,
        "Task: ping\n",
        conn=conn,
        announced=event,
        task="ping",
        model=MODEL,
        publish=lambda name, payload: published.append((name, dict(payload))),
    )
    assert verdict.outcome == "routed"
    assert len(published) == 1
    name, payload = published[0]
    assert name == "loop.route.decided"
    assert payload == {
        "decision": "0",
        "hop": "0",
        "route": "groq_chat",
        "handler": "groq",
        "outcome": "routed",
        "confidence": "0.9300",
        "confidence_percent": "93%",
        "memory_kind": "semantic",
    }
    assert all(isinstance(value, str) for value in payload.values())
    capture_162(
        "loop-route-decided-payload.txt",
        f"ticket 162 - loop.route.decided published for a routed decision\npayload: {payload}\n",
    )


def test_a_fallback_still_publishes_once_with_the_null_columns_as_none(
    conn: sqlite3.Connection,
) -> None:
    published: list[tuple[str, dict[str, object]]] = []
    stub = _StubDecider(raises=DecisionTimeout("slow"))
    event = _announced(run_id="run_fallback_published")
    route.decide(
        stub,
        "Task: ping\n",
        conn=conn,
        announced=event,
        task="ping",
        model=MODEL,
        publish=lambda name, payload: published.append((name, dict(payload))),
    )
    assert len(published) == 1
    name, payload = published[0]
    assert name == "loop.route.decided"
    assert payload == {
        "decision": "0",
        "hop": "0",
        "route": None,
        "handler": None,
        "outcome": "fallback",
        "confidence": None,
        "confidence_percent": None,
        "memory_kind": None,
    }


def test_no_publish_argument_is_a_quiet_opt_out(conn: sqlite3.Connection) -> None:
    """The narrow ``route.decide`` tests above ``publish`` nothing; a missing ``publish`` must
    never raise, so a caller with no bus (every other test in this module) is unaffected."""
    stub = _StubDecider(decisions=_decisions("groq_chat", "0.93"))
    verdict = route.decide(
        stub,
        "Task: ping\n",
        conn=conn,
        announced=_announced(run_id="run_no_publish"),
        task="ping",
        model=MODEL,
    )
    assert verdict.outcome == "routed"


def test_dispatch_threads_publish_through_to_decide(conn: sqlite3.Connection) -> None:
    published: list[str] = []
    stub = _StubDecider(decisions=_decisions("groq_chat", "0.93"))
    routed.dispatch(
        conn,
        stub,
        "ping",
        announced=_announced(run_id="run_dispatch_publish"),
        model=MODEL,
        publish=lambda name, _payload: published.append(name),
    )
    assert published == ["loop.route.decided"]


# ---- route.py: rounding, once, in router/decisions.py (AC3, MUST rule 29) -----------------------


def test_a_probability_is_a_four_decimal_and_a_whole_percent_string_half_up(
    conn: sqlite3.Connection,
) -> None:
    stub = _StubDecider(decisions=_decisions("groq_chat", "0.96125", needs_memory="0.965"))
    event = _announced(run_id="run_round")
    route.decide(stub, "Task: ping\n", conn=conn, announced=event, task="ping", model=MODEL)
    row = conn.execute(
        "SELECT confidence, confidence_percent, needs_memory, needs_memory_percent, "
        "floor, floor_percent FROM decisions WHERE run_id = ?",
        (event.run_id,),
    ).fetchone()
    assert row == ("0.9613", "96%", "0.9650", "97%", "0.5000", "50%")
    capture_161(
        "rounding.txt",
        "ticket 161 - 0.96125 and 0.965, each a four-decimal string and a whole-percent string, "
        f"both half-up from the raw value\nconfidence, confidence_percent, needs_memory, "
        f"needs_memory_percent, floor, floor_percent = {row}\n",
    )


def test_cost_is_the_exact_text_and_cost_nanos_is_its_integer_twin(
    conn: sqlite3.Connection,
) -> None:
    stub = _StubDecider(decisions=_decisions("groq_chat", "0.93", cost="0.0000158"))
    event = _announced(run_id="run_cost")
    route.decide(stub, "Task: ping\n", conn=conn, announced=event, task="ping", model=MODEL)
    row = conn.execute(
        "SELECT cost, cost_nanos, input_tokens, output_tokens FROM decisions WHERE run_id = ?",
        (event.run_id,),
    ).fetchone()
    assert row == ("0.0000158", 15800, 420, 22)


def test_round_four_and_round_percent_are_each_computed_once_from_the_raw_value() -> None:
    """MUST rule 29's own anti-pattern, named: neither string is derived from the other."""
    assert decisions_store.round_four(Decimal("0.96125")) == "0.9613"
    assert decisions_store.round_percent(Decimal("0.96125")) == "96%"
    assert decisions_store.round_four(Decimal("0.965")) == "0.9650"
    assert decisions_store.round_percent(Decimal("0.965")) == "97%"
    assert decisions_store.to_nanos("0.0000158") == 15800


# ---- probe_state: GET /v1/router's read (ticket 186, FR-121, decision 67, G-32) ------------------

ROUTER_MODEL = "typesafe/jev-1.13-20260917"


def _write_router_agents(directory: Path, *entries: str) -> Path:
    target = directory / "agents.toml"
    target.write_text("\n".join(entries), encoding="utf-8")
    return target


def test_probe_state_with_no_recorded_probe_is_all_nulls_but_probed_false(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """AC1: no live rung ever recorded, so every probe field is null and `probed` is false, but
    the floors and the needs-memory threshold are still served - they come from `agents.toml`
    and the fixed vocabulary, never from a probe."""
    state_row = decisions_store.probe_state(conn, tmp_path / "agents.toml")
    assert (
        state_row.model,
        state_row.status,
        state_row.latency_seconds,
        state_row.cost,
        state_row.detail,
        state_row.probed_at,
    ) == (None, None, None, None, None, None)
    assert state_row.probed is False
    assert [(row.route, row.floor, row.floor_percent) for row in state_row.floors] == [
        ("groq_chat", "0.5000", "50%"),
        ("groq_memory", "0.5000", "50%"),
    ]
    assert (state_row.needs_memory_threshold, state_row.needs_memory_threshold_percent) == (
        "0.5000",
        "50%",
    )


def test_probe_state_reads_the_newest_row_when_several_are_recorded(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    decisions_store.record_probe(
        conn,
        model=ROUTER_MODEL,
        status=decisions_store.PASSED,
        latency_seconds="0.500",
        cost="0.0010",
        detail="pass: the older probe",
    )
    decisions_store.record_probe(
        conn,
        model=ROUTER_MODEL,
        status=decisions_store.PASSED,
        latency_seconds="0.842",
        cost="0.0031",
        detail="pass: the newest probe",
    )
    state_row = decisions_store.probe_state(conn, tmp_path / "agents.toml")
    assert state_row.probed is True
    assert state_row.model == ROUTER_MODEL
    assert state_row.status == decisions_store.PASSED
    assert state_row.latency_seconds == "0.842"
    assert state_row.cost == "0.0031"
    assert state_row.detail == "pass: the newest probe"
    assert state_row.probed_at != ""


def test_probe_state_a_failed_probe_is_probed_true_with_cost_null(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A failed probe still names its model, status and detail - only its cost is null, because
    a refusal or a transport error means no usage was ever reported to round (`record_probe`'s own
    rule)."""
    decisions_store.record_probe(
        conn,
        model=ROUTER_MODEL,
        status=decisions_store.FAILED,
        latency_seconds="1.203",
        cost=None,
        detail="fail: DecisionTimeout",
    )
    state_row = decisions_store.probe_state(conn, tmp_path / "agents.toml")
    assert state_row.probed is True
    assert state_row.status == decisions_store.FAILED
    assert state_row.cost is None
    assert state_row.detail == "fail: DecisionTimeout"


def test_probe_state_floors_are_agentstoml_per_entry_in_registry_order(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    target = _write_router_agents(
        tmp_path,
        f"[agents.custom-floor]\ncommand = {json.dumps(sys.executable)}\nfloor = 0.9\n",
        f"[agents.default-floor]\ncommand = {json.dumps(sys.executable)}\n",
    )
    state_row = decisions_store.probe_state(conn, target)
    assert [(row.route, row.floor, row.floor_percent) for row in state_row.floors] == [
        ("groq_chat", "0.5000", "50%"),
        ("groq_memory", "0.5000", "50%"),
        ("custom-floor", "0.9000", "90%"),
        ("default-floor", "0.8000", "80%"),
    ]
    assert Decimal("0.9000") == Decimal(str(0.9))


def test_probe_state_json_payload_is_the_wire_shape() -> None:
    state_row = decisions_store.RouterState(
        model=None,
        probed=False,
        status=None,
        latency_seconds=None,
        cost=None,
        detail=None,
        probed_at=None,
        floors=(
            decisions_store.RouteFloor(route="groq_chat", floor="0.5000", floor_percent="50%"),
        ),
        needs_memory_threshold="0.5000",
        needs_memory_threshold_percent="50%",
        routing="on",
        routing_reason=None,
    )
    payload = decisions_store.router_json_payload(state_row)
    assert set(payload) == {
        "routing",
        "routing_reason",
        "model",
        "probed",
        "status",
        "latency_seconds",
        "cost",
        "detail",
        "probed_at",
        "floors",
        "needs_memory_threshold",
        "needs_memory_threshold_percent",
    }
    assert payload["floors"] == ({"route": "groq_chat", "floor": "0.5000", "floor_percent": "50%"},)


def test_router_status_prints_never_probed_in_plain_and_matches_json(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    plain = CliRunner(mix_stderr=False).invoke(cli_app, ["router", "status"])
    assert plain.exit_code == 0, plain.stderr
    assert "never probed" in plain.stdout
    as_json = CliRunner(mix_stderr=False).invoke(cli_app, ["router", "status", "--json"])
    assert as_json.exit_code == 0, as_json.stderr
    payload = json.loads(as_json.stdout)
    assert payload["probed"] is False
    assert payload["model"] is None


def test_router_status_plain_names_the_model_and_the_badge_word_when_probed(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    decisions_store.record_probe(
        conn,
        model=ROUTER_MODEL,
        status=decisions_store.PASSED,
        latency_seconds="0.842",
        cost="0.0031",
        detail="pass: latency 0.842s, cost 0.0031",
    )
    plain = CliRunner(mix_stderr=False).invoke(cli_app, ["router", "status"])
    assert plain.exit_code == 0, plain.stderr
    assert ROUTER_MODEL in plain.stdout
    assert decisions_store.PASSED in plain.stdout


def test_the_task_is_scrubbed_before_it_reaches_the_row(conn: sqlite3.Connection) -> None:
    secret = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDDEEEEFFFF"
    stub = _StubDecider(decisions=_decisions("groq_chat", "0.93"))
    event = _announced(run_id="run_secret")
    route.decide(
        stub,
        "Task: remember this key\n",
        conn=conn,
        announced=event,
        task=f"remember this key {secret}",
        model=MODEL,
    )
    (task,) = conn.execute(
        "SELECT task FROM decisions WHERE run_id = ?", (event.run_id,)
    ).fetchone()
    assert secret not in task
    assert redact.token("anthropic_key") in task


# ---- cli/routed.py: the dispatch (AC6, AC7) ------------------------------------------------------


@pytest.fixture
def fake_decider() -> FakeDecider:
    from harnessed import config

    return FakeDecider(config.load())


def test_ping_dispatches_quietly_through_the_committed_fixture(
    conn: sqlite3.Connection, fake_decider: FakeDecider
) -> None:
    """The prompt every v4 test sends: routed.py must not raise for it (the ticket's own note)."""
    routed.dispatch(conn, fake_decider, "ping", announced=_announced(), model=MODEL)


def test_a_prompt_no_fixture_was_ever_written_for_still_routes(
    conn: sqlite3.Connection, fake_decider: FakeDecider
) -> None:
    routed.dispatch(
        conn,
        fake_decider,
        "a prompt no fixture was ever written for",
        announced=_announced(),
        model=MODEL,
    )


def test_dispatch_settles_a_needs_human_stop_without_raising(conn: sqlite3.Connection) -> None:
    """Ticket 164: an uncertain stop is settled, not raised (AC1, AC2)."""
    stub = _StubDecider(decisions=_decisions("needs_human", "0.9"))
    verdict = routed.dispatch(conn, stub, "destroy production", announced=_announced(), model=MODEL)
    assert verdict.outcome == "uncertain"
    assert verdict.stop_reason == "needs_human"


def test_dispatch_settles_a_below_floor_stop_without_raising(conn: sqlite3.Connection) -> None:
    stub = _StubDecider(decisions=_decisions("groq_memory", "0.43"))
    verdict = routed.dispatch(conn, stub, "ping", announced=_announced(), model=MODEL)
    assert verdict.outcome == "uncertain"
    assert verdict.stop_reason == "below_floor"


@pytest.mark.parametrize(
    ("raised", "cause"),
    [
        (DecisionUnreachable("gone"), "unreachable"),
        (DecisionTimeout("slow"), "timeout"),
        (DecisionRefused("a non-2xx"), "refused"),
        (DecisionRefused("a refused body"), "refused"),
        (DecisionRefused("an invalid answer"), "refused"),
    ],
    ids=["unreachable", "timeout", "non-2xx", "refused-body", "invalid-answer"],
)
def test_dispatch_no_longer_raises_for_a_fallback_verdict(
    raised: Exception, cause: str, conn: sqlite3.Connection
) -> None:
    """Ticket 165: the one shape ticket 164 left raising now dispatches like any other verdict."""
    stub = _StubDecider(raises=raised)
    verdict = routed.dispatch(conn, stub, "ping", announced=_announced(), model=MODEL)
    assert verdict.outcome == "fallback"
    assert verdict.fallback_cause == cause


def test_fallback_payload_names_the_cause_and_never_a_stop_reason(
    conn: sqlite3.Connection,
) -> None:
    """The payload ``_converse`` publishes as ``error.raised``: a cause, never a raised exception's
    class or a ``stop_reason`` - the run has not ended, unlike ``run_cmd.settle_raised``'s shape."""
    stub = _StubDecider(raises=DecisionTimeout("slow"))
    verdict = routed.dispatch(conn, stub, "ping", announced=_announced(), model=MODEL)
    payload = routed.fallback_payload(verdict)
    assert payload["cause"] == "timeout"
    assert "timeout" in str(payload["message"])
    assert "stop_reason" not in payload


# ---- AC1, AC2: the message ends as an uncertain stop, with 0 loop iterations, 0 delegations, 0 --
# ---- provider calls, no episode, then loop.route.decided, then run.finished ----------------------


def _uncertain_session(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, scope: str
) -> tuple[Session, ScopedFakeDecider, RaisingProvider]:
    from harnessed import config
    from harnessed.cli import run_cmd

    settings = config.load()
    decider = ScopedFakeDecider(fake=FakeDecider(settings), scope=scope)
    provider = RaisingProvider()
    monkeypatch.setattr(run_cmd, "run_loop", _never_called)
    session = Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=default_registry(settings.home),
        provider=provider,
        decider=decider,
    )
    return session, decider, provider


def _never_called(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("run_loop must not be called on an uncertain stop")


def _events(conn: sqlite3.Connection, run_id: str) -> list[tuple[int, str, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT seq, name, payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall()
    return [(int(seq), str(name), json.loads(payload)) for seq, name, payload in rows]


def test_below_floor_ends_as_an_uncertain_stop_with_zero_of_everything(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harnessed.cli import run_cmd

    session, decider, provider = _uncertain_session(conn, monkeypatch, "uncertain-stop")
    memory_run = run_cmd.solve_with_memory(session, "ping")

    assert memory_run.verdict is not None
    assert memory_run.verdict.outcome == "uncertain"
    assert memory_run.verdict.stop_reason == "below_floor"
    assert memory_run.verdict.floor == Decimal("0.50")
    assert decider.calls == 1
    assert provider.calls == 0, "0 provider calls"

    (delegations,) = conn.execute("SELECT COUNT(*) FROM delegations").fetchone()
    assert delegations == 0, "0 delegations"
    assert episodic.for_run(conn, memory_run.run_id) == (), "no episode written"

    (row,) = conn.execute(
        "SELECT outcome, stop_reason, floor FROM decisions WHERE run_id = ?",
        (memory_run.run_id,),
    ).fetchall()
    assert row == ("uncertain", "below_floor", "0.5000")

    names = [name for _, name, _ in _events(conn, memory_run.run_id)]
    assert names == ["run.started", "loop.route.decided", "run.finished"]
    _, _, finished_payload = _events(conn, memory_run.run_id)[-1]
    assert finished_payload == {"guardrail": None, "stop_reason": "below_floor"}
    capture_164(
        "below-floor-stop.txt",
        f"ticket 164 - below_floor stop on {memory_run.run_id}\n"
        f"decider calls: {decider.calls}, provider calls: {provider.calls}, "
        f"delegations: {delegations}, "
        f"episode rows: {len(episodic.for_run(conn, memory_run.run_id))}\n"
        + "\n".join(names)
        + f"\nrun.finished payload: {finished_payload}\n",
    )


def test_needs_human_ends_as_the_same_uncertain_stop(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harnessed.cli import run_cmd

    session, decider, provider = _uncertain_session(conn, monkeypatch, "uncertain-stop")
    memory_run = run_cmd.solve_with_memory(session, "destroy production")

    assert memory_run.verdict is not None
    assert memory_run.verdict.outcome == "uncertain"
    assert memory_run.verdict.stop_reason == "needs_human"
    assert memory_run.verdict.floor is None
    assert provider.calls == 0

    (delegations,) = conn.execute("SELECT COUNT(*) FROM delegations").fetchone()
    assert delegations == 0
    assert episodic.for_run(conn, memory_run.run_id) == ()

    names = [name for _, name, _ in _events(conn, memory_run.run_id)]
    assert names == ["run.started", "loop.route.decided", "run.finished"]
    _, _, finished_payload = _events(conn, memory_run.run_id)[-1]
    assert finished_payload == {"guardrail": None, "stop_reason": "needs_human"}
    capture_164(
        "needs-human-stop.txt",
        f"ticket 164 - needs_human stop on {memory_run.run_id}\n"
        f"decider calls: {decider.calls}, provider calls: {provider.calls}, "
        f"delegations: {delegations}, "
        f"episode rows: {len(episodic.for_run(conn, memory_run.run_id))}\n"
        + "\n".join(names)
        + f"\nrun.finished payload: {finished_payload}\n",
    )


# ---- AC1, AC2, AC3: a fallback publishes error.raised with its cause, stores a row with no -------
# ---- options, and completes through groq_chat via the v4 gate, unchanged, never an agent ---------

EVIDENCE_165 = REPO_ROOT / "evidence" / "165"

#: One transport stub per cause the ticket names: unreachable, a call still open at 3s (timeout),
#: a non-2xx, a refused body and an invalid answer - the last three collapse to ``refused``
#: (``route.py``'s own docstring: ``DecisionRefused`` covers a non-2xx and a parsed-but-invalid
#: answer alike).
FALLBACK_STUBS: Final = (
    ("unreachable", DecisionUnreachable("could not reach the Decisions API")),
    ("timeout", DecisionTimeout("the Decisions API did not answer within 3.0s")),
    ("non-2xx", DecisionRefused("the Decisions API answered 500")),
    ("refused-body", DecisionRefused("the Decisions API answered a body that is not JSON")),
    ("invalid-answer", DecisionRefused("the Decisions API gave no answer for 'route'")),
)


def capture_165(name: str, text: str) -> Path:
    EVIDENCE_165.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_165 / name
    path.write_text(text, encoding="utf-8")
    return path


def test_every_transport_stub_completes_through_groq_chat(conn: sqlite3.Connection) -> None:
    from harnessed import config
    from harnessed.cli import run_cmd
    from harnessed.providers.fake import FakeProvider

    settings = config.load(provider="fake")
    lines: list[str] = []
    for label, raised in FALLBACK_STUBS:
        decider = _StubDecider(raises=raised)
        session = Session(
            settings=settings,
            conn=conn,
            bus=EventBus(conn=conn),
            tools=run_cmd.routed_registry(settings.home),
            provider=FakeProvider(settings),
            decider=decider,
        )
        memory_run = run_cmd.solve_with_memory(session, "ping")

        # AC: the message completes through groq_chat - a real reply, not an uncertain stop.
        assert memory_run.verdict is None
        assert memory_run.result is not None

        events = _events(conn, memory_run.run_id)
        names = [name for _, name, _ in events]
        assert names[0] == "run.started"
        assert names[-1] == "run.finished"

        # AC: error.raised, published with the cause.
        raised_payloads = [payload for _, name, payload in events if name == "error.raised"]
        assert len(raised_payloads) == 1
        cause = raised_payloads[0]["cause"]

        # AC: a fallback row, outcome fallback, no route, no handler, no options.
        (row,) = conn.execute(
            "SELECT outcome, route, handler, fallback_cause FROM decisions WHERE run_id = ?",
            (memory_run.run_id,),
        ).fetchall()
        assert row == ("fallback", None, None, cause)
        (option_count,) = conn.execute(
            "SELECT COUNT(*) FROM decision_options WHERE run_id = ?", (memory_run.run_id,)
        ).fetchone()
        assert option_count == 0

        # AC: never an agent route - no delegation was ever attempted.
        (delegations,) = conn.execute("SELECT COUNT(*) FROM delegations").fetchone()
        assert delegations == 0

        # AC (168's deferred criterion, decision 60): a fallback's registry holds no
        # delegate_to_agent - it never gives Groq the tool back.
        assert "delegate_to_agent" not in session.tools.names()

        # AC: the v4 gate decided it, exactly as v4 - its own decided/failed-open event fired.
        assert any(name.startswith("retrieval.gate.") for name in names)

        lines.append(
            f"{label}: published cause={cause!r}, row={row}, reply={memory_run.result.reply!r}"
        )

    capture_165(
        "fallback-completes-through-groq-chat.txt",
        "ticket 165 - each transport stub publishes error.raised with its cause, stores a "
        "fallback row with no options, and completes through groq_chat via the v4 gate\n"
        + "\n".join(lines)
        + "\n",
    )


# ---- AC1: one Decider call per message, four routed doors, three that make none ------------------

RUN_TIMEOUT_S = 15.0


@pytest.fixture
def decider_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Counts every call through ``route.decide``, the one choke point every door calls through."""
    calls: list[str] = []
    real = route.decide

    def spy(
        decider: object,
        state_text: str,
        *,
        conn: sqlite3.Connection,
        announced: Event,
        task: str,
        model: str,
        state_truncated: bool = False,
        publish: route.Publisher | None = None,
        agents: Mapping[str, AgentSpec] | None = None,
        hop: int = 0,
        exclude: tuple[str, ...] = (),
        continuation: bool = False,
    ) -> route.Verdict:
        calls.append(state_text)
        return real(  # type: ignore[arg-type]
            decider,
            state_text,
            conn=conn,
            announced=announced,
            task=task,
            model=model,
            state_truncated=state_truncated,
            publish=publish,
            agents=agents,
            hop=hop,
            exclude=exclude,
            continuation=continuation,
        )

    monkeypatch.setattr(route, "decide", spy)
    return calls


def test_harnessed_run_makes_exactly_one_decider_call(
    clean_env: None, tmp_harnessed_home: Path, decider_calls: list[str]
) -> None:
    result = CliRunner(mix_stderr=False).invoke(cli_app, ["run", "--json", "ping"])
    assert result.exit_code == 0, result.output
    assert len(decider_calls) == 1


def test_harnessed_run_session_makes_one_more_decider_call_for_the_follow_up(
    clean_env: None, tmp_harnessed_home: Path, decider_calls: list[str]
) -> None:
    runner = CliRunner(mix_stderr=False)
    first = runner.invoke(cli_app, ["run", "--json", "ping"])
    assert first.exit_code == 0, first.output
    key = json.loads(first.stdout)["run_key"]
    decider_calls.clear()

    second = runner.invoke(cli_app, ["run", "--json", "--session", str(key), "ping again"])
    assert second.exit_code == 0, second.output
    assert len(decider_calls) == 1


def test_post_control_v1_runs_makes_exactly_one_decider_call(
    clean_env: None, tmp_harnessed_home: Path, decider_calls: list[str]
) -> None:
    app = control.create_app(tmp_harnessed_home)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/control/v1/runs", json={"prompt": "hello"})
        assert response.status_code == 202, response.text
        app.state.worker.run.result(timeout=RUN_TIMEOUT_S)
    assert len(decider_calls) == 1


def test_post_control_v1_runs_messages_makes_exactly_one_decider_call(
    clean_env: None, tmp_harnessed_home: Path, decider_calls: list[str]
) -> None:
    """``ping``/``ping again``: the two turns need an initiating run that actually finishes and
    records a message, or the follow-up has nothing to continue - the fake corpus answers both."""
    app = control.create_app(tmp_harnessed_home)
    with TestClient(app, raise_server_exceptions=False) as client:
        opened = client.post("/control/v1/runs", json={"prompt": "ping"})
        assert opened.status_code == 202, opened.text
        app.state.worker.run.result(timeout=RUN_TIMEOUT_S)
        session_id = opened.json()["session_id"]
        decider_calls.clear()

        followed = client.post(
            f"/control/v1/runs/{session_id}/messages", json={"prompt": "ping again"}
        )
        assert followed.status_code == 202, followed.text
        app.state.worker.run.result(timeout=RUN_TIMEOUT_S)
    assert len(decider_calls) == 1


def test_eval_run_improve_run_and_improve_promote_make_no_decider_call(
    clean_env: None, tmp_harnessed_home: Path, decider_calls: list[str]
) -> None:
    """FR-119: none of the three eval/improve doors passes ``routed=True`` to ``open_session``."""
    runner = CliRunner(mix_stderr=False)

    evaluated = runner.invoke(cli_app, ["eval", "run", "smoke", "--fresh"])
    assert evaluated.exit_code == 0, evaluated.output
    assert decider_calls == []

    dry_run = runner.invoke(cli_app, ["improve", "run", "--dry-run", "--json"])
    assert dry_run.exit_code == 0, dry_run.stderr
    payload: dict[str, Any] = json.loads(dry_run.stdout)
    assert decider_calls == []

    promoted = runner.invoke(cli_app, ["improve", "promote", str(payload["cand_id"]), "--json"])
    assert promoted.exit_code == 0, promoted.stderr
    assert decider_calls == []
    capture(
        "decider-call-counts.txt",
        "AC1 - one Decider call per message, per door\n"
        "harnessed run: 1 (test_harnessed_run_makes_exactly_one_decider_call)\n"
        "harnessed run --session (follow-up): 1 "
        "(test_harnessed_run_session_makes_one_more_decider_call_for_the_follow_up)\n"
        "POST /control/v1/runs: 1 (test_post_control_v1_runs_makes_exactly_one_decider_call)\n"
        "POST /control/v1/runs/{id}/messages: 1 "
        "(test_post_control_v1_runs_messages_makes_exactly_one_decider_call)\n"
        f"eval run smoke --fresh: {len(decider_calls)} (this test, first door)\n"
        f"improve run --dry-run: {len(decider_calls)} (this test, second door)\n"
        f"improve promote: {len(decider_calls)} (this test, third door)\n",
    )


# ---- ticket 179 (FR-118): the fallback flag and the v4 base instruction are deleted -------------

#: ``memory/gate.py`` is ``scope_out`` for ticket 179 and its own docstring still describes the
#: deleted flag by name (decision 7's fallback paragraph), so it is the one file this scan may
#: not fix and therefore does not scan.
_FLAG_SCAN_EXCEPTIONS: Final = frozenset({Path("src/harnessed/memory/gate.py")})


def test_the_fallback_flag_and_v4_base_instruction_are_gone_from_src() -> None:
    """AC2: once the flag is deleted, neither its env var name nor the v4 constant it chose
    between may appear anywhere under ``src/`` (the one file this ticket cannot touch, and whose
    own text still names the flag historically, is the documented exception above)."""
    src_root = REPO_ROOT / "src" / "harnessed"
    #: Word-bounded so ``HARNESSED_ROUTER_MODEL`` (the model pin, still live) never false-positives
    #: as a hit for the deleted ``HARNESSED_ROUTER`` - a bare substring check would flag every file
    #: that still names ``ROUTER_MODEL_VAR``.
    needles: dict[str, re.Pattern[str]] = {
        "HARNESSED_ROUTER": re.compile(r"\bHARNESSED_ROUTER\b"),
        "BASE_INSTRUCTION_V4": re.compile(r"\bBASE_INSTRUCTION_V4\b"),
    }
    hits: dict[str, list[str]] = {name: [] for name in needles}
    for path in sorted(src_root.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if relative in _FLAG_SCAN_EXCEPTIONS:
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in needles.items():
            if pattern.search(text):
                hits[name].append(relative.as_posix())
    assert hits["HARNESSED_ROUTER"] == []
    assert hits["BASE_INSTRUCTION_V4"] == []

    from harnessed import config
    from harnessed.loop import prompt

    assert not hasattr(config, "ROUTER_VAR")
    assert not hasattr(prompt, "BASE_INSTRUCTION_V4")
    assert "ROUTER_VAR" not in config.__all__
    assert "BASE_INSTRUCTION_V4" not in prompt.__all__

    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "HARNESSED_ROUTER=" not in env_example
    assert "HARNESSED_ROUTER_MODEL=" in env_example, "the model pin is a different variable"

    from tests.conftest import DOCUMENTED_ENV_VARS

    assert "HARNESSED_ROUTER" not in DOCUMENTED_ENV_VARS
    assert "HARNESSED_ROUTER_MODEL" in DOCUMENTED_ENV_VARS

    capture_179(
        "zero-occurrence-scan.txt",
        "src/ scanned for HARNESSED_ROUTER and BASE_INSTRUCTION_V4, excluding "
        f"{sorted(str(p) for p in _FLAG_SCAN_EXCEPTIONS)}\n"
        f"HARNESSED_ROUTER hits: {hits['HARNESSED_ROUTER']}\n"
        f"BASE_INSTRUCTION_V4 hits: {hits['BASE_INSTRUCTION_V4']}\n",
    )


def test_the_routed_registry_still_exposes_zero_delegate_schemas(tmp_path: Path) -> None:
    """AC2's last clause, the one part of it not already covered by the scan above."""
    from harnessed.cli import run_cmd

    tools = run_cmd.routed_registry(tmp_path)
    assert "delegate_to_agent" not in tools.names()


# ---- ticket 166 (FR-105): a Groq route runs the v4 loop, byte for byte --------------------------


def test_the_routed_and_unrouted_requests_over_the_same_fixture_differ_only_in_the_tool_registry(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 60: ``_converse`` still never branches on ``verdict.outcome == "routed"`` - a Groq
    route falls straight through into the exact same gate-then-loop path the unrouted message
    runs. Ticket 166 recorded 0 difference between the two requests and the two tool registries;
    ticket 168 first split the registries, by composing the routed one from
    ``run_cmd.routed_registry`` (every builtin tool minus ``delegate_to_agent``). Ticket 179
    deleted the v4 base instruction that used to also split the two ``ChatRequest``s - ``Session.
    system()`` is ``BASE_INSTRUCTION`` unconditionally now - so the requests captured here are
    byte-identical again; only the registry each session was opened with still differs.
    """
    from harnessed import config
    from harnessed.cli import run_cmd
    from harnessed.providers.fake import FakeProvider
    from harnessed.providers.types import ChatRequest

    settings = config.load(provider="fake")
    captured: list[ChatRequest] = []
    real = run_cmd.run_loop

    def spy(model: object, registry: object, request: ChatRequest, **kwargs: object) -> Any:
        captured.append(request)
        return real(model, registry, request, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(run_cmd, "run_loop", spy)

    routed_tools = run_cmd.routed_registry(settings.home)
    unrouted_tools = default_registry(settings.home)
    routed_session = Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=routed_tools,
        provider=FakeProvider(settings),
        decider=FakeDecider(settings),
    )
    unrouted_session = Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=unrouted_tools,
        provider=FakeProvider(settings),
        decider=None,
    )

    routed_run = run_cmd.solve_with_memory(routed_session, "ping")
    unrouted_run = run_cmd.solve_with_memory(unrouted_session, "ping")

    assert unrouted_run.verdict is None, "the unrouted session never routes at all"
    assert routed_run.verdict is None, "must have run through, not stopped uncertain or fallback"
    (outcome,) = conn.execute(
        "SELECT outcome FROM decisions WHERE run_id = ?", (routed_run.run_id,)
    ).fetchone()
    assert outcome == "routed"

    routed_request, unrouted_request = captured
    assert routed_request == unrouted_request, (
        "179 deleted the v4 base instruction: the model, the system prompt, the messages and the "
        "retrieval must all match now - only the registry each session was opened with differs"
    )

    assert "delegate_to_agent" not in routed_tools.names()
    assert "delegate_to_agent" in unrouted_tools.names()
    assert routed_tools.names() == tuple(
        name for name in unrouted_tools.names() if name != "delegate_to_agent"
    ), "168 drops delegate_to_agent from the routed registry alone"

    capture_168(
        "routed-vs-unrouted-request.txt",
        "the same `ping` fixture, once through a Decider (outcome=routed), once with no Decider\n"
        f"routed request:   {routed_request!r}\n"
        f"unrouted request: {unrouted_request!r}\n"
        f"identical: {routed_request == unrouted_request}\n"
        f"routed tools:   {routed_tools.names()}\n"
        f"unrouted tools: {unrouted_tools.names()}\n",
    )


# ---- ticket 167 (FR-106): an agent route delegates from code, and no model writes its args -------

EVIDENCE_167 = REPO_ROOT / "evidence" / "167"

STUB_ACP_PATH = REPO_ROOT / "tests" / "support" / "stub_acp_agent.py"
STUB_CLI_PATH = REPO_ROOT / "tests" / "support" / "stub_cli_agent.py"


def capture_167(name: str, text: str) -> Path:
    EVIDENCE_167.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_167 / name
    path.write_text(text, encoding="utf-8")
    return path


def _acp_spec(name: str = "probe") -> AgentSpec:
    """A real, runnable ``acp`` stub agent (default ``kind``), spawned through this interpreter."""
    return AgentSpec(name=name, command=sys.executable, args=(str(STUB_ACP_PATH),))


def _cli_spec(name: str = "probe-cli", *, cwd: Path | None = None) -> AgentSpec:
    """A real, runnable ``cli`` stub agent: the task on stdin, echoed back as the reply.

    ``cwd`` isolates the child from the repository root, the same way ``test_acp.py``'s own
    ``cli``-kind test does: under ``--cov`` the child inherits ``COVERAGE_PROCESS_START`` (only
    ``SECRET_VARS`` are stripped) and coverage's subprocess hook writes a data file into whatever
    the invoking pytest process's own cwd is, which ``cli_runner.run``'s write-detection snapshot
    then reports as the agent writing a file - a false positive this ticket found pre-existing
    (confirmed via ``git stash``, unrelated to delegation) and fixed by giving the spec its own
    ``cwd`` so the snapshot is never taken over a directory coverage also writes to.
    """
    return AgentSpec(
        name=name,
        command=sys.executable,
        args=(str(STUB_CLI_PATH),),
        kind="cli",
        input="stdin",
        cwd=str(cwd) if cwd is not None else None,
    )


def _agent_session(
    conn: sqlite3.Connection, agents: dict[str, AgentSpec], decider: Decider
) -> tuple[Session, _RecordingProvider]:
    """A session routed to an agent, over a recording FakeProvider: ticket 198's "0 provider calls
    between the decision and the delegation" is the ordering of its one request (the closing
    synthesis) after ``acp.delegation.finished``."""
    from harnessed import config
    from harnessed.providers.fake import FakeProvider

    settings = config.load(provider="fake")
    provider = _RecordingProvider(FakeProvider(settings))
    session = Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=default_registry(settings.home),
        provider=provider,  # type: ignore[arg-type]
        decider=decider,
        agents=agents,
    )
    return session, provider


class _SequenceDecider:
    """A Decider double that answers each call from a fixed, ordered list - the follow-up proof
    below needs a second, different answer for the same session's second message."""

    def __init__(self, *answers: Decisions) -> None:
        self._answers = list(answers)
        self.calls = 0

    def decide(self, state_text: str, asked: tuple[Question, ...]) -> Decisions:
        answer = self._answers[self.calls]
        self.calls += 1
        return answer


def test_decide_asks_the_registry_widened_route_question_when_agents_are_given(
    conn: sqlite3.Connection,
) -> None:
    """The wiring this ticket adds to ``decide()``'s live call (163/164 built the builder; this is
    what first calls it): the ``route`` question offered is the registry's, not the fixed three."""

    class _RecordingDecider:
        def __init__(self) -> None:
            self.asked: tuple[Question, ...] | None = None

        def decide(self, state_text: str, asked: tuple[Question, ...]) -> Decisions:
            self.asked = asked
            return _decisions("probe", "0.90")

    agents = {"probe": _spec("probe", use_for="a probe agent", not_for="anything urgent")}
    recorder = _RecordingDecider()
    verdict = route.decide(
        recorder,
        "Task: ping\n",
        conn=conn,
        announced=_announced(),
        task="ping",
        model=MODEL,
        agents=agents,
    )
    assert recorder.asked is not None
    route_question = next(q for q in recorder.asked if q.name == "route")
    assert route_question.options == ("groq_chat", "groq_memory", "probe", "needs_human")
    assert verdict.outcome == "routed"
    assert verdict.handler == "agent"
    assert verdict.route == "probe"


def test_an_acp_agent_route_delegates_by_code_with_zero_provider_calls_in_between(
    conn: sqlite3.Connection,
) -> None:
    from harnessed.cli import run_cmd

    spec = _acp_spec()
    session, provider = _agent_session(
        conn,
        {"probe": spec},
        _SequenceDecider(_decisions("probe", "0.90"), _decisions("done", "0.95")),
    )
    memory_run = run_cmd.solve_with_memory(session, "investigate the failing build")

    assert memory_run.verdict is None, "a routed agent turn is not an uncertain stop"
    assert memory_run.result is not None
    assert len(provider.requests) == 1, "one request: the closing synthesis"

    (outcome,) = conn.execute(
        "SELECT outcome FROM decisions WHERE run_id = ?", (memory_run.run_id,)
    ).fetchone()
    assert outcome == "routed"

    task, acceptance = conn.execute("SELECT task, acceptance FROM delegations").fetchone()
    assert task == routed.build_task("investigate the failing build"), "built in code"
    assert task.endswith(routed.NO_CO_AUTHOR)
    assert acceptance == routed.ACCEPTANCE, "decision 60's one static sentence"

    names = [name for _, name, _ in _events(conn, memory_run.run_id)]
    assert "acp.delegation.started" in names
    assert "acp.delegation.finished" in names
    assert any(name.startswith("acp.tool.") for name in names), "v4's acp.tool.* events, unchanged"
    assert names[-1] == "run.finished"
    assert names.index("loop.iteration.started") > names.index("acp.delegation.finished")

    capture_167(
        "acp-delegation.txt",
        f"task={task!r} acceptance={acceptance!r}\nreply={memory_run.result.reply!r}\n"
        + "\n".join(names)
        + "\n",
    )


def _activate_two(conn: sqlite3.Connection) -> tuple[str, str]:
    """Two active procedures, written straight to the active set (``activate`` keeps one)."""
    texts = ("always cite the file you read", "never guess a path")
    for text in texts:
        row = procedural.put(conn, text, "test")
        conn.execute(
            "INSERT INTO active_procedures (proc_id, activated_at, cand_id) VALUES (?, ?, NULL)",
            (row.proc_id, "2026-10-02T00:00:00Z"),
        )
    conn.commit()
    return texts


def test_rule_34_a_delegation_and_the_synthesis_carry_no_procedure_text(
    conn: sqlite3.Connection,
) -> None:
    from harnessed.cli import run_cmd

    texts = _activate_two(conn)
    session, provider = _agent_session(
        conn,
        {"probe": _acp_spec()},
        _SequenceDecider(_decisions("probe", "0.90"), _decisions("done", "0.95")),
    )
    memory_run = run_cmd.solve_with_memory(session, "investigate the failing build")

    assert memory_run.result is not None
    (acceptance,) = conn.execute("SELECT acceptance FROM delegations").fetchone()
    assert acceptance == routed.ACCEPTANCE
    started = [p for _, n, p in _events(conn, memory_run.run_id) if n == "acp.delegation.started"]
    assert started
    for blob in (acceptance, *(str(p) for p in started)):
        assert "<procedure" not in blob
        assert not any(text in blob for text in texts)

    (request,) = provider.requests
    assert request.system == SYNTHESIS_INSTRUCTION
    assert "<procedure" not in request.system
    assert session.synthesis_system() == SYNTHESIS_INSTRUCTION
    system = session.system()
    assert system == build_system_prompt(BASE_INSTRUCTION, texts)
    assert all(text in system for text in texts), "the Groq loop's own prompt keeps them"


def test_rule_34_the_router_state_carries_the_procedure_count_and_no_text(
    conn: sqlite3.Connection,
) -> None:
    texts = _activate_two(conn)
    built = state.build_state(conn, "what time is it in Lagos?")
    assert "2 procedures." in built.text
    assert not any(text in built.text for text in texts)


def test_a_cli_agent_route_delegates_by_code_and_writes_the_same_delegations_row(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd

    spec = _cli_spec(cwd=tmp_path)
    session, provider = _agent_session(
        conn,
        {"probe-cli": spec},
        _SequenceDecider(_decisions("probe-cli", "0.90"), _decisions("done", "0.95")),
    )
    memory_run = run_cmd.solve_with_memory(session, "summarize the incident")

    assert memory_run.result is not None
    (request,) = provider.requests
    user_turn = next(m.content for m in request.messages if m.role == "user")
    assert (
        "summarize the incident" in user_turn.split("</retrieved_context>")[0]
    ), "the stub echoes its stdin task, and it is the synthesis's retrieved context"
    names = [name for _, name, _ in _events(conn, memory_run.run_id)]
    assert names.index("loop.iteration.started") > names.index("acp.delegation.finished")

    task, acceptance = conn.execute("SELECT task, acceptance FROM delegations").fetchone()
    assert task == routed.build_task("summarize the incident")
    assert acceptance == routed.ACCEPTANCE

    (delegation_count,) = conn.execute("SELECT COUNT(*) FROM delegations").fetchone()
    assert delegation_count == 1, "the same delegations row a cli entry always writes"

    capture_167("cli-delegation.txt", f"task={task!r} reply={memory_run.result.reply!r}\n")


def test_the_agents_result_is_the_reply_and_a_groq_follow_up_resumes_with_it(
    conn: sqlite3.Connection,
) -> None:
    from harnessed import config
    from harnessed.cli import run_cmd
    from harnessed.providers.fake import FakeProvider

    settings = config.load(provider="fake")
    bus = EventBus(conn=conn)
    # The chain's own hop answers `done` after the agent returns (ticket 176), before the
    # follow-up message's own decision.
    decider = _SequenceDecider(
        _decisions("probe", "0.90"), _decisions("done", "0.95"), _decisions("groq_chat", "0.95")
    )
    session = Session(
        settings=settings,
        conn=conn,
        bus=bus,
        tools=default_registry(settings.home),
        provider=FakeProvider(settings),
        decider=decider,
        agents={"probe": _acp_spec()},
    )

    first = run_cmd.solve_with_memory(session, "ping")
    assert first.verdict is None
    assert first.result is not None
    reply = first.result.reply
    assert reply == "pong", "Groq's synthesis over the agent's result is the reply"

    rows = conn.execute(
        "SELECT role, content FROM run_messages WHERE run_id = ? ORDER BY idx", (first.run_id,)
    ).fetchall()
    assert [role for role, _ in rows] == ["user", "assistant"], "exactly one user, one assistant"
    assert rows[0][1].endswith("\n\nping") and "<retrieved_context>" in rows[0][1]
    assert rows[1][1] == reply

    # Two episodes: `delegate.py`'s own "delegation" transcript (unchanged) and this ticket's
    # "loop" episode - the one a later run's retrieval and a follow-up's episodic memory read.
    kinds = sorted(episode.kind for episode in episodic.for_run(conn, first.run_id))
    assert kinds == ["delegation", "loop"], "written back as a loop episode, alongside delegate.py"

    second = run_cmd.continue_session(session, first.run_id, "ping again")
    assert second.result is not None, "a following Groq message on the session resumes with it"

    capture_167(
        "follow-up-resumes.txt",
        f"first reply={reply!r}\nsecond reply={second.result.reply!r}\nrun_messages: {rows}\n",
    )


def test_an_uncertain_stop_leaves_record_exchange_nothing_to_do(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: an uncertain stop never reaches the agent branch at all, so it never calls
    ``record_exchange`` - proven the same way ticket 164 proved 0 delegations and no episode."""
    from harnessed.cli import run_cmd

    session, decider, provider = _uncertain_session(conn, monkeypatch, "uncertain-stop")
    memory_run = run_cmd.solve_with_memory(session, "ping")

    assert memory_run.verdict is not None
    assert memory_run.verdict.outcome == "uncertain"
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM run_messages WHERE run_id = ?", (memory_run.run_id,)
    ).fetchone()
    assert count == 0, "record_exchange appended nothing"


# ---- ticket 169: needs_memory replaces the gate's parsed yes or no -----------------------------


def test_a_routed_verdict_at_or_above_the_threshold_asks_the_gate_to_retrieve(
    conn: sqlite3.Connection,
) -> None:
    """AC1 (the 0.85 half): ``run_cmd._router_memory`` compares the verdict's own ``needs_memory``
    against ``questions.NEEDS_MEMORY_THRESHOLD``, never a value ``memory/`` reads itself."""
    from harnessed.cli import run_cmd

    stub = _StubDecider(decisions=_decisions("groq_chat", "0.93", needs_memory="0.85"))
    verdict = route.decide(
        stub, "Task: ping\n", conn=conn, announced=_announced(), task="ping", model=MODEL
    )

    answered = run_cmd._router_memory(verdict)

    assert answered.retrieve is True
    assert answered.answer == "0.85"
    capture_169(
        "threshold-retrieves.txt",
        f"needs_memory=0.85 >= {questions.NEEDS_MEMORY_THRESHOLD}: RouterMemory={answered}\n",
    )


def test_a_routed_verdict_below_the_threshold_asks_the_gate_to_skip(
    conn: sqlite3.Connection,
) -> None:
    """AC1 (the 0.20 half)."""
    from harnessed.cli import run_cmd

    stub = _StubDecider(decisions=_decisions("groq_chat", "0.93", needs_memory="0.20"))
    verdict = route.decide(
        stub, "Task: ping\n", conn=conn, announced=_announced(), task="ping", model=MODEL
    )

    answered = run_cmd._router_memory(verdict)

    assert answered.retrieve is False
    assert answered.answer == "0.20"
    capture_169(
        "threshold-skips.txt",
        f"needs_memory=0.20 < {questions.NEEDS_MEMORY_THRESHOLD}: RouterMemory={answered}\n",
    )


def test_a_verdict_with_no_needs_memory_fails_open(conn: sqlite3.Connection) -> None:
    """AC3: `route.decide` never actually produces this for a routed verdict today (only a
    fallback does, and a fallback never reaches `_router_memory` at all - it leaves `answered`
    `None` and runs the unchanged v4 gate). This is the defensive branch: a `Verdict` naming no
    `needs_memory` still fails open rather than raising comparing `None` against the threshold.
    """
    from harnessed.cli import run_cmd

    verdict = route.Verdict(
        outcome="routed",
        handler="groq",
        route="groq_chat",
        confidence=Decimal("0.93"),
        floor=Decimal("0.50"),
        stop_reason=None,
        needs_memory=None,
        memory_kind=None,
        state_truncated=False,
        fallback_cause=None,
        decisions=None,
    )

    answered = run_cmd._router_memory(verdict)

    assert answered.retrieve is None
    assert answered.answer == ""


def test_a_fallback_verdict_is_never_turned_into_a_router_memory(
    conn: sqlite3.Connection,
) -> None:
    """AC2: a fallback verdict carries no `needs_memory` either (`route._fallback` sets it
    `None`), but `_converse` never calls `_router_memory` for one at all - `answered` stays `None`
    and the v4 gate runs unchanged, which is the whole of what keeps every existing
    `test_retrieval_gate.py` test passing unmodified. This asserts the fallback verdict shape
    that guarantees it: `_converse` branches on `outcome == "fallback"` before it would ever call
    `_router_memory`.
    """
    stub = _StubDecider(raises=DecisionUnreachable("no route"))
    verdict = route.decide(
        stub, "Task: ping\n", conn=conn, announced=_announced(), task="ping", model=MODEL
    )

    assert verdict.outcome == "fallback"
    assert verdict.needs_memory is None

    capture_169(
        "fallback-runs-v4-unchanged.txt",
        f"fallback verdict: outcome={verdict.outcome!r}, needs_memory={verdict.needs_memory!r}\n"
        "_converse branches on outcome == 'fallback' before _router_memory could be called: "
        "answered stays None and the v4 gate runs unchanged (0 test_retrieval_gate.py tests "
        "modified for this ticket, only extended).\n",
    )


# ---- ticket 170: memory_kind scopes which stores retrieval searches ----------------------------


@pytest.mark.parametrize(
    ("memory_kind", "expected_kinds"),
    [
        ("semantic", ("fact",)),
        ("episodic", ("episode",)),
        ("procedural", ()),
    ],
)
def test_router_memory_kinds_follow_the_verdicts_own_memory_kind(
    conn: sqlite3.Connection, memory_kind: str, expected_kinds: tuple[str, ...]
) -> None:
    """AC1: a test per `memory_kind` answer asserts the `kinds` `_router_memory` builds - what
    `gate.assemble_if_gated` carries straight through as `retrieval.assemble`'s own `kinds`
    keyword (`memory/gate.py::assemble_if_gated`'s `resolved_kinds = ... answered.kinds`)."""
    from harnessed.cli import run_cmd

    stub = _StubDecider(
        decisions=_decisions("groq_chat", "0.93", needs_memory="0.85", memory_kind=memory_kind)
    )
    verdict = route.decide(
        stub, "Task: ping\n", conn=conn, announced=_announced(), task="ping", model=MODEL
    )

    answered = run_cmd._router_memory(verdict)

    assert answered.kinds == expected_kinds
    capture_170(
        f"kinds-for-{memory_kind}.txt",
        f"memory_kind={memory_kind!r}: RouterMemory.kinds={answered.kinds!r}\n",
    )


def test_router_memory_kinds_follow_memory_kind_even_when_needs_memory_fails_open(
    conn: sqlite3.Connection,
) -> None:
    """AC1's edge: a `Verdict` with no `needs_memory` still carries `memory_kind` (the two
    questions are answered independently in the one Decider call), so `_router_memory`'s
    fail-open branch maps it too - the gate's fail-open still retrieves scoped, not by the wide
    pre-170 default."""
    from harnessed.cli import run_cmd

    verdict = route.Verdict(
        outcome="routed",
        handler="groq",
        route="groq_chat",
        confidence=Decimal("0.93"),
        floor=Decimal("0.50"),
        stop_reason=None,
        needs_memory=None,
        memory_kind="episodic",
        state_truncated=False,
        fallback_cause=None,
        decisions=None,
    )

    answered = run_cmd._router_memory(verdict)

    assert answered.retrieve is None
    assert answered.kinds == ("episode",)


# ---- ticket 176: after an agent returns, the router picks the next route or done ---------------

EVIDENCE_176 = REPO_ROOT / "evidence" / "176"

#: The chain's operator text (FR-117's own example).
CHAIN_TASK: Final = "design a wireframe for X, then draft the launch email"
#: What the two stub `cli` entries print, whatever their task: a fixed output keeps each hop's
#: `Previous step:` distinct, which is what the committed chain fixtures select their variant on.
WIREFRAME_OUTPUT: Final = "WIREFRAME: a header, a hero and one signup form."
EMAIL_OUTPUT: Final = "EMAIL: Subject: X is live. Body: sign up from the new form."


def capture_176(name: str, text: str) -> Path:
    EVIDENCE_176.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_176 / name
    path.write_text(text, encoding="utf-8")
    return path


def _fixed_cli_spec(name: str, output: str, cwd: Path) -> AgentSpec:
    """A stub `cli` entry that prints ``output`` whatever it is asked (its task on argv, unused)."""
    return AgentSpec(
        name=name,
        command=sys.executable,
        args=(str(STUB_CLI_PATH), "--task", output),
        kind="cli",
        input="argv",
        cwd=str(cwd),
    )


def _chain_agents(tmp_path: Path) -> dict[str, AgentSpec]:
    return {
        "wireframe": _fixed_cli_spec("wireframe", WIREFRAME_OUTPUT, tmp_path),
        "email": _fixed_cli_spec("email", EMAIL_OUTPUT, tmp_path),
    }


class _RecordingProvider:
    """A FakeProvider that keeps every request it answered, for the Groq hop's user turn."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: list[Any] = []

    def complete(self, request: Any) -> Any:
        self.requests.append(request)
        return self.inner.complete(request)


def _chain_session(
    conn: sqlite3.Connection,
    tmp_path: Path,
    scope: str,
    *,
    max_iterations: int | None = None,
) -> tuple[Session, ScopedFakeDecider, _RecordingProvider]:
    from dataclasses import replace

    from harnessed import config
    from harnessed.providers.fake import FakeProvider

    settings = config.load(provider="fake")
    if max_iterations is not None:
        settings = replace(settings, max_iterations=max_iterations)
    decider = ScopedFakeDecider(fake=FakeDecider(settings), scope=scope)
    provider = _RecordingProvider(FakeProvider(settings))
    session = Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=default_registry(settings.home),
        provider=provider,  # type: ignore[arg-type]
        decider=decider,
        agents=_chain_agents(tmp_path),
    )
    return session, decider, provider


def _hops(conn: sqlite3.Connection, run_id: str) -> list[tuple[int, str | None, str, str | None]]:
    rows = conn.execute(
        "SELECT hop, route, outcome, task FROM decisions WHERE run_id = ? ORDER BY idx", (run_id,)
    ).fetchall()
    return [(int(hop), route_, outcome, task) for hop, route_, outcome, task in rows]


def test_a_hops_state_carries_the_previous_step_digest(conn: sqlite3.Connection) -> None:
    built = state.build_state(conn, "ping", previous="x" * 2500 + "THE END")
    assert "\nPrevious step:\n" in built.text
    section = built.text.split("Previous step:\n", 1)[1]
    assert section == ("x" * 2500 + "THE END")[-state.PREVIOUS_STEP_CAP :]
    assert state.PREVIOUS_STEP_CAP == 2000
    assert "Previous step:" not in state.build_state(conn, "ping").text, "a first decision has none"


def test_the_previous_step_is_scrubbed(conn: sqlite3.Connection) -> None:
    built = state.build_state(conn, "ping", previous=f"the agent printed {SECRET}")
    assert SECRET not in built.text
    assert redact.token("anthropic_key") in built.text


def test_an_oversized_state_cuts_the_previous_step_before_the_task(
    conn: sqlite3.Connection,
) -> None:
    task = "keep this task whole"
    built = state.build_state(conn, task, previous="word " * 400, budget=150)
    assert built.truncated
    assert f"Task: {task}" in built.text, "the previous step is cut before the task is"
    assert count_tokens(built.text) <= 150


def test_done_is_offered_last_on_a_hop_only() -> None:
    first = questions.build_route_question()
    hop = questions.build_route_question(hop=True)
    assert "done" not in first.options
    assert hop.options == (*first.options, "done")


def test_a_hop_excludes_the_entry_that_just_answered() -> None:
    agents = {
        "wireframe": _spec("wireframe", use_for="layouts", not_for="email"),
        "email": _spec("email", use_for="copy", not_for="layouts"),
    }
    options = questions.route_options(agents, hop=True, exclude=("wireframe",))
    assert options == ("groq_chat", "groq_memory", "email", "needs_human", "done")
    descriptions = questions.route_descriptions(agents, hop=True, exclude=("wireframe",))
    assert set(descriptions) == set(options)
    question = questions.build_route_question(agents, hop=True, exclude=("wireframe",))
    assert question.options == options
    assert question.criteria is not None and set(question.criteria) == set(options)
    assert "wireframe" in questions.route_options(agents, hop=False, exclude=("wireframe",))
    assert "wireframe" in questions.route_descriptions(agents, hop=False, exclude=("wireframe",))


def test_a_one_entry_registry_hop_offers_the_groq_pair_needs_human_and_done() -> None:
    agents = {"wireframe": _spec("wireframe", use_for="layouts", not_for="email")}
    options = questions.route_options(agents, hop=True, exclude=("wireframe",))
    assert options == ("groq_chat", "groq_memory", "needs_human", "done")


def test_the_previous_step_label_names_its_author(conn: sqlite3.Connection) -> None:
    built = state.build_state(conn, "ping", previous="OUT", previous_by="wireframe")
    assert "\nPrevious step: (by wireframe)\nOUT" in built.text
    assert "previous step:" in built.text.casefold(), "the fixtures' matcher still fires"


def test_decide_withholds_the_excluded_entry_and_refuses_it_if_answered(
    conn: sqlite3.Connection,
) -> None:
    agents = {
        "wireframe": _spec("wireframe", use_for="layouts", not_for="email"),
        "email": _spec("email", use_for="copy", not_for="layouts"),
    }
    asked: list[tuple[Question, ...]] = []

    class _Recording:
        def decide(self, state_text: str, qs: tuple[Question, ...]) -> Decisions:
            asked.append(qs)
            raise DecisionRefused("an option nobody offered")

    verdict = route.decide(
        _Recording(),  # type: ignore[arg-type]
        "Task: ping\n",
        conn=conn,
        announced=_announced(),
        task="ping",
        model=MODEL,
        agents=agents,
        hop=1,
        exclude=("wireframe",),
    )
    assert asked[0][0].options == ("groq_chat", "groq_memory", "email", "needs_human", "done")
    assert (verdict.outcome, verdict.fallback_cause) == ("fallback", "refused")


def test_a_chain_hop_never_re_offers_the_entry_that_just_answered(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd

    session, decider, _provider = _chain_session(conn, tmp_path, "chain-wireframe-email")
    memory_run = run_cmd.solve_with_memory(session, CHAIN_TASK)
    assert memory_run.result is not None
    rows = conn.execute(
        "SELECT d.hop, o.option FROM decision_options o "
        "JOIN decisions d ON d.run_id = o.run_id AND d.idx = o.idx "
        "WHERE d.run_id = ? AND o.question = 'route'",
        (memory_run.run_id,),
    ).fetchall()
    offered: dict[int, set[str]] = {}
    for hop, option in rows:
        offered.setdefault(int(hop), set()).add(option)
    assert "wireframe" in offered[0]
    assert "wireframe" not in offered[1] and "email" in offered[1]
    assert "email" not in offered[2] and "wireframe" in offered[2]
    assert "Previous step: (by wireframe)" in decider.states[1]
    assert "Previous step: (by email)" in decider.states[2]


def test_a_done_answer_on_a_hop_is_routed_with_no_handler_and_no_floor(
    conn: sqlite3.Connection,
) -> None:
    published: list[tuple[str, Mapping[str, object]]] = []
    verdict = route.decide(
        _StubDecider(decisions=_decisions("done", "0.30")),
        "Task: ping\n",
        conn=conn,
        announced=_announced(),
        task="ping",
        model=MODEL,
        hop=1,
        publish=lambda name, payload: published.append((name, payload)),
    )
    assert (verdict.outcome, verdict.route, verdict.handler, verdict.floor) == (
        "routed",
        "done",
        None,
        None,
    )
    hop, task, handler = conn.execute("SELECT hop, task, handler FROM decisions").fetchone()
    assert (hop, task, handler) == (1, None, None), "a hop's row carries its hop and no task"
    ((name, payload),) = published
    assert name == "loop.route.decided"
    assert payload["hop"] == "1"


def test_wireframe_then_email_is_three_decisions_and_two_delegations(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd
    from harnessed.loop.prompt import CONTEXT_CLOSE, CONTEXT_OPEN

    session, decider, provider = _chain_session(conn, tmp_path, "chain-wireframe-email")
    memory_run = run_cmd.solve_with_memory(session, CHAIN_TASK)

    assert memory_run.result is not None
    synthesis = "The wireframe and the launch email are ready: sign up from the new form."
    assert memory_run.result.reply == synthesis, "the reply is the Groq synthesis pass"
    assert decider.calls == 3
    (request,) = provider.requests
    user_turn = next(m.content for m in request.messages if m.role == "user")
    assert user_turn == f"{CONTEXT_OPEN}\n{EMAIL_OUTPUT}\n{CONTEXT_CLOSE}\n\n{CHAIN_TASK}"
    assert (
        request.system == SYNTHESIS_INSTRUCTION
    ), "rule 34: the synthesis pass carries 0 procedures"

    hops = _hops(conn, memory_run.run_id)
    assert hops == [
        (0, "wireframe", "routed", CHAIN_TASK),
        (1, "email", "routed", None),
        (2, "done", "routed", None),
    ]
    delegations = conn.execute("SELECT agent FROM delegations ORDER BY rowid").fetchall()
    assert [agent for (agent,) in delegations] == ["wireframe", "email"]

    assert "Previous step:" not in decider.states[0]
    assert WIREFRAME_OUTPUT in decider.states[1].split("Previous step:", 1)[1]
    assert EMAIL_OUTPUT in decider.states[2].split("Previous step:", 1)[1]
    (email_task,) = conn.execute("SELECT task FROM delegations WHERE agent = 'email'").fetchone()
    assert WIREFRAME_OUTPUT in email_task, "the next agent is handed the previous step"

    events = _events(conn, memory_run.run_id)
    names = [name for _, name, _ in events]
    decided = [payload for _, name, payload in events if name == "loop.route.decided"]
    assert [(p["hop"], p["route"]) for p in decided] == [
        ("0", "wireframe"),
        ("1", "email"),
        ("2", "done"),
    ], "every hop has its event"
    assert names.count("acp.delegation.finished") == 2
    last_finished = max(i for i, n in enumerate(names) if n == "acp.delegation.finished")
    assert names.index("loop.iteration.started", last_finished) > last_finished
    assert names[-1] == "run.finished"
    assert not [
        p for _, n, p in events if n == "loop.guardrail.tripped" and p["scope"] == "chain"
    ], "no chain trip"
    finished = events[-1][2]
    assert finished["iterations"] == 3, "hops (2) + loop turns (1)"
    assert finished["guardrail"] == "no_more_tool_calls"
    assert finished["stop_reason"] == "end_turn"

    rows = conn.execute(
        "SELECT role, content FROM run_messages WHERE run_id = ? ORDER BY idx", (memory_run.run_id,)
    ).fetchall()
    assert rows == [("user", user_turn), ("assistant", synthesis)]

    capture_176(
        "chain-wireframe-email.txt",
        f"ticket 176 - {CHAIN_TASK!r} on {memory_run.run_id}\n"
        + "decisions:\n"
        + "".join(f"  hop={h} route={r} outcome={o}\n" for h, r, o, _ in hops)
        + "delegations: "
        + ", ".join(agent for (agent,) in delegations)
        + f"\nreply: {memory_run.result.reply}\nevents:\n"
        + "".join(f"  {name}\n" for name in names),
    )


def test_a_groq_route_at_hop_0_keeps_the_base_instruction(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd
    from harnessed.loop.prompt import BASE_INSTRUCTION

    session, provider = _agent_session(
        conn, {"probe": _acp_spec()}, _SequenceDecider(_decisions("groq_chat", "0.90"))
    )
    task = "sketch a wireframe, then describe it in one line"
    memory_run = run_cmd.solve_with_memory(session, task)

    assert memory_run.result is not None
    (request,) = provider.requests
    assert request.system == build_system_prompt(
        BASE_INSTRUCTION, tuple(p.text for p in procedural.active(conn))
    )


def test_a_groq_hop_gets_the_agents_result_as_retrieved_context(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd
    from harnessed.loop.prompt import CONTEXT_CLOSE, CONTEXT_OPEN

    task = "sketch a wireframe, then describe it in one line"
    session, decider, provider = _chain_session(conn, tmp_path, "chain-groq-hop")
    memory_run = run_cmd.solve_with_memory(session, task)

    assert decider.calls == 2
    assert memory_run.result is not None
    assert memory_run.result.iterations == 2, "one hop plus the Groq turn"
    assert memory_run.result.reply == "A signup page: a header, a hero and one signup form."
    assert [route_ for _, route_, _, _ in _hops(conn, memory_run.run_id)] == [
        "wireframe",
        "groq_chat",
    ]

    (request,) = provider.requests
    user_turn = next(m.content for m in request.messages if m.role == "user")
    assert user_turn == f"{CONTEXT_OPEN}\n{WIREFRAME_OUTPUT}\n{CONTEXT_CLOSE}\n\n{task}"
    assert (
        request.system == SYNTHESIS_INSTRUCTION
    ), "rule 34: the synthesis pass carries 0 procedures"

    names = [name for _, name, _ in _events(conn, memory_run.run_id)]
    assert "memory.gate.decided" not in names, "the agent's result is the context, not a retrieval"
    assert names[-1] == "run.finished"

    capture_176(
        "groq-hop-request.txt",
        f"ticket 176 - Groq hop on {memory_run.run_id}\nuser turn sent to Groq:\n{user_turn}\n"
        f"reply: {memory_run.result.reply}\n",
    )


def test_a_hop_below_its_floor_ends_the_chain_and_groq_writes_the_reply_from_it(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd
    from harnessed.loop.prompt import CONTEXT_CLOSE, CONTEXT_OPEN

    session, decider, provider = _chain_session(conn, tmp_path, "chain-below-floor")
    memory_run = run_cmd.solve_with_memory(session, CHAIN_TASK)

    assert memory_run.verdict is None, "the message ran; only the chain stopped"
    assert memory_run.result is not None
    assert memory_run.result.reply.startswith("The wireframe and the launch email")
    (request,) = provider.requests
    user_turn = next(m.content for m in request.messages if m.role == "user")
    assert user_turn == f"{CONTEXT_OPEN}\n{WIREFRAME_OUTPUT}\n{CONTEXT_CLOSE}\n\n{CHAIN_TASK}"
    assert (
        request.system == SYNTHESIS_INSTRUCTION
    ), "rule 34: the synthesis pass carries 0 procedures"
    hops = _hops(conn, memory_run.run_id)
    assert [(h, r, o) for h, r, o, _ in hops] == [
        (0, "wireframe", "routed"),
        (1, "email", "uncertain"),
    ]
    (count,) = conn.execute("SELECT COUNT(*) FROM delegations").fetchone()
    assert count == 1
    names = [name for _, name, _ in _events(conn, memory_run.run_id)]
    assert names[-1] == "run.finished"
    capture_176(
        "below-floor-hop.txt",
        f"ticket 198 - below-floor hop on {memory_run.run_id}\n"
        + "".join(f"  hop={h} route={r} outcome={o}\n" for h, r, o, _ in hops)
        + f"synthesis request user turn:\n{user_turn}\nreply: {memory_run.result.reply}\n",
    )


def test_a_hop_fallback_publishes_error_raised_and_groq_writes_the_reply(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed import config
    from harnessed.cli import run_cmd
    from harnessed.errors import DecisionTimeout
    from harnessed.providers.fake import FakeProvider

    class _TimesOutOnHopOne:
        calls = 0

        def decide(self, state_text: str, asked: tuple[Question, ...]) -> Decisions:
            self.calls += 1
            if self.calls == 1:
                return _decisions("wireframe", "0.90")
            raise DecisionTimeout("the router timed out")

    settings = config.load(provider="fake")
    provider = _RecordingProvider(FakeProvider(settings))
    session = Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=default_registry(settings.home),
        provider=provider,  # type: ignore[arg-type]
        decider=_TimesOutOnHopOne(),
        agents=_chain_agents(tmp_path),
    )
    memory_run = run_cmd.solve_with_memory(session, CHAIN_TASK)

    assert memory_run.result is not None
    assert memory_run.result.reply.startswith("The wireframe and the launch email")
    assert len(provider.requests) == 1
    assert provider.requests[0].system == SYNTHESIS_INSTRUCTION
    names = [name for _, name, _ in _events(conn, memory_run.run_id)]
    assert "error.raised" in names
    assert names.index("error.raised") < names.index("loop.iteration.started")


def test_a_chain_at_the_ceiling_stops_and_says_iteration_limit(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd

    session, decider, provider = _chain_session(conn, tmp_path, "chain-ceiling", max_iterations=3)
    memory_run = run_cmd.solve_with_memory(session, CHAIN_TASK)

    assert memory_run.result is not None
    delegations = [
        agent
        for (agent,) in conn.execute("SELECT agent FROM delegations ORDER BY rowid").fetchall()
    ]
    assert len(delegations) == 2, "the chain leaves one iteration for the synthesis"
    assert memory_run.result.reply.startswith("The wireframe and the launch email")
    assert memory_run.result.guardrail == "iteration_limit"
    assert memory_run.result.iterations == 3
    assert decider.calls == 2, "no decision is asked once the ceiling is reached"
    assert len(provider.requests) == 1
    assert provider.requests[0].system == SYNTHESIS_INSTRUCTION

    events = _events(conn, memory_run.run_id)
    tripped = [
        payload
        for _, name, payload in events
        if name == "loop.guardrail.tripped" and payload["scope"] == "chain"
    ]
    assert tripped == [
        {"guardrail": "iteration_limit", "iterations": 2, "stop_reason": None, "scope": "chain"}
    ]
    names = [name for _, name, _ in events]
    assert names.index("loop.guardrail.tripped") < names.index("loop.iteration.started")
    _, last_name, finished = events[-1]
    assert last_name == "run.finished"
    assert finished["guardrail"] == "iteration_limit"
    assert finished["iterations"] == 3

    capture_176(
        "ceiling-stop.txt",
        f"ticket 176 - chain ceiling on {memory_run.run_id} (max_iterations=3)\n"
        f"delegations: {', '.join(delegations)}\n"
        f"loop.guardrail.tripped: {tripped[0]}\nrun.finished: {finished}\n"
        f"reply: {memory_run.result.reply}\n",
    )


# ---- ticket 196: the fixed routes use the spike's measured wording (decision 58, FR-102) --------

EVIDENCE_196 = REPO_ROOT / "evidence" / "196"


def capture_196(name: str, text: str) -> Path:
    EVIDENCE_196.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_196 / name
    path.write_text(text, encoding="utf-8")
    return path


def test_the_fixed_descriptions_are_the_spikes_measured_wording() -> None:
    """A live probe on 2026-09-25 with this wording sent `ping` and the notes task to
    `groq_chat` at 0.92 and 0.97; the prior text was never what the spike measured (ticket 196)."""
    assert questions._FIXED_DESCRIPTIONS["groq_chat"] == (
        "A direct conversational answer from general knowledge: greetings, definitions, "
        "explanations, advice, or writing a short text or file. No web, no live facts, no "
        "repository."
    )
    assert questions._FIXED_DESCRIPTIONS["groq_memory"] == (
        "Answering or storing needs the operator's own memory: who they are, what they built, "
        "their past decisions, earlier sessions, or facts they told the harness before."
    )
    assert questions._FIXED_DESCRIPTIONS["needs_human"] == (
        "Ambiguous, destructive or unsafe requests the operator must confirm first."
    )
    # `done` (ticket 198): Groq writes the reply from the previous step's output.
    assert questions._FIXED_DESCRIPTIONS[questions.DONE] == (
        "the previous step already finished the task; Groq writes the reply from its output"
    )
    capture_196(
        "fixed-descriptions.txt",
        "\n".join(
            f"{option}: {questions._FIXED_DESCRIPTIONS[option]}"
            for option in questions.FIXED_ROUTE_OPTIONS
        ),
    )


def test_a_pinned_verdict_is_certain_routed_and_never_asks_for_memory() -> None:
    groq = route.Verdict.pinned("groq_chat")
    agent = route.Verdict.pinned("email")
    assert (groq.outcome, groq.handler, groq.route) == ("routed", "groq", "groq_chat")
    assert (agent.outcome, agent.handler, agent.route) == ("routed", "agent", "email")
    assert groq.decisions is None and groq.needs_memory == 0


# ---- ticket 240 (FR-141, decisions 58, 59, 77): `done` on a task's continuation ----


def test_a_continuations_hop_0_question_offers_done_last_and_an_ordinary_one_does_not(
    conn: sqlite3.Connection,
) -> None:
    class _Recording:
        def __init__(self) -> None:
            self.options: list[tuple[str, ...]] = []

        def decide(self, state_text: str, asked: tuple[Question, ...]) -> Decisions:
            self.options.append(next(q for q in asked if q.name == "route").options)
            return _decisions("groq_chat", "0.90")

    recorder = _Recording()
    for continuation in (False, True):
        route.decide(
            recorder,
            "Task: x\n",
            conn=conn,
            announced=_announced(),
            task="x",
            model=MODEL,
            continuation=continuation,
        )
    assert recorder.options == [
        ("groq_chat", "groq_memory", "needs_human"),
        ("groq_chat", "groq_memory", "needs_human", "done"),
    ]


@pytest.mark.parametrize(
    ("confidence", "outcome", "stop_reason"),
    [("0.80", "routed", None), ("0.95", "routed", None), ("0.79", "uncertain", "below_floor")],
)
def test_done_on_a_continuation_is_acted_on_at_the_task_done_floor(
    conn: sqlite3.Connection, confidence: str, outcome: str, stop_reason: str | None
) -> None:
    assert Decimal("0.80") == questions.TASK_DONE_FLOOR
    verdict = route.decide(
        _StubDecider(decisions=_decisions("done", confidence)),
        "Task: x\n",
        conn=conn,
        announced=_announced(),
        task="x",
        model=MODEL,
        continuation=True,
    )
    assert (verdict.outcome, verdict.stop_reason, verdict.route) == (outcome, stop_reason, "done")
    assert verdict.floor == Decimal("0.80")


def test_a_chain_hop_still_ends_on_done_at_any_confidence(conn: sqlite3.Connection) -> None:
    verdict = route.decide(
        _StubDecider(decisions=_decisions("done", "0.30")),
        "Task: x\n",
        conn=conn,
        announced=_announced(),
        task="x",
        model=MODEL,
        hop=1,
    )
    assert (verdict.outcome, verdict.floor) == ("routed", None)


def test_the_state_carries_the_task_section_only_on_a_task_message(
    conn: sqlite3.Connection,
) -> None:
    from harnessed.router import state as state_module

    plain = state_module.build_state(conn, "ping")
    held = state_module.build_state(conn, "ping", the_task="tidy\nUsed so far: 1 of 20 steps.")
    assert "The task:" not in plain.text
    assert "The task:\ntidy\nUsed so far: 1 of 20 steps.\n" in held.text
    assert (
        held.text.index("Task: ping")
        < held.text.index("The task:")
        < held.text.index("Operator and memory:")
    )


def test_a_continuations_last_reply_is_cut_from_its_head_to_fit_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 264: a reply the digest cut is quoted as ``Last reply:``, newest text kept."""
    from harnessed.memory import db
    from harnessed.router import state as state_module

    monkeypatch.setenv("HARNESSED_HOME", str(tmp_path))
    conn = db.connect()
    try:
        reply = "head " * 400 + "TAIL"
        built = state_module.build_state(
            conn, "t", (("assistant", reply),), the_task="x", last_reply=reply
        )
        assert built.text.rstrip().endswith("TAIL") and "Last reply:" in built.text
        small = state_module.build_state(
            conn, "t", (("assistant", reply),), the_task="x", last_reply=reply, budget=250
        )
        assert small.truncated and small.text.split("Last reply:\n")[1].endswith("TAIL")
        assert len(small.text.split("Last reply:\n")[1]) < len(reply)
    finally:
        conn.close()


def test_the_task_cut_is_measured_against_the_reply_that_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 267 (941b50c): the task keeps the longest prefix that fits beside the kept reply."""
    from harnessed.memory import db
    from harnessed.memory.budget import count_tokens
    from harnessed.router import state as state_module

    monkeypatch.setenv("HARNESSED_HOME", str(tmp_path))
    conn = db.connect()
    try:
        task = "".join(f"w{n} " for n in range(400))
        reply = "head " * 400 + "TAIL"
        budget = 300
        built = state_module.build_state(
            conn, task, (("assistant", reply),), last_reply=reply, budget=budget
        )
        assert built.truncated and count_tokens(built.text) <= budget
        cut = built.text.split("\n", 1)[0].removeprefix("Task: ")
        assert 0 < len(cut) < len(task) and task.startswith(cut)
        longer = built.text.replace(f"Task: {cut}\n", f"Task: {task[: len(cut) + 1]}\n", 1)
        assert count_tokens(longer) > budget
    finally:
        conn.close()


# ---- ticket 275 (FR-141, decisions 58, 77): what `done` means on a continuation ----


def _route_question_asked(
    conn: sqlite3.Connection, *, hop: int = 0, continuation: bool
) -> Question:
    class _Recording:
        def __init__(self) -> None:
            self.asked: list[Question] = []
            self.calls = 0

        def decide(self, state_text: str, asked: tuple[Question, ...]) -> Decisions:
            self.calls += 1
            self.asked.append(next(q for q in asked if q.name == "route"))
            return _decisions("groq_chat", "0.90")

    recorder = _Recording()
    route.decide(
        recorder,
        "Task: x\n",
        conn=conn,
        announced=_announced(),
        task="x",
        model=MODEL,
        hop=hop,
        continuation=continuation,
    )
    assert recorder.calls == 1
    return recorder.asked[0]


def test_a_continuations_done_is_judged_against_the_task_and_its_last_reply(
    conn: sqlite3.Connection,
) -> None:
    asked = _route_question_asked(conn, continuation=True)
    assert asked.criteria is not None
    assert asked.criteria[questions.DONE] == questions.CONTINUATION_DONE_DESCRIPTION
    text = questions.CONTINUATION_DONE_DESCRIPTION
    assert "The task:" in text
    assert "Last reply:" in text
    assert "previous step" not in text.lower()


def test_a_chain_hops_done_description_is_unchanged(conn: sqlite3.Connection) -> None:
    asked = _route_question_asked(conn, hop=1, continuation=False)
    assert asked.criteria is not None
    assert asked.criteria[questions.DONE] == (
        "the previous step already finished the task; Groq writes the reply from its output"
    )


def test_a_continuations_options_floors_and_call_count_are_unchanged(
    conn: sqlite3.Connection,
) -> None:
    asked = _route_question_asked(conn, continuation=True)
    assert asked.options == ("groq_chat", "groq_memory", "needs_human", "done")
    assert Decimal("0.80") == questions.TASK_DONE_FLOOR
    assert questions.FLOORS == {"groq_chat": Decimal("0.50"), "groq_memory": Decimal("0.50")}


# ---- ticket 281 (FR-141, decisions 59, 77): a continuation's done bar reads done's own score ----


def _done_decisions(done: str, confidence: str, route_value: str = "done") -> Decisions:
    base = _decisions(route_value, confidence)
    route_answer = Choice(
        value=route_value,
        confidence=Decimal(confidence),
        probabilities={"done": Decimal(done), "groq_chat": Decimal("0.05")},
    )
    return replace(base, answers={**base.answers, "route": route_answer})


@pytest.mark.parametrize(
    ("done", "confidence", "outcome", "stop_reason"),
    [
        ("0.82", "0.78", "routed", None),
        ("0.78", "0.82", "uncertain", "below_floor"),
        ("0.80", "0.60", "routed", None),
    ],
)
def test_a_continuations_done_bar_reads_dones_own_probability(
    conn: sqlite3.Connection,
    done: str,
    confidence: str,
    outcome: str,
    stop_reason: str | None,
) -> None:
    verdict = route.decide(
        _StubDecider(decisions=_done_decisions(done, confidence)),
        "Task: x\n",
        conn=conn,
        announced=_announced(),
        task="x",
        model=MODEL,
        continuation=True,
    )
    assert (verdict.outcome, verdict.stop_reason) == (outcome, stop_reason)
    assert verdict.floor == Decimal("0.80")
    # The figure compared with the floor is the one stored beside it (decision row and card).
    assert verdict.confidence == Decimal(done)
    row = conn.execute("SELECT confidence, confidence_percent FROM decisions").fetchone()
    assert Decimal(row[0]) == Decimal(done)
    assert row[1] == f"{int(Decimal(done) * 100)}%"


def test_a_chain_hops_done_and_an_agent_route_still_compare_confidence(
    conn: sqlite3.Connection,
) -> None:
    hop = route.decide(
        _StubDecider(decisions=_done_decisions("0.82", "0.30")),
        "Task: x\n",
        conn=conn,
        announced=_announced(),
        task="x",
        model=MODEL,
        hop=1,
    )
    assert hop.confidence == Decimal("0.30") and hop.floor is None
    groq = route.decide(
        _StubDecider(decisions=_done_decisions("0.82", "0.40", route_value="groq_chat")),
        "Task: x\n",
        conn=conn,
        announced=_announced(),
        task="x",
        model=MODEL,
        continuation=True,
    )
    assert (groq.outcome, groq.confidence) == ("uncertain", Decimal("0.40"))


# ---- ticket 291 (FR-171, decision 101): the compared figure, served once by the store ----


def _continuation_decision(
    conn: sqlite3.Connection,
    *,
    session: str,
    stored: str,
    done_probability: str,
    outcome: str = "uncertain",
    hop: int = 0,
    continuation: bool = True,
    route_value: str = "done",
) -> None:
    """One recorded decision whose ``done`` option is priced ``done_probability`` and whose stored
    ``confidence`` is ``stored``, on a task continuation or an ordinary message."""
    bus = EventBus(run_id=session, conn=conn)
    seq = bus.publish("run.started", {"provider": "fake"}).seq
    if continuation:
        conn.execute(
            "INSERT INTO task_messages (run_id, message_seq, task_id, kind) "
            "VALUES (?, ?, 'task_x', 'continuation')",
            (session, seq),
        )
    answered = Decisions(
        answers={
            "route": Choice(
                value=route_value,
                confidence=Decimal(stored),
                probabilities={"done": Decimal(done_probability)},
            ),
        },
        id="gen-291",
        model=MODEL,
        usage={},
        cost="0.0000100",
    )
    decisions_store.record_decision(
        conn,
        run_id=session,
        message_seq=seq,
        task="finish it",
        model=MODEL,
        hop=hop,
        latency_seconds="0.1",
        outcome=outcome,
        route=route_value,
        handler=None,
        confidence=Decimal(stored),
        floor=Decimal("0.8"),
        needs_memory=None,
        memory_kind=None,
        stop_reason="below_floor" if outcome == "uncertain" else None,
        fallback_cause=None,
        state_truncated=False,
        answered=answered,
    )


def _compared(conn: sqlite3.Connection, session: str) -> list[dict[str, object]]:
    payload = decisions_store.json_payload(decisions_store.for_session(conn, session))
    return [
        {"compared": row["compared"], "compared_reason": row["compared_reason"]}
        for row in payload["decisions"]  # type: ignore[attr-defined]
    ]


def test_an_uncertain_continuation_done_compares_the_done_probability(
    conn: sqlite3.Connection,
) -> None:
    # post-281: the stored confidence is done's own probability
    _continuation_decision(conn, session="run_a", stored="0.78", done_probability="0.78")
    assert _compared(conn, "run_a") == [{"compared": "done_probability", "compared_reason": None}]


def test_a_pre_281_uncertain_continuation_done_compares_the_confidence(
    conn: sqlite3.Connection,
) -> None:
    # pre-281: 78% confidence against 80%, while done was priced 82%
    _continuation_decision(conn, session="run_b", stored="0.78", done_probability="0.82")
    assert _compared(conn, "run_b") == [{"compared": "confidence", "compared_reason": None}]


def test_an_uncertain_stop_off_a_continuation_hop_zero_compares_the_confidence(
    conn: sqlite3.Connection,
) -> None:
    _continuation_decision(
        conn, session="run_c", stored="0.78", done_probability="0.78", continuation=False
    )
    _continuation_decision(conn, session="run_c", stored="0.78", done_probability="0.78", hop=1)
    _continuation_decision(
        conn, session="run_c", stored="0.78", done_probability="0.78", route_value="groq_chat"
    )
    assert [row["compared"] for row in _compared(conn, "run_c")] == [
        "confidence",
        "confidence",
        "confidence",
    ]


def test_a_decision_the_router_acted_on_serves_no_compared_figure(
    conn: sqlite3.Connection,
) -> None:
    _continuation_decision(
        conn, session="run_d", stored="0.9", done_probability="0.9", outcome="routed"
    )
    assert _compared(conn, "run_d") == [{"compared": None, "compared_reason": "the router acted"}]


def test_the_compared_expression_is_one_constant_the_other_stores_copy() -> None:
    assert "done_probability" in decisions_store.COMPARED_SQL
    assert decisions_store.COMPARED_WORDS == ("done_probability", "confidence")


# ---- routing off (ticket 316, FR-175, decision 105) -------------------------------------------


def test_probe_state_says_routing_on_with_no_reason_under_the_offline_provider(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    state_row = decisions_store.probe_state(conn, tmp_path / "agents.toml")
    assert (state_row.routing, state_row.routing_reason) == ("on", None)


def test_probe_state_and_router_status_say_routing_off_with_only_a_groq_key(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "placeholder-not-a-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    state_row = decisions_store.probe_state(conn, tmp_path / "agents.toml")
    assert state_row.routing == "off"
    assert state_row.routing_reason == "routing is off: set OPENROUTER_API_KEY to turn it on"

    as_json = CliRunner(mix_stderr=False).invoke(cli_app, ["router", "status", "--json"])
    assert as_json.exit_code == 0, as_json.stderr
    assert json.loads(as_json.stdout)["routing"] == "off"
    plain = CliRunner(mix_stderr=False).invoke(cli_app, ["router", "status"])
    assert "routing is off: set OPENROUTER_API_KEY to turn it on" in plain.stdout


def test_routing_words_are_on_and_off() -> None:
    assert decisions_store.ROUTING_WORDS == ("on", "off")
