"""FR-12: one cheap gate decides whether retrieval happens, and every failure fails open.

Four claims carry this ticket, and each is tested against the thing that could break it.

* **A "no" costs nothing.** When the gate declines, ``assemble`` is never called - measured with
  a counting spy wrapped around the real function - and the context handed to the model is the
  empty string rather than a shorter one.
* **Nothing in the gate can end a run.** A provider that raises and a provider that answers
  something unparseable are the same event: retrieval proceeds and ``retrieval.gate.failed_open``
  is published. The raising provider is *injected*, never monkeypatched, so what is proven is a
  property of the seam and not of the patch.
* **The gate is consulted at most once per run**, measured across a full composed run with a
  counting provider rather than asserted about the source.
* **The gate is an ordinary request.** It carries ``metadata["scope"] = "_gate"`` and FakeProvider
  serves it out of the committed corpus with no special case (architecture decision 7).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessed import config, shipped
from harnessed.events.bus import EventBus
from harnessed.events.names import NAMES
from harnessed.loop.engine import run_loop
from harnessed.loop.prompt import build_system_prompt
from harnessed.memory import db, episodic, gate, procedural, retrieval, semantic
from harnessed.providers.fake import FakeProvider
from harnessed.providers.types import ChatRequest, ChatResponse, Message
from harnessed.tools import default_registry
from support.raising_provider import RaisingProvider
from support.spies import CountingAssembler, CountingProvider, RecordingObserver

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_FIXTURES = shipped.fake_wire() / "_gate"
EVIDENCE_169 = REPO_ROOT / "evidence" / "169"


def capture_169(name: str, text: str) -> Path:
    EVIDENCE_169.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_169 / name
    path.write_text(text, encoding="utf-8")
    return path


# The task every gate in this file is asked about, and the words the seeded store is built from
# so that a retrieval which does proceed actually finds something.
TASK = "deploy the harness offline"

# The model name the caller resolves from ``Settings.small_model``. The gate never reads it.
SMALL_MODEL = "fake-small"

# Answers no reader could call a boolean. Each must be treated exactly like an exception.
UNPARSEABLE = ("", "   ", "maybe", "it depends on the task", "42", "ye s", "affirmative-ish")


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database file under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


def seed(connection: sqlite3.Connection, run_id: str = "run_gated") -> None:
    """Enough in all three stores that a retrieval which proceeds returns a non-empty context."""
    procedure = procedural.put(
        connection, "To deploy the harness offline, replay the corpus.", source="test"
    )
    procedural.activate(connection, procedure.proc_id, cand_id=None)
    semantic.put(connection, "The harness can deploy offline because it replays fixtures.", "test")
    episodic.put(connection, run_id, "loop", "The run asked to deploy the harness offline.")


def answering(*answers: str) -> CountingProvider:
    """A gate provider that says ``answers`` and counts how often it was asked."""
    return CountingProvider(
        script=[
            ChatResponse(response_id=f"resp_gate_{index}", model=SMALL_MODEL, content=answer)
            for index, answer in enumerate(answers)
        ]
    )


# ---- the two event names ---------------------------------------------------------------------


def test_the_gate_publishes_only_frozen_names() -> None:
    assert {gate.DECIDED, gate.FAILED_OPEN} <= NAMES


# ---- a decision either way is published --------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "retrieve"),
    [("yes", True), ("Yes.", True), ("true", True), ("no", False), ("NO", False), ("false", False)],
)
def test_a_decision_either_way_is_published(answer: str, retrieve: bool) -> None:
    observer = RecordingObserver()
    bus = EventBus(run_id="run_gate_decided")
    bus.subscribe(observer)

    decision = gate.decide(answering(answer), TASK, small_model=SMALL_MODEL, publish=bus.publish)

    assert decision.retrieve is retrieve
    assert decision.failed_open is False
    decided = observer.named(gate.DECIDED)
    assert len(decided) == 1
    assert decided[0].payload["retrieve"] is retrieve
    assert observer.named(gate.FAILED_OPEN) == []


# ---- a "no" costs a call and returns nothing ---------------------------------------------------


def test_a_declining_gate_makes_zero_retrieval_calls_and_an_empty_context(
    conn: sqlite3.Connection,
) -> None:
    seed(conn)
    assembler = CountingAssembler(retrieval.assemble)

    decision, assembled = gate.assemble_if_gated(
        answering("no"),
        conn,
        TASK,
        budget=4000,
        small_model=SMALL_MODEL,
        run_id="run_gated",
        assemble=assembler,
    )

    assert decision.retrieve is False
    assert assembler.calls == 0
    assert assembled.text == ""
    assert assembled.tokens == 0
    assert assembled.budget == 4000
    assert assembled.included == ()
    assert assembled.truncated is False


def test_an_admitting_gate_assembles_exactly_once(conn: sqlite3.Connection) -> None:
    seed(conn)
    assembler = CountingAssembler(retrieval.assemble)

    decision, assembled = gate.assemble_if_gated(
        answering("yes"),
        conn,
        TASK,
        budget=4000,
        small_model=SMALL_MODEL,
        run_id="run_gated",
        assemble=assembler,
    )

    assert decision.retrieve is True
    assert assembler.calls == 1
    assert "deploy the harness offline" in assembled.text


# ---- fail open: an exception and an unparseable answer are the same event ----------------------


def test_a_raising_provider_fails_open_and_publishes(conn: sqlite3.Connection) -> None:
    seed(conn)
    observer = RecordingObserver()
    bus = EventBus(run_id="run_gate_raised")
    bus.subscribe(observer)
    provider = RaisingProvider()
    assembler = CountingAssembler(retrieval.assemble)

    decision, assembled = gate.assemble_if_gated(
        provider,
        conn,
        TASK,
        budget=4000,
        small_model=SMALL_MODEL,
        run_id="run_gated",
        assemble=assembler,
        publish=bus.publish,
    )

    assert provider.calls == 1
    assert decision.retrieve is True
    assert decision.failed_open is True
    assert assembler.calls == 1
    assert assembled.text != ""
    failed = observer.named(gate.FAILED_OPEN)
    assert len(failed) == 1
    assert provider.message in str(failed[0].payload["reason"])
    assert observer.names.index(gate.FAILED_OPEN) < observer.names.index(gate.DECIDED)


@pytest.mark.parametrize("answer", UNPARSEABLE)
def test_an_unparseable_answer_is_treated_identically_to_an_exception(answer: str) -> None:
    observer = RecordingObserver()
    bus = EventBus(run_id="run_gate_unparseable")
    bus.subscribe(observer)

    decision = gate.decide(answering(answer), TASK, small_model=SMALL_MODEL, publish=bus.publish)

    raised = gate.decide(RaisingProvider(), TASK, small_model=SMALL_MODEL)

    assert decision.retrieve is raised.retrieve is True
    assert decision.failed_open is raised.failed_open is True
    assert len(observer.named(gate.FAILED_OPEN)) == 1
    assert len(observer.named(gate.DECIDED)) == 1
    assert observer.named(gate.DECIDED)[0].payload["retrieve"] is True


def test_failing_open_needs_no_publisher_at_all() -> None:
    """The default publisher is nobody listening, which is a valid way to run the gate."""
    assert gate.decide(RaisingProvider(), TASK, small_model=SMALL_MODEL).retrieve is True


# ---- the request the gate issues ---------------------------------------------------------------


def test_the_gate_request_carries_the_gate_scope() -> None:
    provider = answering("yes")

    gate.decide(provider, TASK, small_model=SMALL_MODEL)

    request = provider.seen[0]
    assert request.metadata["scope"] == gate.GATE_SCOPE == "_gate"
    assert request.model == SMALL_MODEL
    assert request.messages[-1].content == TASK
    assert request.tools == ()


def test_the_gate_fixtures_are_committed_under_the_gate_scope() -> None:
    assert (GATE_FIXTURES / "_index.yaml").is_file()
    assert (GATE_FIXTURES / "base.json").is_file()


def test_fakeprovider_serves_the_gate_with_no_special_case(clean_env: None) -> None:
    provider = FakeProvider(config.load())

    decision = gate.decide(provider, TASK, small_model=SMALL_MODEL)

    assert gate.GATE_SCOPE in provider.scopes
    assert decision.retrieve is True
    assert decision.failed_open is False


# ---- at most once per run ----------------------------------------------------------------------


def one_run(
    home: Path, conn: sqlite3.Connection, gate_provider: CountingProvider, budget: int = 4000
) -> int:
    """One run, composed the way the composition root will compose it.

    The gate is consulted once, before the loop starts, and the loop never sees it: "once per
    run" is a property of where the call sits, and that place is the caller. ``loop/engine.py``
    is not this ticket's to edit, so the composition lives here.
    """
    _, assembled = gate.assemble_if_gated(
        gate_provider, conn, TASK, budget=budget, small_model=SMALL_MODEL, run_id="run_gated"
    )
    request = ChatRequest(
        model="fake",
        system=build_system_prompt("You are a harness.", [assembled.text]),
        messages=(Message(role="user", content=TASK),),
    )
    loop_provider = CountingProvider(
        script=[ChatResponse(response_id="resp_1", model="fake", content="done")]
    )
    run_loop(loop_provider, default_registry(home), request, limit=6)
    return loop_provider.calls


@pytest.mark.parametrize("answer", ["yes", "no", "maybe"])
def test_the_gate_is_consulted_at_most_once_per_run(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, answer: str
) -> None:
    seed(conn)
    gate_provider = answering(answer)

    loop_calls = one_run(tmp_harnessed_home, conn, gate_provider)

    assert gate_provider.calls == 1
    assert loop_calls >= 1


# ---- ticket 169: RouterMemory replaces the parsed yes or no when the router answered -----------


@pytest.mark.parametrize(
    ("retrieve", "expect_text"),
    [(True, True), (False, False)],
)
def test_a_router_answer_makes_zero_gate_calls_either_way(
    conn: sqlite3.Connection, retrieve: bool, expect_text: bool
) -> None:
    """AC1: whether the router says retrieve or skip, the gate never asks its own provider - the
    provider below would answer ``"yes"`` if it were ever asked, which it must not be."""
    seed(conn)
    provider = answering("yes")
    answered = gate.RouterMemory(
        retrieve=retrieve, answer="0.85" if retrieve else "0.20", kinds=("fact", "episode")
    )

    decision, assembled = gate.assemble_if_gated(
        provider,
        conn,
        TASK,
        budget=4000,
        small_model=SMALL_MODEL,
        run_id="run_gated",
        answered=answered,
    )

    assert provider.calls == 0
    assert decision.retrieve is retrieve
    assert decision.failed_open is False
    assert decision.answer == answered.answer
    assert (assembled.text != "") is expect_text


def test_a_router_answer_narrows_assembly_to_its_own_kinds(conn: sqlite3.Connection) -> None:
    """``RouterMemory.kinds`` reaches ``assemble`` in place of ``assemble_if_gated``'s own
    ``kinds`` keyword, never both."""
    seed(conn)
    answered = gate.RouterMemory(retrieve=True, answer="0.85", kinds=("fact", "episode"))

    _decision, assembled = gate.assemble_if_gated(
        answering("yes"),
        conn,
        TASK,
        budget=4000,
        small_model=SMALL_MODEL,
        run_id="run_gated",
        kinds=("procedure",),  # would find nothing extra; RouterMemory.kinds overrides it
        answered=answered,
    )

    assert "deploy the harness offline" in assembled.text


def test_a_router_answer_with_no_needs_memory_fails_open_and_counts_in_failed_open() -> None:
    """AC3: a router answer missing ``needs_memory`` (``RouterMemory.retrieve is None``) fails
    open - retrieves, and ``failed_open`` is ``True`` - exactly the same rule an unparseable v4
    answer follows, applied to a decision the router never got to make."""
    observer = RecordingObserver()
    bus = EventBus(run_id="run_gate_router_missing")
    bus.subscribe(observer)
    provider = answering("yes")
    answered = gate.RouterMemory(retrieve=None, answer="", kinds=("fact", "episode"))

    decision = gate.decide(
        provider, TASK, small_model=SMALL_MODEL, publish=bus.publish, answered=answered
    )

    assert provider.calls == 0
    assert decision.retrieve is True
    assert decision.failed_open is True
    decided = observer.named(gate.DECIDED)
    assert len(decided) == 1
    assert decided[0].payload["failed_open"] is True
    failed = observer.named(gate.FAILED_OPEN)
    assert len(failed) == 1

    capture_169(
        "fail-open-missing-needs-memory.txt",
        "RouterMemory(retrieve=None, answer='', kinds=('fact', 'episode')): 0 gate provider "
        f"calls, decision={decision}\n"
        f"retrieval.gate.decided payload: {decided[0].payload}\n"
        f"retrieval.gate.failed_open published: {len(failed) == 1}\n",
    )


def test_a_router_answer_publishes_the_same_four_keys_as_the_v4_gate() -> None:
    """AC4: the decision 48 guard applies to a router-answered decision exactly as it does to a
    v4 one - the same four keys, published once."""
    v4_observer = RecordingObserver()
    v4_bus = EventBus(run_id="run_v4_keys")
    v4_bus.subscribe(v4_observer)
    gate.decide(answering("yes"), TASK, small_model=SMALL_MODEL, publish=v4_bus.publish)

    router_observer = RecordingObserver()
    router_bus = EventBus(run_id="run_router_keys")
    router_bus.subscribe(router_observer)
    gate.decide(
        answering("yes"),  # never called
        TASK,
        small_model=SMALL_MODEL,
        publish=router_bus.publish,
        answered=gate.RouterMemory(retrieve=True, answer="0.85", kinds=("fact", "episode")),
    )

    v4_keys = set(v4_observer.named(gate.DECIDED)[0].payload)
    router_keys = set(router_observer.named(gate.DECIDED)[0].payload)
    assert router_keys == v4_keys == {"retrieve", "failed_open", "answer", "reason"}
    assert len(router_observer.named(gate.DECIDED)) == 1, "published once per message"

    capture_169(
        "same-four-keys-key-guard.txt",
        f"v4 retrieval.gate.decided keys:     {sorted(v4_keys)}\n"
        f"router retrieval.gate.decided keys: {sorted(router_keys)}\n"
        "published once per message (decision 48 guard holds for a router-answered decision too)\n",
    )
