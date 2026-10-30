"""FR-145 (ticket 244): a delegation into a repository records what the repository became.

The agents are the real stubs (``tests/support/stub_acp_agent.py`` and ``stub_cli_agent.py``)
spawned through this interpreter, delegating into a temporary repository made with ``git init``
under ``tmp_path``. Nothing here touches this checkout's git.
"""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessed.acp import cli_runner, ledger, repo
from harnessed.acp import delegate as acp_delegate
from harnessed.acp.config import AgentSpec
from harnessed.cli import routed
from harnessed.errors import DelegationError, DirtyTreeRefused
from harnessed.events.bus import EventBus
from harnessed.loop import messages
from harnessed.memory import db

REPO_ROOT = Path(__file__).resolve().parents[1]
ACP_STUB = REPO_ROOT / "tests" / "support" / "stub_acp_agent.py"
CLI_STUB = REPO_ROOT / "tests" / "support" / "stub_cli_agent.py"
REPO_PATH = REPO_ROOT / "src" / "harnessed" / "acp" / "repo.py"
EVIDENCE = REPO_ROOT / "evidence" / "244"

pytestmark = [
    pytest.mark.usefixtures("no_child_coverage"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH"),
]

IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *IDENTITY, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def work(tmp_path: Path) -> Path:
    folder = tmp_path / "work"
    folder.mkdir()
    git(folder, "init", "-q")
    (folder / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(folder, "add", "seed.txt")
    git(folder, "commit", "-q", "-m", "seed")
    return folder


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "harness.db")
    db.migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def acp_spec(cwd: Path) -> AgentSpec:
    return AgentSpec(
        name="probe",
        command=sys.executable,
        args=(str(ACP_STUB),),
        cwd=str(cwd),
        timeout_s=10.0,
    )


def cli_spec(cwd: Path) -> AgentSpec:
    return AgentSpec(
        name="probe",
        command=sys.executable,
        args=(str(CLI_STUB), "--task", "{task}"),
        cwd=str(cwd),
        timeout_s=10.0,
        kind="cli",
        input="argv",
        allow_write=True,
    )


def key_for(spec: AgentSpec) -> str:
    return ledger.task_hash(agent=spec.name, cwd=str(spec.cwd), task="do it", acceptance="done")


def run_acp(spec: AgentSpec, store: sqlite3.Connection, tmp_path: Path) -> None:
    acp_delegate.delegate(spec, "do it", "done", home=tmp_path, store=store)


def recorded(store: sqlite3.Connection, key: str) -> dict[str, str | list[str]] | None:
    return messages._repository_for(store, key)  # the one reader the wire serves from


def note(name: str, text: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"{name}.txt").write_text(text.rstrip() + "\n", encoding="utf-8")


# ---- the stub agent in a temporary repository ---------------------------------------------------


def test_an_uncommitted_write_is_named_and_the_heads_are_equal(
    work: Path, store: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "sub/dir/new.txt")
    spec = acp_spec(work)
    run_acp(spec, store, tmp_path)
    found = recorded(store, key_for(spec))
    assert found is not None
    assert found["head_before"] == found["head_after"] == git(work, "rev-parse", "HEAD")
    assert found["changed_paths"] == ["sub/dir/new.txt"], "forward slashes on every host"
    assert found["changed_count"] == "1"
    assert found["committed"] == "false"
    assert found["started_dirty"] == "false"
    assert found["branch"] == git(work, "rev-parse", "--abbrev-ref", "HEAD")
    note("uncommitted", json.dumps(found, indent=2, sort_keys=True))


def test_a_commit_moves_the_head_and_the_stat_is_gits_own_line(
    work: Path, store: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "written.txt")
    monkeypatch.setenv("HARNESSED_STUB_COMMIT", "1")
    before = git(work, "rev-parse", "HEAD")
    spec = acp_spec(work)
    run_acp(spec, store, tmp_path)
    found = recorded(store, key_for(spec))
    assert found is not None
    assert found["head_before"] == before
    assert found["head_after"] == git(work, "rev-parse", "HEAD") != before
    assert found["head_before_short"] == git(work, "rev-parse", "--short", before)
    assert found["head_after_short"] == git(work, "rev-parse", "--short", "HEAD")
    assert found["changed_paths"] == ["written.txt"]
    assert found["committed"] == "true"
    assert found["diff_stat"] == git(work, "diff", "--shortstat", before, "HEAD")
    assert found["diff_stat"] != ""
    note("committed", json.dumps(found, indent=2, sort_keys=True))


def test_a_dirty_start_is_recorded_as_such(
    work: Path, store: sqlite3.Connection, tmp_path: Path
) -> None:
    (work / "stray.txt").write_text("x", encoding="utf-8")
    spec = acp_spec(work)
    acp_delegate.delegate(spec, "do it", "done", home=tmp_path, store=store, allow_dirty=True)
    found = recorded(store, key_for(spec))
    assert found is not None and found["started_dirty"] == "true"


def test_a_cli_entry_records_the_same_way(
    work: Path, store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "cli.txt")
    spec = cli_spec(work)
    cli_runner.run(spec, "do it", "done", store=store)
    found = recorded(store, key_for(spec))
    assert found is not None
    assert found["changed_paths"] == ["cli.txt"]


def test_changed_count_is_the_number_of_path_rows_written(
    work: Path, store: sqlite3.Connection
) -> None:
    record = repo.Record(
        head_before="a",
        head_after="b",
        head_before_short="a",
        head_after_short="b",
        branch="main",
        diff_stat="",
        paths=("x/y.txt", "z.txt"),
        started_dirty=False,
    )
    repo.write(store, "task_k", 1, record)
    ((count,),) = store.execute("SELECT changed_count FROM repo_records")
    assert count == store.execute("SELECT COUNT(*) FROM repo_paths").fetchone()[0] == 2
    assert [k for (k,) in store.execute("SELECT DISTINCT kind FROM repo_paths")] == ["changed"]


def test_an_unmoved_head_with_no_paths_serves_committed_false(
    work: Path, store: sqlite3.Connection
) -> None:
    record = repo.Record(
        head_before="a",
        head_after="a",
        head_before_short="a",
        head_after_short="a",
        branch="main",
        diff_stat="",
        paths=(),
        started_dirty=False,
    )
    repo.write(store, "task_none_changed", 1, record)
    found = recorded(store, "task_none_changed")
    assert found is not None
    assert found["committed"] == "false" and found["changed_count"] == "0"


# ---- no repository, no git ----------------------------------------------------------------------


def test_outside_a_repository_there_is_no_record(tmp_path: Path, store: sqlite3.Connection) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    spec = acp_spec(plain)
    run_acp(spec, store, tmp_path)
    assert recorded(store, key_for(spec)) is None
    assert store.execute("SELECT COUNT(*) FROM repo_records").fetchone() == (0,)


def test_without_git_on_path_there_is_no_record(
    work: Path, store: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert repo.start(str(work)) is None
    spec = cli_spec(work)
    cli_runner.run(spec, "do it", "done", store=store)
    assert recorded(store, key_for(spec)) is None


# ---- the session messages read -------------------------------------------------------------------


def seed_hop(conn: sqlite3.Connection, task_hash: str, tmp_path: Path) -> messages.Hop:
    from harnessed.acp import outputs

    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    conn.execute(
        "INSERT INTO decisions (run_id, idx, message_seq, hop, task, outcome, route, handler, "
        "state_truncated, latency_seconds, model, created_at, result, result_reason) "
        "VALUES (?, 0, 1, 0, 'do it', 'routed', 'probe', 'agent', 0, '0.1', 'jev', "
        "'2026-01-01T00:00:00Z', 'succeeded', NULL)",
        (bus.run_id,),
    )
    conn.commit()
    outputs.record_hop(
        conn,
        run_id=bus.run_id,
        message_seq=1,
        hop=0,
        task_hash=task_hash,
        summary="done",
        cwd=tmp_path,
        before={},
        produces=None,
        outputs_glob=None,
    )
    bus.publish("run.finished", {})
    (row,) = messages.for_session(conn, bus.run_id)
    (hop,) = row.hops
    return hop


def test_a_hop_carries_the_repository_or_null_with_a_reason(
    work: Path, store: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "w.txt")
    spec = acp_spec(work)
    run_acp(spec, store, tmp_path)
    hop = seed_hop(store, key_for(spec), tmp_path)
    assert hop.repository is not None and hop.repository_reason is None
    assert set(hop.repository) == {
        "head_before",
        "head_after",
        "head_before_short",
        "head_after_short",
        "branch",
        "diff_stat",
        "changed_count",
        "committed",
        "changed_paths",
        "dirty_paths",
        "started_dirty",
    }
    absent = seed_hop(store, "task_none", tmp_path)
    assert absent.repository is None
    assert absent.repository_reason is not None
    assert "not a git repository" in absent.repository_reason


# ---- a dirty tree is refused unless the operator says otherwise (ticket 245, FR-146) ------------


def dirty_work(work: Path) -> Path:
    (work / "stray.txt").write_text("x", encoding="utf-8")
    (work / "seed.txt").write_text("changed\n", encoding="utf-8")
    return work


def launched(work: Path) -> bool:
    return (work / "w.txt").exists()


def ledger_row(store: sqlite3.Connection, key: str) -> tuple[str, str, int]:
    status, outcome, attempts = store.execute(
        "SELECT status, outcome, "
        "json_extract(outcome, '$.attempts') FROM delegations WHERE task_hash = ?",
        (key,),
    ).fetchone()
    return status, outcome, int(attempts or 0)


@pytest.mark.parametrize("kind", ["acp", "cli"])
def test_a_dirty_tree_refuses_the_delegation_and_never_launches_the_agent(
    kind: str,
    work: Path,
    store: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "w.txt")
    dirty_work(work)
    spec = acp_spec(work) if kind == "acp" else cli_spec(work)
    bus = EventBus(conn=store)
    with pytest.raises(DirtyTreeRefused) as caught:
        if kind == "acp":
            acp_delegate.delegate(spec, "do it", "done", home=tmp_path, store=store, bus=bus)
        else:
            cli_runner.run(spec, "do it", "done", store=store, bus=bus)
    assert isinstance(caught.value, DelegationError)
    assert not launched(work), "the agent was not launched"
    key = key_for(spec)
    status, outcome, _ = ledger_row(store, key)
    assert status == "failed" and "dirty_tree" in outcome
    found = recorded(store, key)
    assert found is not None
    assert found["dirty_paths"] == ["seed.txt", "stray.txt"], "as git printed them"
    assert found["changed_paths"] == [] and found["started_dirty"] == "true"
    finished = [
        json.loads(payload)
        for (payload,) in store.execute(
            "SELECT payload FROM events WHERE name = 'acp.delegation.finished'"
        )
    ]
    assert [item["reason"] for item in finished] == ["dirty_tree"]
    assert routed.failure_reason(caught.value) == "dirty_tree"
    note(
        f"refused-{kind}",
        json.dumps(
            {"ledger": [status, outcome], "repository": found, "event": finished[0]},
            indent=2,
            sort_keys=True,
        ),
    )


def test_a_refused_delegation_does_not_use_up_an_attempt(
    work: Path, store: sqlite3.Connection, tmp_path: Path
) -> None:
    dirty_work(work)
    spec = acp_spec(work)
    for _ in range(3):
        with pytest.raises(DirtyTreeRefused):
            acp_delegate.delegate(spec, "do it", "done", home=tmp_path, store=store)
    acp_delegate.delegate(spec, "do it", "done", home=tmp_path, store=store, allow_dirty=True)
    assert recorded(store, key_for(spec)) is not None


@pytest.mark.parametrize("kind", ["acp", "cli"])
def test_allow_dirty_runs_the_delegation_and_the_record_says_started_dirty(
    kind: str,
    work: Path,
    store: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_WRITE", "w.txt")
    dirty_work(work)
    spec = acp_spec(work) if kind == "acp" else cli_spec(work)
    if kind == "acp":
        acp_delegate.delegate(spec, "do it", "done", home=tmp_path, store=store, allow_dirty=True)
    else:
        cli_runner.run(spec, "do it", "done", store=store, allow_dirty=True)
    assert launched(work)
    found = recorded(store, key_for(spec))
    assert found is not None and found["started_dirty"] == "true"
    assert found["dirty_paths"] == []
    note(f"allowed-{kind}", json.dumps(found, indent=2, sort_keys=True))


def test_a_clean_tree_and_a_non_repository_are_never_refused(
    work: Path, store: sqlite3.Connection, tmp_path: Path
) -> None:
    acp_delegate.delegate(acp_spec(work), "do it", "done", home=tmp_path, store=store)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "x.txt").write_text("x", encoding="utf-8")
    acp_delegate.delegate(acp_spec(plain), "do it", "done", home=tmp_path, store=store)


def test_the_hop_of_a_refused_delegation_serves_dirty_paths(
    work: Path, store: sqlite3.Connection, tmp_path: Path
) -> None:
    dirty_work(work)
    bus = EventBus(conn=store)
    bus.publish("run.started", {"provider": "fake"})
    store.execute(
        "INSERT INTO decisions (run_id, idx, message_seq, hop, task, outcome, route, handler, "
        "state_truncated, latency_seconds, model, created_at, result, result_reason) "
        "VALUES (?, 0, 1, 0, 'do it', 'routed', 'probe', 'agent', 0, '0.1', 'jev', "
        "'2026-01-01T00:00:00Z', 'failed', 'dirty_tree')",
        (bus.run_id,),
    )
    store.commit()
    with pytest.raises(DirtyTreeRefused):
        routed.run_agent(
            acp_spec(work),
            "do it",
            "done",
            home=tmp_path,
            bus=bus,
            store=store,
            message_seq=1,
            hop=0,
        )
    bus.publish("run.finished", {})
    (row,) = messages.for_session(store, bus.run_id)
    (hop,) = row.hops
    assert (hop.result, hop.reason) == ("failed", "dirty_tree")
    assert hop.repository is not None
    assert hop.repository["dirty_paths"] == ["seed.txt", "stray.txt"]


# ---- the read-only scan -------------------------------------------------------------------------


def test_repo_py_names_only_read_commands() -> None:
    literals = {
        node.value
        for node in ast.walk(ast.parse(REPO_PATH.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    forbidden = {"push", "merge", "commit", "add", "checkout", "reset", "deploy", "fetch", "pull"}
    assert literals & forbidden == set()
    assert {"rev-parse", "diff", "status"} <= literals
    text = REPO_PATH.read_text(encoding="utf-8")
    assert "shell=True" not in text
    note("argv-scan", "rev-parse, diff, status only; 0 push, merge, commit or deploy")


# ---- a recorded live delegation (ticket 246, FR-147) --------------------------------------------


def test_a_recorded_repository_delegation_carries_its_record() -> None:
    path = REPO_ROOT / "tests" / "fixtures" / "acp" / "repository-delegation-messages.json"
    messages_body = json.loads(path.read_text(encoding="utf-8"))["messages"]
    (record,) = (h["repository"] for m in messages_body for h in m["hops"])
    for key in ("head_before", "head_after", "changed_count", "diff_stat"):
        assert key in record
    assert len(record["head_before"]) == 40 and len(record["head_after"]) == 40
    assert record["changed_count"] == str(len(record["changed_paths"]))
    assert all("\\" not in p for p in record["changed_paths"] + record["dirty_paths"])
