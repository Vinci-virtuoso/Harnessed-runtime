"""FR-21: one in-process event bus, a frozen name taxonomy, and observers that cannot break a run.

This is the seam v2's SSE gateway attaches to, which is why it is built in phase 2 rather than
phase 7: the ``event:`` field of that contract is the event name verbatim, so the names are
frozen here and never renamed once shipped.

Four claims live here. The taxonomy is frozen and scheme-conformant, and publishing a name
outside it is an error rather than a silent no-op. The bus - never the publisher - stamps
``run_id``, ``ts`` and a monotonic ``seq``. Every payload passes ``redact.scrub()`` before it
reaches an observer or the ``events`` table. And an observer that raises is isolated, with
``observer.failed`` published rather than the failure swallowed.

The fixture-driven run below is driven by this module rather than by ``loop/engine.py``, which
ticket 011 builds. When the engine lands it must publish the same names in the same order; the
sequence asserted here is what it is being held to.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessed import clock
from harnessed.config import Settings
from harnessed.errors import EventError, HarnessError, UnknownEventError
from harnessed.events import bus as bus_module
from harnessed.events import names as names_module
from harnessed.events.bus import Event, EventBus
from harnessed.memory import db
from harnessed.providers.fake import FakeProvider
from harnessed.providers.types import ChatRequest, Message, ToolResult
from support.spies import RaisingObserver, RecordingObserver, is_subsequence

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
ARCHITECTURE = REPO_ROOT / ".spec" / "architecture.md"

REPLAY_SCOPE = "smoke-005-replay"

# The two-iteration fixture-driven run below: one tool-using turn, then a final answer.
EXPECTED_SEQUENCE = (
    "run.started",
    "loop.iteration.started",
    "provider.request.sent",
    "provider.response.received",
    "tool.call.started",
    "tool.call.finished",
    "loop.iteration.finished",
    "loop.iteration.started",
    "provider.request.sent",
    "provider.response.received",
    "loop.iteration.finished",
    "run.finished",
)

# Assembled from fragments: a literal that looks like a credential is what the repository's own
# write guard rejects, and a test for redaction on publish is not an exception to that rule.
GROQ_SAMPLE = "gsk" + "_" + "AAAABBBBCCCCDDDDEEEEFFFF"

# ``<domain>.<noun>.<past-tense-verb>``, lowercase, dot-separated, two or three segments. The
# tense itself is not machine-checkable; the shape is, and the shape is what v2 parses.
SCHEME = re.compile(r"^[a-z]+(?:\.[a-z][a-z_]*){1,2}$")


# ---- helpers -----------------------------------------------------------------------------------


def settings() -> Settings:
    """A keyless offline ``Settings``, built without reading the process environment."""
    return Settings(
        home=REPO_ROOT / ".harnessed",
        provider="fake",
        model="fake-1",
        small_model="fake-1",
        base_url=None,
        embed_base_url=None,
        embed_model=None,
        retrieval_token_budget=4000,
        max_iterations=12,
    )


def architecture_taxonomy() -> frozenset[str]:
    """The names frozen in ``architecture.md`` open question 12, read from the document itself.

    Derived rather than pinned as a literal: a later ticket that adds a name must not have to
    edit this test, but it may never drop one the plan froze.
    """
    text = ARCHITECTURE.read_text(encoding="utf-8")
    section = text.split("### 12. Event taxonomy", 1)[1]
    block = section.split("```", 2)[1]
    return frozenset(block.split())


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database file under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


def drive_fixture_run(bus: EventBus, provider: FakeProvider) -> int:
    """Drive one fixture-backed run, publishing what ``loop/engine.py`` will publish.

    Two iterations: the corpus answers the first with a tool call and the second with a final
    message, so the sequence covers a loop turn, a provider round trip and a tool call.
    """
    bus.publish("run.started", {"scope": REPLAY_SCOPE})
    messages: list[Message] = [Message(role="user", content="what does the readme say")]
    iteration = 0
    while iteration < 4:
        iteration += 1
        bus.publish("loop.iteration.started", {"iteration": iteration})
        request = ChatRequest(
            model="fake-1", messages=tuple(messages), metadata={"scope": REPLAY_SCOPE}
        )
        bus.publish("provider.request.sent", {"model": request.model, "iteration": iteration})
        response = provider.complete(request)
        bus.publish("provider.response.received", {"stop_reason": response.stop_reason})
        messages.append(
            Message(role="assistant", content=response.content, tool_calls=response.tool_calls)
        )
        for call in response.tool_calls:
            bus.publish("tool.call.started", {"tool": call.name})
            result = ToolResult(call_id=call.id, content="offline by default")
            bus.publish("tool.call.finished", {"tool": call.name, "is_error": result.is_error})
            messages.append(Message(role="tool", content=result.content, tool_results=(result,)))
        bus.publish("loop.iteration.finished", {"iteration": iteration})
        if response.stop_reason != "tool_use":
            break
    bus.publish("run.finished", {"iterations": iteration})
    return iteration


# ---- the frozen taxonomy -----------------------------------------------------------------------


def test_the_taxonomy_is_a_frozen_set() -> None:
    assert isinstance(names_module.NAMES, frozenset)
    assert all(isinstance(name, str) for name in names_module.NAMES)


def test_every_name_the_plan_froze_is_present() -> None:
    """Names may be added by a later ticket. None may ever be dropped or renamed."""
    assert architecture_taxonomy() <= names_module.NAMES


def test_every_name_obeys_the_naming_scheme() -> None:
    offenders = sorted(name for name in names_module.NAMES if not SCHEME.match(name))
    assert offenders == []


def test_the_isolation_name_is_in_the_taxonomy() -> None:
    assert names_module.OBSERVER_FAILED in names_module.NAMES


def test_publishing_an_unknown_name_raises(conn: sqlite3.Connection) -> None:
    bus = EventBus(run_id="run_test", conn=conn)
    with pytest.raises(UnknownEventError) as raised:
        bus.publish("run.starrted", {})
    assert "run.starrted" in str(raised.value)
    assert isinstance(raised.value, HarnessError)


def test_an_unknown_name_persists_nothing(conn: sqlite3.Connection) -> None:
    bus = EventBus(run_id="run_test", conn=conn)
    with pytest.raises(UnknownEventError):
        bus.publish("nope.not.real", {})
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


# ---- what the bus stamps -----------------------------------------------------------------------


def test_the_bus_injects_run_id_ts_and_a_monotonic_seq(frozen_clock: object) -> None:
    bus = EventBus(run_id="run_stamped")
    first = bus.publish("run.started", {})
    second = bus.publish("run.finished", {})
    assert (first.run_id, second.run_id) == ("run_stamped", "run_stamped")
    assert (first.seq, second.seq) == (1, 2)
    assert first.ts == clock.isoformat(clock.now())
    assert first.ts.endswith("Z")


def test_seq_is_per_run_not_per_process() -> None:
    one = EventBus(run_id="run_one")
    other = EventBus(run_id="run_other")
    one.publish("run.started", {})
    one.publish("run.finished", {})
    assert other.publish("run.started", {}).seq == 1


def test_a_bus_without_a_run_id_generates_one() -> None:
    assert EventBus().run_id.startswith("run_")


def test_the_publisher_may_not_supply_the_injected_fields() -> None:
    bus = EventBus(run_id="run_supplied")
    with pytest.raises(EventError) as raised:
        bus.publish("run.started", {"seq": 7})
    assert "seq" in str(raised.value)
    assert not isinstance(raised.value, UnknownEventError)


def test_a_payload_is_optional() -> None:
    assert EventBus(run_id="run_bare").publish("run.started").payload == {}


def test_an_event_is_immutable() -> None:
    event = EventBus(run_id="run_frozen").publish("run.started", {})
    with pytest.raises(AttributeError):
        event.seq = 99  # type: ignore[misc]


# ---- redaction on publish (FR-13 rule 7) --------------------------------------------------------


def test_a_payload_is_scrubbed_before_it_reaches_an_observer(clean_env: None) -> None:
    bus = EventBus(run_id="run_secret")
    observer = RecordingObserver()
    bus.subscribe(observer)
    bus.publish("provider.request.sent", {"headers": {"authorization": GROQ_SAMPLE}})
    delivered = observer.events[0].payload["headers"]
    assert delivered == {"authorization": "[redacted:groq_key]"}


def test_a_nested_payload_is_scrubbed_before_it_is_persisted(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    bus = EventBus(run_id="run_secret", conn=conn)
    bus.publish("error.raised", {"trace": ["header", {"key": GROQ_SAMPLE}]})
    stored = conn.execute("SELECT payload FROM events").fetchone()[0]
    assert GROQ_SAMPLE not in stored
    assert "[redacted:groq_key]" in stored


def test_the_original_payload_is_left_alone(clean_env: None) -> None:
    """Scrubbing copies. A publisher's own dict is not mutated under it."""
    bus = EventBus(run_id="run_copy")
    payload: dict[str, object] = {"key": GROQ_SAMPLE}
    bus.publish("error.raised", payload)
    assert payload == {"key": GROQ_SAMPLE}


# ---- observer isolation -------------------------------------------------------------------------


def test_an_observer_that_raises_does_not_break_the_run(conn: sqlite3.Connection) -> None:
    provider = FakeProvider(settings())
    bus = EventBus(run_id="run_isolated", conn=conn)
    failing = RaisingObserver()
    recording = RecordingObserver()
    bus.subscribe(failing)
    bus.subscribe(recording)

    iterations = drive_fixture_run(bus, provider)

    assert iterations == 2
    assert failing.calls > 0
    assert is_subsequence(EXPECTED_SEQUENCE, recording.names)


def test_a_failing_observer_publishes_observer_failed() -> None:
    bus = EventBus(run_id="run_failed")
    bus.subscribe(RaisingObserver(message="boom"))
    recording = RecordingObserver()
    bus.subscribe(recording)

    bus.publish("run.started", {})

    failures = recording.named("observer.failed")
    assert len(failures) == 1
    payload = failures[0].payload
    assert payload["event"] == "run.started"
    assert payload["error"] == "RuntimeError"
    assert payload["message"] == "boom"
    assert "RaisingObserver" in str(payload["observer"])


def test_an_observer_failing_on_observer_failed_does_not_recurse() -> None:
    bus = EventBus(run_id="run_recursion")
    failing = RaisingObserver()
    bus.subscribe(failing)
    bus.publish("run.started", {})
    # One failure for run.started, one for the observer.failed that reported it, and no more.
    assert failing.calls == 2


def test_a_later_observer_still_sees_an_event_an_earlier_one_failed_on() -> None:
    bus = EventBus(run_id="run_order")
    bus.subscribe(RaisingObserver())
    recording = RecordingObserver()
    bus.subscribe(recording)
    bus.publish("run.started", {})
    assert "run.started" in recording.names


def test_observer_failed_is_itself_persisted(conn: sqlite3.Connection) -> None:
    bus = EventBus(run_id="run_persisted_failure", conn=conn)
    bus.subscribe(RaisingObserver())
    bus.publish("run.started", {})
    stored = [row[0] for row in conn.execute("SELECT name FROM events ORDER BY seq")]
    assert stored == ["run.started", "observer.failed"]


# ---- persistence ---------------------------------------------------------------------------------


def test_every_published_event_is_persisted_keyed_by_run_id_and_seq(
    conn: sqlite3.Connection, frozen_clock: object
) -> None:
    provider = FakeProvider(settings())
    bus = EventBus(run_id="run_persisted", conn=conn)
    drive_fixture_run(bus, provider)

    rows = conn.execute(
        "SELECT run_id, seq, name, payload, created_at FROM events ORDER BY seq"
    ).fetchall()
    assert [row[2] for row in rows] == list(EXPECTED_SEQUENCE)
    assert [row[1] for row in rows] == list(range(1, len(EXPECTED_SEQUENCE) + 1))
    assert {row[0] for row in rows} == {"run_persisted"}
    assert {row[4] for row in rows} == {clock.isoformat(clock.now())}
    assert json.loads(rows[0][3]) == {"scope": REPLAY_SCOPE}


def test_two_runs_share_the_table_without_colliding(conn: sqlite3.Connection) -> None:
    EventBus(run_id="run_a", conn=conn).publish("run.started", {})
    EventBus(run_id="run_b", conn=conn).publish("run.started", {})
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_a_bus_without_a_connection_publishes_in_process_only() -> None:
    """The bus is usable before the database is open; persistence is what a connection adds."""
    observer = RecordingObserver()
    bus = EventBus(run_id="run_memory_only")
    bus.subscribe(observer)
    bus.publish("run.started", {})
    assert observer.names == ["run.started"]


# ---- the ordered sequence FR-21 asserts ---------------------------------------------------------


def test_a_recording_observer_receives_the_expected_ordered_sequence(
    conn: sqlite3.Connection,
) -> None:
    provider = FakeProvider(settings())
    bus = EventBus(run_id="run_sequence", conn=conn)
    observer = RecordingObserver()
    bus.subscribe(observer)

    drive_fixture_run(bus, provider)

    assert observer.names == list(EXPECTED_SEQUENCE)


def test_an_observer_subscribed_late_sees_only_what_followed() -> None:
    bus = EventBus(run_id="run_late")
    bus.publish("run.started", {})
    observer = RecordingObserver()
    bus.subscribe(observer)
    bus.publish("run.finished", {})
    assert observer.names == ["run.finished"]


def test_every_name_the_run_publishes_is_in_the_taxonomy(conn: sqlite3.Connection) -> None:
    provider = FakeProvider(settings())
    bus = EventBus(run_id="run_taxonomy", conn=conn)
    observer = RecordingObserver()
    bus.subscribe(observer)
    drive_fixture_run(bus, provider)
    assert set(observer.names) <= names_module.NAMES


# ---- structure -------------------------------------------------------------------------------


#: The modules permitted to catch broadly, and the reason each one is. Each guards a call it
#: cannot let escape: ``bus.py`` around an observer (FR-21, an observer that raises must not break
#: the run), ``tools/registry.py`` around a tool body (FR-6, every tool outcome leaves the seam as
#: a ``ToolResult``) and ``control/worker.py`` around a run on the control app's worker (decision
#: 49: on ``HarnessError`` "or anything else" it publishes ``error.raised``, so the future always
#: completes and the worker is never stuck busy) and ``cli/task_run.py`` around a task's message
#: (ticket 240: whatever a message raises is the task's own end, ``failed`` / ``raised`` before
#: ``run.finished``, never a task left ``running`` with nothing driving it). A seam that promised
#: "errors are values" while catching only the errors it anticipated would not be a seam.
BROAD_CATCH_OWNERS = [
    "acp/delegate.py",
    "cli/task_run.py",
    "control/worker.py",
    "events/bus.py",
    "tools/registry.py",
]


def broad_catch_owners(root: Path) -> list[str]:
    """Every module under ``root`` with a bare ``except`` or ``except Exception``."""
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = node.type
            broad = caught is None or (isinstance(caught, ast.Name) and caught.id == "Exception")
            if broad:
                offenders.append(path.relative_to(root).as_posix())
    return sorted(set(offenders))


def test_the_named_seams_are_the_only_modules_that_catch_broadly() -> None:
    """Architecture: a broad catch is sanctioned only where it guards foreign code.

    Compared as an exact set (ratified 2026-10-11, FR-173): a sixth owner needs a ruling.
    """
    assert set(broad_catch_owners(PACKAGE_ROOT)) == set(BROAD_CATCH_OWNERS)
    assert len(BROAD_CATCH_OWNERS) == 5


def test_a_sixth_broad_catch_owner_fails_the_pin(tmp_path: Path) -> None:
    for owner in BROAD_CATCH_OWNERS:
        target = tmp_path / owner
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("try:\n    pass\nexcept Exception:\n    pass\n", encoding="utf-8")
    assert set(broad_catch_owners(tmp_path)) == set(BROAD_CATCH_OWNERS)
    (tmp_path / "loop").mkdir()
    (tmp_path / "loop" / "engine.py").write_text(
        "try:\n    pass\nexcept:\n    pass\n", encoding="utf-8"
    )
    assert set(broad_catch_owners(tmp_path)) != set(BROAD_CATCH_OWNERS)


def test_the_bus_imports_only_the_leaves_plus_the_two_memory_modules_it_must_use() -> None:
    """The one sanctioned crossing of the import table, enumerated so it cannot widen.

    ``events -> config`` is the direction the plan drew, but the same plan maps FR-13 onto
    ``events/bus.py`` (every publish is redacted) and FR-21 onto ``001_init.sql`` (every publish
    is a row). Both need a module under ``memory/``. The crossing is these two and no others,
    and neither of them imports ``events`` back.
    """
    tree = ast.parse((PACKAGE_ROOT / "events" / "bus.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("harnessed"):
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
    assert imported == {
        "harnessed.clock",
        "harnessed.ids",
        "harnessed.errors.EventError",
        "harnessed.errors.UnknownEventError",
        "harnessed.events.names.NAMES",
        "harnessed.events.names.OBSERVER_FAILED",
        "harnessed.memory.db",
        "harnessed.memory.redact",
    }


def test_the_package_exports_the_seam() -> None:
    assert bus_module.EventBus is EventBus
    assert issubclass(Event, object)


def test_task_finished_is_frozen_and_is_its_own_terminal_pair() -> None:
    """Ticket 239 (FR-140): a task cancelled at rest is a one-event session (decision 17)."""
    from harnessed.events import read

    assert "task.finished" in names_module.NAMES
    assert read.TERMINAL_PAIRS["task.finished"] == "task.finished"


def test_task_started_and_paused_are_frozen_and_neither_is_a_terminal_pair() -> None:
    """Ticket 240 (FR-141): both publish inside a message, so neither ends a stream."""
    from harnessed.events import read

    assert {"task.started", "task.paused"} <= names_module.NAMES
    halves = set(read.TERMINAL_PAIRS) | set(read.TERMINAL_PAIRS.values())
    assert not {"task.started", "task.paused"} & halves


def test_memory_consolidation_finished_is_frozen_and_self_paired() -> None:
    """Ticket 249 (FR-152): a batch is a one-event session, so its finish is its own start."""
    from harnessed.events import read

    name = "memory.consolidation.finished"
    assert name in names_module.NAMES
    assert read.TERMINAL_PAIRS[name] == name
