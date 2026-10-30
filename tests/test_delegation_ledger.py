"""FR-23 - the delegation ledger prevents a second destructive run.

This is the most consequential ledger in v1, because re-running a coding agent against a repo is
destructive (``state-and-idempotency.md``). Every claim here is therefore made against a **real
child process**: the agent is ``tests/support/stub_acp_agent.py`` spawned through this
interpreter and driven by the real ``acp/delegate.py`` over the real protocol. A mocked spawn
cannot tell "the ledger stopped the second run" from "the client never started a process at all",
which is the only failure this file exists to catch.

Three things are worth knowing before reading the assertions.

**The spawn counter watches two targets, not one.** ``asyncio.create_subprocess_exec`` reaches
``subprocess.Popen`` on POSIX but builds an ``asyncio.windows_utils.Popen`` on Windows, so
``support.spies.watch_spawns`` installs a spy on each. It counts command lines, never method
calls, and every assertion filters on the stub's path because a process-tree kill is itself a
spawn.

**The attempt counter rides in the ``outcome`` column.** ``003_ledgers.sql`` is committed and
migrations are forward-only, so the count travels in the canonical JSON payload beside the
compact summary rather than in a column of its own.

**The cap is expressed as an error.** A third call for an exhausted hash raises
``DelegationError`` rather than returning a ``Delegation``, because that is already how this
module reports a delegation that did not happen, and ``tools/builtin/delegate.py`` turns it into
an error ``ToolResult`` - which is what "returned immediately" means to the loop.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from harnessed import clock
from harnessed.acp import config as acp_config
from harnessed.acp import delegate as acp_delegate
from harnessed.acp import ledger
from harnessed.errors import AcpVersionMismatch, DelegationError, DelegationTimeout
from harnessed.events.bus import EventBus
from harnessed.memory import db, episodic
from support.spies import RecordingObserver, SpawnSpy, watch_spawns

REPO_ROOT = Path(__file__).resolve().parents[1]
STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_acp_agent.py"

# Ticket 117: under `--cov` a stub agent that inherits coverage's variables starts too slowly.
pytestmark = pytest.mark.usefixtures("no_child_coverage")
LEDGER_PATH = REPO_ROOT / "src" / "harnessed" / "acp" / "ledger.py"
DELEGATE_PATH = REPO_ROOT / "src" / "harnessed" / "acp" / "delegate.py"

# Generous enough for a cold interpreter start on Windows on the happy path.
READ_TIMEOUT = 10.0
# Short enough that a `hang` delegation costs about a second, and it costs it twice here.
SHORT_TIMEOUT = 1.5


# ---- the world every test delegates into --------------------------------------------------------


@pytest.fixture
def harness_home(tmp_path: Path) -> Path:
    home = tmp_path / ".harnessed"
    home.mkdir()
    return home


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "harness.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(db_path)
    db.migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def stub_agents(path: Path, **fields: str) -> Path:
    """An ``agents.toml`` whose one agent is the stub, spawned through this interpreter."""
    declared = {
        "command": json.dumps(sys.executable),
        "args": f"[{json.dumps(str(STUB_PATH))}]",
        "install": '"install the probe agent first"',
        "timeout_s": str(READ_TIMEOUT),
    }
    declared.update(fields)
    lines = ["[agents.probe]"] + [f"{key} = {value}" for key, value in declared.items()]
    target = path / "agents.toml"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def probe_spec(path: Path, **fields: str) -> acp_config.AgentSpec:
    """The stub agent, loaded through the real loader so no test builds a spec by hand."""
    return acp_config.agent_named(stub_agents(path, **fields), None)


def stub_spawns(spy: SpawnSpy) -> int:
    """How many times the stub itself was started, ignoring any process-tree kill."""
    return len(spy.matching(str(STUB_PATH)))


def rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return list(conn.execute("SELECT task_hash, status, outcome FROM delegations"))


def hash_for(spec: acp_config.AgentSpec, task: str, acceptance: str) -> str:
    """The key the delegation under test writes under, computed the way the module computes it."""
    return ledger.task_hash(
        agent=spec.name, cwd=spec.cwd or str(Path.cwd()), task=task, acceptance=acceptance
    )


# ---- 1. the hash ---------------------------------------------------------------------------------


def test_the_hash_is_canonical_json_over_the_four_inputs_prefixed_task() -> None:
    computed = ledger.task_hash(agent="probe", cwd="/work", task="do it", acceptance="done")
    assert computed.startswith("task_")
    assert ledger.canonical(agent="probe", cwd="/work", task="do it", acceptance="done") == (
        '{"acceptance":"done","agent":"probe","cwd":"/work","task":"do it"}'
    )


def test_the_hash_is_stable_across_keyword_order() -> None:
    first = ledger.task_hash(agent="probe", cwd="/work", task="do it", acceptance="done")
    second = ledger.task_hash(acceptance="done", task="do it", cwd="/work", agent="probe")
    assert first == second


@pytest.mark.parametrize("field", ["agent", "cwd", "task", "acceptance"])
def test_changing_any_one_of_the_four_changes_the_hash(field: str) -> None:
    base = {"agent": "probe", "cwd": "/work", "task": "do it", "acceptance": "done"}
    changed = dict(base, **{field: base[field] + "!"})
    assert ledger.task_hash(**base) != ledger.task_hash(**changed)


# ---- 2. the row is written before the spawn ------------------------------------------------------


def test_the_row_is_on_disk_at_the_instant_the_process_exists(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection, db_path: Path
) -> None:
    """Proved from inside the run: the probe reads the ledger when the pid first exists.

    Asserting the row afterwards would only show that the write happened, never that it happened
    before the spawn, which is the whole claim. ``on_spawn`` fires on the background loop's
    thread and a SQLite connection belongs to the thread that made it, so the probe opens its
    own - which makes the claim stronger, because a row another connection can see is a row that
    is committed rather than merely queued.
    """
    spec = probe_spec(tmp_path)
    seen: list[str] = []

    def probe(_pid: int) -> None:
        onlooker = db.connect(db_path)
        try:
            recorded = ledger.get(onlooker, hash_for(spec, "do it", "the thing is done"))
        finally:
            onlooker.close()
        seen.append("absent" if recorded is None else recorded.status)

    acp_delegate.delegate(
        spec,
        task="do it",
        acceptance="the thing is done",
        home=harness_home,
        store=store,
        on_spawn=probe,
    )
    assert seen == [ledger.ATTEMPTING]


def test_a_crash_during_the_spawn_is_now_a_recorded_failure_not_an_attempt(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 152, decision 69: a crash outside the three named errors used to leave the row at
    ``attempting`` forever (the exact defect FR-93 closes). It now settles ``failed`` like every
    other terminal path, and the caller sees a chained ``DelegationError`` rather than the raw
    crash.
    """

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the spawn died")

    monkeypatch.setattr(acp_delegate, "_converse", explode)
    with pytest.raises(DelegationError) as caught:
        acp_delegate.delegate(
            probe_spec(tmp_path),
            task="do it",
            acceptance="done",
            home=harness_home,
            store=store,
        )
    assert isinstance(caught.value.__cause__, RuntimeError)
    recorded = rows(store)
    assert len(recorded) == 1
    assert recorded[0][1] == ledger.FAILED


# ---- 3. the identical delegation spawns exactly once ---------------------------------------------


def test_the_identical_delegation_twice_spawns_exactly_one_process(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    spec = probe_spec(tmp_path)
    with watch_spawns() as spy:
        first = acp_delegate.delegate(
            spec, task="do it", acceptance="done", home=harness_home, store=store
        )
        second = acp_delegate.delegate(
            spec, task="do it", acceptance="done", home=harness_home, store=store
        )
    assert stub_spawns(spy) == 1
    assert first.as_dict() == second.as_dict()
    assert second.status == "succeeded"


def test_a_changed_task_does_spawn_a_second_time(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    spec = probe_spec(tmp_path)
    with watch_spawns() as spy:
        acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home, store=store)
        acp_delegate.delegate(
            spec, task="do it twice", acceptance="done", home=harness_home, store=store
        )
    assert stub_spawns(spy) == 2
    assert len(rows(store)) == 2


def test_a_changed_acceptance_does_spawn_a_second_time(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    spec = probe_spec(tmp_path)
    with watch_spawns() as spy:
        acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home, store=store)
        acp_delegate.delegate(
            spec, task="do it", acceptance="done and reviewed", home=harness_home, store=store
        )
    assert stub_spawns(spy) == 2


def test_a_changed_cwd_does_spawn_a_second_time(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    here = probe_spec(tmp_path)
    there = probe_spec(tmp_path, cwd=json.dumps(str(elsewhere)))
    assert here.cwd != there.cwd
    with watch_spawns() as spy:
        acp_delegate.delegate(here, task="do it", acceptance="done", home=harness_home, store=store)
        acp_delegate.delegate(
            there, task="do it", acceptance="done", home=harness_home, store=store
        )
    assert stub_spawns(spy) == 2


def test_a_changed_agent_name_does_spawn_a_second_time(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    first = probe_spec(tmp_path)
    renamed = tmp_path / "renamed"
    renamed.mkdir()
    body = stub_agents(renamed).read_text(encoding="utf-8").replace("agents.probe", "agents.other")
    (renamed / "agents.toml").write_text(body, encoding="utf-8")
    second = acp_config.agent_named(renamed / "agents.toml", None)
    assert (first.name, second.name) == ("probe", "other")
    with watch_spawns() as spy:
        acp_delegate.delegate(
            first, task="do it", acceptance="done", home=harness_home, store=store
        )
        acp_delegate.delegate(
            second, task="do it", acceptance="done", home=harness_home, store=store
        )
    assert stub_spawns(spy) == 2


# ---- 4. the cached outcome and its event ---------------------------------------------------------


def test_a_succeeded_hash_returns_the_cached_outcome_and_publishes_cached(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    bus = EventBus()
    observer = RecordingObserver()
    bus.subscribe(observer)
    spec = probe_spec(tmp_path)
    acp_delegate.delegate(
        spec, task="do it", acceptance="done", home=harness_home, store=store, bus=bus
    )
    assert "acp.delegation.cached" not in observer.names
    cached = acp_delegate.delegate(
        spec, task="do it", acceptance="done", home=harness_home, store=store, bus=bus
    )
    published = observer.named("acp.delegation.cached")
    assert len(published) == 1
    assert published[0].payload["status"] == "succeeded"
    assert published[0].payload["task_hash"] == hash_for(spec, "do it", "done")
    assert cached.status == "succeeded"


def test_the_cached_call_publishes_no_second_started_or_finished(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    bus = EventBus()
    observer = RecordingObserver()
    bus.subscribe(observer)
    spec = probe_spec(tmp_path)
    for _ in range(2):
        acp_delegate.delegate(
            spec, task="do it", acceptance="done", home=harness_home, store=store, bus=bus
        )
    assert len(observer.named("acp.delegation.started")) == 1
    assert len(observer.named("acp.delegation.finished")) == 1


def test_a_cached_call_writes_no_second_transcript(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    spec = probe_spec(tmp_path)
    for _ in range(2):
        acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home, store=store)
    assert len(sorted((harness_home / "acp").glob("*.json"))) == 1


def test_without_a_store_nothing_is_cached_and_the_agent_runs_again(
    tmp_path: Path, harness_home: Path
) -> None:
    """The ledger is the store. No store, no done-state, and no silent pretence of one."""
    spec = probe_spec(tmp_path)
    with watch_spawns() as spy:
        acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home)
        acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home)
    assert stub_spawns(spy) == 2


# ---- 5. the attempt cap --------------------------------------------------------------------------


def hanging_spec(path: Path, monkeypatch: pytest.MonkeyPatch) -> acp_config.AgentSpec:
    """A stub that never answers, so the delegation really times out rather than pretending to."""
    monkeypatch.setenv("HARNESSED_STUB_MODE", "hang")
    return probe_spec(path, timeout_s=str(SHORT_TIMEOUT))


def test_two_failed_attempts_then_the_third_call_never_spawns(
    tmp_path: Path,
    harness_home: Path,
    store: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = hanging_spec(tmp_path, monkeypatch)
    with watch_spawns() as spy:
        for _ in range(2):
            with pytest.raises(DelegationTimeout):
                acp_delegate.delegate(
                    spec, task="do it", acceptance="done", home=harness_home, store=store
                )
        assert stub_spawns(spy) == 2
        with pytest.raises(DelegationError) as refused:
            acp_delegate.delegate(
                spec, task="do it", acceptance="done", home=harness_home, store=store
            )
        assert stub_spawns(spy) == 2
    assert "2" in str(refused.value)
    assert hash_for(spec, "do it", "done") in str(refused.value)


def test_the_cap_is_two_so_a_single_failure_is_retried(
    tmp_path: Path,
    harness_home: Path,
    store: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = hanging_spec(tmp_path, monkeypatch)
    with watch_spawns() as spy:
        for _ in range(2):
            with pytest.raises(DelegationTimeout):
                acp_delegate.delegate(
                    spec, task="do it", acceptance="done", home=harness_home, store=store
                )
    assert stub_spawns(spy) == 2
    assert ledger.ATTEMPT_CAP == 2


def test_the_recorded_failure_is_what_the_third_call_answers_with(
    tmp_path: Path,
    harness_home: Path,
    store: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = hanging_spec(tmp_path, monkeypatch)
    for _ in range(2):
        with pytest.raises(DelegationTimeout):
            acp_delegate.delegate(
                spec, task="do it", acceptance="done", home=harness_home, store=store
            )
    recorded = ledger.get(store, hash_for(spec, "do it", "done"))
    assert recorded is not None
    assert recorded.status == ledger.FAILED
    assert recorded.attempts == 2
    assert recorded.exhausted
    with pytest.raises(DelegationError) as refused:
        acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home, store=store)
    assert recorded.failure in str(refused.value)


EVIDENCE_152 = REPO_ROOT / "evidence" / "152"


def test_an_unnamed_error_is_a_recorded_failure_not_a_success(
    tmp_path: Path,
    harness_home: Path,
    store: sqlite3.Connection,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-93, decision 69: the fourth failure settles too, and no attempt is left dangling."""
    monkeypatch.setenv("HARNESSED_STUB_MODE", "internal_error")
    spec = probe_spec(tmp_path)
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    key = hash_for(spec, "do it", "done")
    before: list[str] = []

    def probe(_pid: int) -> None:
        # ``on_spawn`` fires on the background loop's own thread, so this reads through its own
        # connection rather than ``store`` (ticket 022).
        onlooker = db.connect(db_path)
        try:
            recorded_row = ledger.get(onlooker, key)
        finally:
            onlooker.close()
        before.append("absent" if recorded_row is None else recorded_row.status)

    with pytest.raises(DelegationError):
        acp_delegate.delegate(
            spec,
            task="do it",
            acceptance="done",
            home=harness_home,
            store=store,
            bus=bus,
            on_spawn=probe,
        )
    recorded = rows(store)
    assert len(recorded) == 1
    assert recorded[0][1] == ledger.FAILED
    assert [row[1] for row in recorded if row[1] == ledger.ATTEMPTING] == []
    finished = seen.named("acp.delegation.finished")
    assert len(finished) == 1
    assert finished[0].payload["status"] == "error"
    held = ledger.get(store, key)
    assert held is not None and held.failure == "-32603 Internal error"
    # Re-run after the failure: the ledger must not serve it back as a cached success.
    with pytest.raises(DelegationError):
        acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home, store=store)
    after_rerun = rows(store)[0][1]
    assert after_rerun == ledger.FAILED
    EVIDENCE_152.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_152 / "ledger-and-event.txt").write_text(
        "\n".join(
            [
                f"row before settling (on_spawn): {before}",
                f"row after settling: status={recorded[0][1]!r} outcome={recorded[0][2]!r}",
                f"acp.delegation.finished published: {len(finished)} time(s), "
                f"payload={finished[0].payload!r}",
                f"row after the re-run: status={after_rerun!r} (not served from cache as a "
                f"success)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_a_version_mismatch_is_a_recorded_failure_not_a_success(
    tmp_path: Path,
    harness_home: Path,
    store: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "bad_version")
    spec = probe_spec(tmp_path)
    with pytest.raises(AcpVersionMismatch):
        acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home, store=store)
    recorded = rows(store)
    assert len(recorded) == 1
    assert recorded[0][1] == ledger.FAILED


# ---- 6. what the ledger stores -------------------------------------------------------------------


def test_the_stored_outcome_is_the_compact_summary_and_not_the_transcript(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    spec = probe_spec(tmp_path)
    result = acp_delegate.delegate(
        spec, task="do it", acceptance="done", home=harness_home, store=store
    )
    recorded = ledger.get(store, hash_for(spec, "do it", "done"))
    assert recorded is not None
    assert set(recorded.summary) == {
        "status",
        "session_id",
        "files_changed",
        "summary",
        "acceptance",
    }
    assert recorded.summary["summary"] == result.summary
    assert "transcript" not in str(rows(store)[0][2])


def test_the_full_transcript_lives_on_disk_and_in_episodic_memory_instead(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    bus = EventBus()
    acp_delegate.delegate(
        probe_spec(tmp_path),
        task="do it",
        acceptance="done",
        home=harness_home,
        store=store,
        bus=bus,
    )
    on_disk = sorted((harness_home / "acp").glob("*.json"))
    assert len(on_disk) == 1
    assert "transcript" in on_disk[0].read_text(encoding="utf-8")
    episodes = episodic.for_run(store, bus.run_id)
    assert [episode.kind for episode in episodes] == [acp_delegate.EPISODE_KIND]
    assert "transcript" in episodes[0].text


def test_the_attempt_counter_rides_in_the_outcome_payload(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    spec = probe_spec(tmp_path)
    acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home, store=store)
    payload = json.JSONDecoder().decode(str(rows(store)[0][2]))
    assert payload["attempts"] == 1
    assert set(payload) == {"attempts", "summary", "failure"}


def test_the_stored_row_carries_the_four_hashed_inputs(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    spec = probe_spec(tmp_path)
    acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home, store=store)
    recorded = ledger.get(store, hash_for(spec, "do it", "done"))
    assert recorded is not None
    assert (recorded.agent, recorded.task, recorded.acceptance) == ("probe", "do it", "done")
    assert recorded.cwd == (spec.cwd or str(Path.cwd()))


def test_an_unknown_hash_is_none_rather_than_an_error(store: sqlite3.Connection) -> None:
    assert ledger.get(store, "task_nothing") is None


def test_a_row_with_no_outcome_at_all_reads_back_as_a_fresh_attempt(
    store: sqlite3.Connection,
) -> None:
    """``outcome`` is nullable in the committed schema, so reading one must not be a crash.

    Nothing this module writes leaves the column empty, but "never assume a resumed run starts
    clean" is a rule about rows this process did not write.
    """
    db.write(
        store,
        (
            (
                "INSERT INTO delegations "
                "(task_hash, agent, cwd, task, acceptance, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("task_bare", "probe", "/work", "do it", "done", ledger.ATTEMPTING, "now"),
            ),
        ),
    )
    recorded = ledger.get(store, "task_bare")
    assert recorded is not None
    assert (recorded.attempts, recorded.summary, recorded.failure) == (0, {}, "")
    assert not recorded.exhausted


# ---- 7. the pattern itself -----------------------------------------------------------------------


def module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def function_named(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined")


def call_names(node: ast.AST) -> list[str]:
    """Every ``a.b(...)`` inside ``node``, in source order, as ``"a.b"``."""
    found = [
        (child.lineno, f"{child.func.value.id}.{child.func.attr}")
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
    ]
    return [name for _, name in sorted(found)]


def test_the_read_comes_before_the_write_which_comes_before_the_spawn() -> None:
    """The pattern verbatim: read the done-state, persist it, then do the destructive work."""
    order = call_names(function_named(module_ast(DELEGATE_PATH), "delegate"))
    assert order.index("ledger.get") < order.index("ledger.begin")
    assert order.index("ledger.begin") < order.index("runtime.submit")


def test_every_ledger_write_is_its_own_transaction_and_is_never_batched() -> None:
    """MUST rule 6: no "optimization" that batches one ledger write behind another step."""
    writes = [
        node
        for node in ast.walk(module_ast(LEDGER_PATH))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write"
    ]
    assert len(writes) == 2
    for call in writes:
        statements = call.args[1]
        assert isinstance(statements, ast.Tuple)
        assert len(statements.elts) == 1


def test_the_ledger_settles_on_every_terminal_path_of_delegate() -> None:
    """Six ways a delegation ends, six settlements: timed out, mismatched, failed, quota-refused,
    errored, succeeded.

    Ticket 088 added ``failed``: an agent that closed the connection before answering. Ticket 152
    added ``error``: any other exception, caught after the three named ones (FR-93, decision 69).
    Ticket 194 added ``quota_refused``: an error carrying its template's recorded marker.
    """
    order = call_names(function_named(module_ast(DELEGATE_PATH), "delegate"))
    assert order.count("ledger.settle") == 6


def test_the_ledger_never_spells_the_quarantined_string_parser() -> None:
    """Ticket 003's quarantine is a package-wide text scan and it reaches this file too.

    The ``outcome`` column really does have to be decoded, so the decoder object is used
    directly - the same distinction ticket 017's ``json_object`` grader drew.
    """
    text = LEDGER_PATH.read_text(encoding="utf-8")
    assert "JSONDecoder" in text
    assert "loads" not in text


def test_the_ledger_imports_only_what_the_import_table_allows() -> None:
    """``acp -> events, memory, config``, plus the leaves that import nothing internal."""
    permitted = {
        "harnessed.clock",
        "harnessed.memory.db",
        "harnessed.memory.redact",
    }
    imported = set()
    for node in ast.walk(module_ast(LEDGER_PATH)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        base = node.module
        if base != "harnessed" and not base.startswith("harnessed."):
            continue
        # `from harnessed import clock` records the bare package, so expand it before matching:
        # admitting `harnessed` into the permitted set would make every prefix match instead.
        prefix = "harnessed" if base == "harnessed" else base
        imported.update(f"{prefix}.{alias.name}" for alias in node.names)
    assert imported <= permitted, imported - permitted


# ---- 8. a `cli` delegation writes the same table (FR-99, decision 62) ----------------------------


def test_a_cli_delegation_writes_into_the_same_delegations_table_an_acp_one_does(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    """Ticket 158: ``acp/cli_runner.py`` is a second writer of the one ``delegations`` ledger,
    never a table of its own. One ACP delegation and one ``cli`` delegation land as two rows in
    the same ``SELECT * FROM delegations``, through the exact same ``ledger.begin``/``settle``."""
    from harnessed.acp import cli_runner
    from harnessed.acp.config import AgentSpec

    acp_delegate.delegate(
        probe_spec(tmp_path), task="do it", acceptance="done", home=harness_home, store=store
    )
    cli_spec = AgentSpec(
        name="cli-probe",
        command=sys.executable,
        args=(str(REPO_ROOT / "tests" / "support" / "stub_cli_agent.py"), "--task", "{task}"),
        cwd=str(tmp_path),
        kind="cli",
        input="stdin",
    )
    cli_runner.run(cli_spec, task="do it", acceptance="done", store=store)
    recorded = rows(store)
    assert len(recorded) == 2
    assert {row[1] for row in recorded} == {ledger.SUCCEEDED}


# ---- ticket 194: recent_refusals -----------------------------------------------------------------


def _settled(conn: sqlite3.Connection, agent: str, failure: str, minutes_ago: int) -> None:
    key = ledger.task_hash(agent=agent, cwd="/w", task=failure, acceptance="a")
    ledger.begin(conn, key, agent=agent, cwd="/w", task=failure, acceptance="a", attempts=1)
    ledger.settle(conn, key, status=ledger.FAILED, attempts=1, failure=failure)
    finished = clock.isoformat(clock.now() - timedelta(minutes=minutes_ago))
    conn.execute("UPDATE delegations SET finished_at = ? WHERE task_hash = ?", (finished, key))
    conn.commit()


def test_recent_refusals_names_entries_refused_inside_the_window_only(
    store: sqlite3.Connection,
) -> None:
    _settled(store, "fresh", f"{ledger.QUOTA_REFUSED}: -32603 out", 10)
    _settled(store, "stale", f"{ledger.QUOTA_REFUSED}: -32603 out", 61)
    _settled(store, "other", "-32603 Internal error", 1)
    assert ledger.REFUSAL_WINDOW_S == 3600
    assert ledger.recent_refusals(store, clock.now()) == ("fresh",)


def test_recent_refusals_is_empty_when_nothing_refused(store: sqlite3.Connection) -> None:
    assert ledger.recent_refusals(store, clock.now()) == ()
