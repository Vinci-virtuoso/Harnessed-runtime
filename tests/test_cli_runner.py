"""FR-99, decision 62 - a ``kind = "cli"`` agent runs non-interactively, bounded, and unconfined.

Every claim here is made against a real child process, ``tests/support/stub_cli_agent.py``,
spawned through this interpreter by the real ``acp/cli_runner.py``. It is a script, not a mock,
for the same reason ``test_delegation_ledger.py`` insists on a real ACP stub: a mocked spawn cannot
tell "the runner closed stdin at once" from "the client never started a process at all".

The jail is the one thing this file must not overstate. It proves the *detection* the architecture
promises (a write inside the pinned directory fails the delegation, naming the path) and it proves
the thing the architecture is equally explicit about: a write *outside* that directory succeeds,
on this platform, because the jail stops nothing (MUST rule 12 - the claim is a test).
"""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessed.acp import cli_runner, jail, ledger
from harnessed.acp import delegate as acp_delegate
from harnessed.acp.config import AgentSpec
from harnessed.config import SECRET_VARS
from harnessed.errors import DelegationError, DelegationTimeout
from harnessed.events.bus import EventBus
from harnessed.memory import db
from support.spies import RecordingObserver, watch_spawns

REPO_ROOT = Path(__file__).resolve().parents[1]
STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_cli_agent.py"
CLI_RUNNER_PATH = REPO_ROOT / "src" / "harnessed" / "acp" / "cli_runner.py"
EVIDENCE = REPO_ROOT / "evidence" / "158"

# Ticket 117: a stub started under `--cov` inherits coverage's variables and cold-starts too slowly.
pytestmark = pytest.mark.usefixtures("no_child_coverage")

READ_TIMEOUT = 10.0
SHORT_TIMEOUT = 1.0


# ---- the world every test delegates into --------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "harness.db")
    db.migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def spec_for(tmp_path: Path, *, input: str = "argv", **overrides: object) -> AgentSpec:
    """The stub agent, spawned through this interpreter, task on argv by default."""
    args = (str(STUB_PATH), "--task", "{task}") if input == "argv" else (str(STUB_PATH),)
    fields: dict[str, object] = {
        "name": "probe",
        "command": sys.executable,
        "args": args,
        "cwd": str(tmp_path),
        "timeout_s": READ_TIMEOUT,
        "kind": "cli",
        "input": input,
        "allow_write": False,
    }
    fields.update(overrides)
    return AgentSpec(**fields)  # type: ignore[arg-type]


def rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return list(conn.execute("SELECT task_hash, status, outcome FROM delegations"))


def write_evidence(name: str, text: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"{name}.txt").write_text(text.rstrip() + "\n", encoding="utf-8")


# ---- 1. EOF rather than a hang -------------------------------------------------------------------


def test_argv_input_still_closes_stdin_at_once_so_a_reader_gets_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub waits on stdin regardless of how the task arrived; the runner must not hang."""
    monkeypatch.setenv("HARNESSED_STUB_WAIT_STDIN", "1")
    spec = spec_for(tmp_path, input="argv", timeout_s=SHORT_TIMEOUT * 5)
    started = time.monotonic()
    result = cli_runner.run(spec, task="do it", acceptance="done")
    elapsed = time.monotonic() - started
    assert result.status == "succeeded"
    assert result.summary == "do it"
    # Generous, but a hang would run past the whole timeout; closing stdin at once finishes fast.
    assert elapsed < SHORT_TIMEOUT * 5
    write_evidence(
        f"eof-{sys.platform}",
        f"platform={sys.platform}: input='argv', stub blocked reading stdin, runner still closed "
        f"it at once. Finished in {elapsed:.3f}s against a {SHORT_TIMEOUT * 5}s timeout, never "
        f"waiting for the ceiling.",
    )


# ---- 2. stdin input, stdout reply, the 1 MiB cap ------------------------------------------------


def test_stdin_input_carries_the_task_and_stdout_is_the_reply(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, input="stdin")
    result = cli_runner.run(spec, task="the task text", acceptance="done")
    assert result.status == "succeeded"
    assert result.summary == "the task text"


def test_a_reply_over_the_cap_is_cut_and_the_cut_is_stated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_REPLY_BYTES", str(cli_runner.MAX_REPLY_BYTES + 1000))
    spec = spec_for(tmp_path, input="stdin")
    result = cli_runner.run(spec, task="ignored", acceptance="done")
    assert result.summary.startswith("x" * 100)
    assert "cut" in result.summary
    kept = result.summary.split("\n...[cut:")[0]
    assert len(kept) == cli_runner.MAX_REPLY_BYTES
    write_evidence(
        f"cap-{sys.platform}",
        f"platform={sys.platform}: stdout was {cli_runner.MAX_REPLY_BYTES + 1000} bytes; kept "
        f"exactly {len(kept)} of {cli_runner.MAX_REPLY_BYTES} and stated the cut: "
        f"{result.summary[-60:]!r}",
    )


def test_a_reply_at_or_under_the_cap_is_not_marked_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_REPLY_BYTES", "100")
    spec = spec_for(tmp_path, input="stdin")
    result = cli_runner.run(spec, task="ignored", acceptance="done")
    assert result.summary == "x" * 100
    assert "cut" not in result.summary


# ---- 3. the timeout kills the whole tree ---------------------------------------------------------


def test_a_stub_past_its_timeout_is_killed_and_the_delegation_settles_failed_timeout(
    tmp_path: Path, store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_SLEEP_S", str(SHORT_TIMEOUT * 5))
    spec = spec_for(tmp_path, input="stdin", timeout_s=SHORT_TIMEOUT)
    seen_pid: list[int] = []
    with pytest.raises(DelegationTimeout) as caught:
        cli_runner.run(spec, task="do it", acceptance="done", store=store, on_spawn=seen_pid.append)
    assert "timeout" in str(caught.value)
    assert len(seen_pid) == 1
    # The tree kill already ran inside cli_runner.run; the pid must be dead by the time it raises.
    assert not acp_delegate.alive(seen_pid[0])
    recorded = rows(store)
    assert len(recorded) == 1
    assert recorded[0][1] == ledger.FAILED
    assert "timeout" in str(recorded[0][2])
    write_evidence(
        f"timeout-{sys.platform}",
        f"platform={sys.platform}: stub slept {SHORT_TIMEOUT * 5}s past a {SHORT_TIMEOUT}s "
        f"timeout_s. delegate.kill_tree(pid={seen_pid[0]}) ran; the pid is dead afterwards "
        f"({not acp_delegate.alive(seen_pid[0])}). Ledger row settled failed, reason timeout.",
    )


def test_a_nonzero_exit_settles_failed_and_is_not_cached_as_a_success(
    tmp_path: Path, store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_EXIT_CODE", "3")
    spec = spec_for(tmp_path)
    with pytest.raises(DelegationError) as caught:
        cli_runner.run(spec, task="do it", acceptance="done", store=store)
    assert "exit_code" in str(caught.value)
    assert "stub failed on purpose" in str(caught.value)
    recorded = rows(store)
    assert len(recorded) == 1
    assert recorded[0][1] == ledger.FAILED


def test_a_file_removed_during_the_snapshot_walk_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "kept.txt").write_text("k", encoding="utf-8")
    (tmp_path / "gone.txt").write_text("g", encoding="utf-8")
    real_is_file = Path.is_file

    def flaky(self: Path) -> bool:
        if self.name == "gone.txt":
            raise FileNotFoundError(self)
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", flaky)
    assert list(cli_runner._snapshot(str(tmp_path))) == ["kept.txt"]


@pytest.fixture
def no_jail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision 62's case exactly as it was: a host with no jail, whatever this one has."""
    monkeypatch.setattr(jail, "detect", lambda: jail.Jail(jail.NONE))


@pytest.fixture(autouse=True)
def fresh_jail_cache() -> Iterator[None]:
    """Ticket 217: ``detect()`` caches its self-test for the process; each test starts clean."""
    jail.reset()
    yield
    jail.reset()


def self_test_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host whose ``bwrap`` is on ``PATH`` and whose self-test succeeds."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(jail, "_self_test", lambda: jail.Jail(jail.BWRAP))


needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None or sys.platform == "win32",
    reason="no bwrap on this host; ci: linux job",
)


def bwrap_usable() -> bool:
    """Whether bwrap can really start here: a container may refuse user namespaces."""
    if shutil.which("bwrap") is None or sys.platform == "win32":
        return False
    probe = subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True)
    return probe.returncode == 0


needs_working_bwrap = pytest.mark.skipif(
    not bwrap_usable(), reason="bwrap is absent or this container refuses user namespaces"
)


# ---- ticket 214: detect() ------------------------------------------------------------------------


def test_detect_answers_bwrap_on_posix_with_bwrap_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    self_test_passes(monkeypatch)
    assert jail.detect() == jail.Jail("bwrap")


class _Completed:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = stderr


def count_subprocesses(monkeypatch: pytest.MonkeyPatch, outcome: object) -> list[list[str]]:
    """Patch ``subprocess.run`` to answer ``outcome`` (or raise it) and record each argv."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> object:
        calls.append(argv)
        assert kwargs.get("timeout") == 5
        assert not kwargs.get("shell")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_detect_runs_the_self_test_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap")
    calls = count_subprocesses(monkeypatch, _Completed(0))
    assert jail.detect() == jail.Jail("bwrap")
    assert calls == [
        ["bwrap", "--ro-bind", "/", "/", "--unshare-pid", "--die-with-parent", "--", "true"]
    ]
    assert jail.detect() == jail.Jail("bwrap")
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (
            _Completed(1, b"\nbwrap: No permitted user namespace\nmore\n"),
            "bwrap: No permitted user namespace",
        ),
        (subprocess.TimeoutExpired("bwrap", 5), "timed out"),
        (OSError("exec format error"), "exec format error"),
    ],
)
def test_a_refused_self_test_answers_none_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, outcome: object, reason: str
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap")
    calls = count_subprocesses(monkeypatch, outcome)
    assert jail.detect() == jail.Jail("none", reason=reason)
    jail.detect()
    assert len(calls) == 1  # a refusal is cached too


def test_jail_reason_defaults_to_empty() -> None:
    assert jail.Jail("none").reason == ""


def test_detect_runs_no_subprocess_on_win32_or_without_bwrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = count_subprocesses(monkeypatch, _Completed(0))
    monkeypatch.setattr(sys, "platform", "win32")
    assert jail.detect() == jail.Jail("job_object")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert jail.detect() == jail.Jail("none", reason="bwrap not on PATH")
    assert calls == []


def test_jail_detect_on_darwin_answers_none_with_the_reason_before_looking_for_bwrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = count_subprocesses(monkeypatch, _Completed(0))
    monkeypatch.setattr(sys, "platform", "darwin")

    def no_lookup(name: str) -> str:
        raise AssertionError("darwin must not look for bwrap")

    monkeypatch.setattr(shutil, "which", no_lookup)
    assert jail.MACOS_REASON == "no jail is chosen for macOS: sandbox-exec is deprecated by Apple"
    assert jail.detect() == jail.Jail("none", reason=jail.MACOS_REASON)
    assert calls == []


def test_a_refused_jail_launches_without_the_wrapper_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        jail, "detect", lambda: jail.Jail(jail.NONE, reason="No permitted user namespace")
    )
    bus = EventBus()
    observer = RecordingObserver()
    bus.subscribe(observer)
    result = cli_runner.run(
        spec_for(tmp_path, input="stdin", allow_write=True), task="t", acceptance="d", bus=bus
    )
    assert result.status == "succeeded"
    (started,) = (e for e in observer.events if e.name == "acp.delegation.started")
    assert started.payload["jail"] == "none"
    assert started.payload["jail_reason"] == "No permitted user namespace"


def test_the_ledger_summary_carries_the_jail_reason(
    tmp_path: Path, store: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jail, "detect", lambda: jail.Jail(jail.NONE, reason="refused here"))
    cli_runner.run(spec_for(tmp_path, input="stdin"), task="t", acceptance="d", store=store)
    (outcome,) = (r[0] for r in store.execute("SELECT outcome FROM delegations"))
    assert '"jail_reason"' in outcome and "refused here" in outcome


def test_the_jail_reason_is_scrubbed_where_it_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-or-v1-" + "a1b2c3d4" * 6
    monkeypatch.setattr(jail, "detect", lambda: jail.Jail(jail.NONE, reason=f"leaked {secret}"))
    bus = EventBus()
    observer = RecordingObserver()
    bus.subscribe(observer)
    cli_runner.run(spec_for(tmp_path, input="stdin"), task="t", acceptance="d", bus=bus)
    (started,) = (e for e in observer.events if e.name == "acp.delegation.started")
    assert secret not in str(started.payload["jail_reason"])


def test_detect_answers_job_object_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert jail.detect().kind == "job_object"


def test_detect_answers_none_on_posix_without_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert jail.detect().kind == "none"


def test_jail_imports_the_standard_library_only() -> None:
    tree = ast.parse((REPO_ROOT / "src" / "harnessed" / "acp" / "jail.py").read_text("utf-8"))
    for node in ast.walk(tree):
        names = (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
        for name in names:
            # Ticket 322 (decision 95): the host leaf's ``SeamLine`` is the one internal import.
            assert name.split(".")[0] in sys.stdlib_module_names or name == "harnessed.host", name


def test_wrap_binds_the_working_directory_by_allow_write() -> None:
    writable = jail.wrap(["x"], "/work", True)
    readonly = jail.wrap(["x"], "/work", False)
    assert writable[writable.index("--bind") :][:3] == ["--bind", "/work", "/work"]
    assert "--bind" not in readonly
    assert ["--ro-bind", "/work", "/work"] in [readonly[i : i + 3] for i in range(len(readonly))]
    for flag in ("--unshare-pid", "--die-with-parent", "--new-session"):
        assert flag in readonly
    assert "--unshare-net" not in readonly  # the loopback metering proxy must stay reachable
    assert readonly[-1] == "x"


def test_wrap_binds_the_cwd_after_the_tmpfs() -> None:
    argv = jail.wrap(["x"], "/tmp/work", True)
    assert argv.index("--tmpfs") < argv.index("--bind")
    assert argv[argv.index("--bind") :][:3] == ["--bind", "/tmp/work", "/tmp/work"]


def test_wrap_binds_an_executable_under_tmp_read_only_after_the_tmpfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/tmp/tools/agent")
    argv = jail.wrap(["/tmp/tools/agent", "-x"], "/work", True)
    triples = [argv[i : i + 3] for i in range(len(argv))]
    assert ["--ro-bind", "/tmp/tools", "/tmp/tools"] in triples
    assert argv.index("--tmpfs") < argv.index("/tmp/tools")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/agent")
    assert "/tmp/tools" not in jail.wrap(["/usr/bin/agent"], "/work", True)


def test_wrap_builds_a_posix_argv_from_windows_style_host_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "\\tmp\\tools\\agent")
    argv = jail.wrap(["agent", "-x"], "\\tmp\\work\\dir", True)
    triples = [argv[i : i + 3] for i in range(len(argv))]
    assert ["--ro-bind", "/tmp/tools", "/tmp/tools"] in triples
    assert ["--bind", "/tmp/work/dir", "/tmp/work/dir"] in triples
    assert argv.index("--tmpfs") < argv.index("/tmp/tools")
    assert not any("\\" in part for part in argv[:-2])


# ---- ticket 214: the bwrap jail, against a real child -------------------------------------------


def outside_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Aim the stub's reporting write at a path outside its working directory."""
    target = REPO_ROOT / "escaped-214.txt"
    target.unlink(missing_ok=True)
    monkeypatch.setenv("HARNESSED_STUB_WRITE_OUTSIDE_REPORT", "1")
    monkeypatch.setenv("HARNESSED_STUB_OUTSIDE_TARGET", str(target))
    return target


@needs_working_bwrap
def test_under_bwrap_a_write_outside_the_directory_fails_and_the_delegation_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = outside_report(tmp_path, monkeypatch)
    cwd = tmp_path / "jail"
    cwd.mkdir()
    try:
        result = cli_runner.run(
            spec_for(cwd, input="stdin", allow_write=False), task="t", acceptance="done"
        )
        assert result.status == "succeeded"
        assert result.summary.startswith("error:"), result.summary
        assert not target.exists()
        assert list(cwd.iterdir()) == []
    finally:
        target.unlink(missing_ok=True)
    write_evidence("bwrap-outside-write", f"reply: {result.summary}")


@needs_working_bwrap
def test_under_bwrap_a_cwd_under_tmp_is_visible_and_writable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_WRITE_INSIDE", "inside.txt")
    cwd = Path(tempfile.mkdtemp(dir="/tmp", prefix="harnessed-217-"))
    try:
        result = cli_runner.run(
            spec_for(cwd, input="stdin", allow_write=True), task="t", acceptance="done"
        )
        assert result.status == "succeeded"
        assert (cwd / "inside.txt").read_text(encoding="utf-8") == "written by the stub"
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


@needs_working_bwrap
def test_under_bwrap_an_executable_under_tmp_still_runs(tmp_path: Path) -> None:
    tools = Path(tempfile.mkdtemp(dir="/tmp", prefix="harnessed-217-tools-"))
    try:
        shim = tools / "agent"
        shim.write_text(f'#!/bin/sh\nexec {sys.executable} {STUB_PATH} "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        result = cli_runner.run(
            spec_for(tmp_path, input="stdin", command=str(shim), args=()),
            task="t",
            acceptance="done",
        )
        assert result.status == "succeeded"
    finally:
        shutil.rmtree(tools, ignore_errors=True)


@needs_working_bwrap
def test_under_bwrap_allow_write_true_permits_inside_and_still_denies_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = outside_report(tmp_path, monkeypatch)
    monkeypatch.setenv("HARNESSED_STUB_WRITE_INSIDE", "inside.txt")
    cwd = tmp_path / "jail"
    cwd.mkdir()
    try:
        result = cli_runner.run(
            spec_for(cwd, input="stdin", allow_write=True), task="t", acceptance="done"
        )
        assert (cwd / "inside.txt").read_text(encoding="utf-8") == "written by the stub"
        assert result.summary.startswith("error:")
        assert not target.exists()
    finally:
        target.unlink(missing_ok=True)


@needs_working_bwrap
def test_under_bwrap_allow_write_false_makes_an_inside_write_fail_in_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_WRITE_INSIDE", "extra.txt")
    with pytest.raises(DelegationError) as caught:
        cli_runner.run(spec_for(tmp_path, input="stdin"), task="t", acceptance="done")
    assert "exit_code" in str(caught.value)
    assert not (tmp_path / "extra.txt").exists()


@needs_working_bwrap
def test_under_bwrap_no_grandchild_survives_the_timeout_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "grandchild"
    monkeypatch.setenv("HARNESSED_STUB_GRANDCHILD", str(pidfile))
    monkeypatch.setenv("HARNESSED_STUB_SLEEP_S", "30")
    spec = spec_for(tmp_path, input="stdin", allow_write=True, timeout_s=3.0)
    with pytest.raises(DelegationTimeout):
        cli_runner.run(spec, task="t", acceptance="done")
    beat = Path(str(pidfile) + ".beat")
    assert beat.exists(), "the stub never started its grandchild"
    time.sleep(0.5)  # anything still running has touched the beat inside this window
    first = beat.read_text(encoding="utf-8")
    time.sleep(0.5)
    assert beat.read_text(encoding="utf-8") == first, "a grandchild survived the kill"


@needs_working_bwrap
def test_an_openrouter_entry_under_bwrap_still_reaches_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The network is not jailed (no --unshare-net): the metering proxy is on loopback."""
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    seen: list[bytes] = []

    def accept() -> None:
        conn, _ = server.accept()
        seen.append(conn.recv(16))
        conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    code = (
        "import socket, os\n"
        "s = socket.create_connection(('127.0.0.1', int(os.environ['PROXY_PORT'])), 5)\n"
        "s.sendall(b'ping'); print('reached')\n"
    )
    spec = spec_for(tmp_path, input="stdin", args=("-c", code))
    try:
        result = cli_runner.run(
            spec, task="t", acceptance="done", proxy_env={"PROXY_PORT": str(port)}
        )
    finally:
        thread.join(timeout=5)
        server.close()
    assert result.summary.strip() == "reached"
    assert seen == [b"ping"]


@needs_working_bwrap
def test_the_started_event_and_the_ledger_summary_carry_the_jail_kind(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    bus = EventBus()
    observer = RecordingObserver()
    bus.subscribe(observer)
    cli_runner.run(
        spec_for(tmp_path, input="stdin"), task="t", acceptance="d", bus=bus, store=store
    )
    started = [e for e in observer.events if e.name == "acp.delegation.started"]
    assert [e.payload["jail"] for e in started] == ["bwrap"]
    (outcome,) = (r[0] for r in store.execute("SELECT outcome FROM delegations"))
    assert "bwrap" in outcome and '"jail"' in outcome


def test_the_started_event_says_none_where_the_host_has_no_jail(
    tmp_path: Path, no_jail: None
) -> None:
    bus = EventBus()
    observer = RecordingObserver()
    bus.subscribe(observer)
    cli_runner.run(spec_for(tmp_path, input="stdin"), task="t", acceptance="d", bus=bus)
    started = [e for e in observer.events if e.name == "acp.delegation.started"]
    assert [e.payload["jail"] for e in started] == ["none"]


@pytest.mark.skipif(sys.platform != "win32", reason="the Job Object is win32-only; ci: windows job")
def test_win32_job_object_leaves_no_grandchild_of_a_killed_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "grandchild"
    monkeypatch.setenv("HARNESSED_STUB_GRANDCHILD", str(pidfile))
    monkeypatch.setenv("HARNESSED_STUB_SLEEP_S", "30")
    spec = spec_for(tmp_path, input="stdin", allow_write=True, timeout_s=3.0)
    with pytest.raises(DelegationTimeout):
        cli_runner.run(spec, task="t", acceptance="done")
    beat = Path(str(pidfile) + ".beat")
    assert beat.exists()
    time.sleep(0.5)
    first = beat.read_text(encoding="utf-8")
    time.sleep(0.5)
    assert beat.read_text(encoding="utf-8") == first


# ---- 4. the jail: detects a write inside, and does not stop one outside -------------------------


def test_allow_write_false_and_a_write_inside_fails_naming_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_jail: None
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_WRITE_INSIDE", "extra.txt")
    spec = spec_for(tmp_path, input="stdin", allow_write=False)
    with pytest.raises(DelegationError) as caught:
        cli_runner.run(spec, task="do it", acceptance="done")
    assert "wrote_files" in str(caught.value)
    assert "extra.txt" in str(caught.value)
    assert (tmp_path / "extra.txt").read_text(encoding="utf-8") == "written by the stub"
    write_evidence(
        f"wrote-files-{sys.platform}",
        f"platform={sys.platform}: allow_write=false, stub wrote 'extra.txt' inside its pinned "
        f"directory. Delegation failed, reason wrote_files, path named: {caught.value}",
    )


def test_allow_write_false_does_not_stop_a_write_outside_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_jail: None
) -> None:
    """With no jail on the host, detection only (MUST rule 12): the write outside succeeds."""
    outside = tmp_path / "elsewhere" / "escaped.txt"
    outside.parent.mkdir()
    monkeypatch.setenv("HARNESSED_STUB_WRITE_OUTSIDE", str(outside))
    cwd = tmp_path / "jail"
    cwd.mkdir()
    spec = spec_for(cwd, input="stdin", allow_write=False)
    result = cli_runner.run(spec, task="do it", acceptance="done")
    assert result.status == "succeeded"
    assert outside.read_text(encoding="utf-8") == "written by the stub"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"outside-write-{sys.platform}.txt").write_text(
        f"platform={sys.platform}: a write outside the pinned directory succeeded and was not "
        f"stopped, only ever detectable had it landed inside. allow_write=false detects, never "
        f"prevents (decision 62, MUST rule 12).\n",
        encoding="utf-8",
    )


# ---- 5. no SECRET_VARS value reaches the child ---------------------------------------------------


def test_the_child_environment_holds_none_of_the_seeded_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert SECRET_VARS, "the frozen set must not be empty for this test to mean anything"
    seeded = {name: f"secret-value-for-{name}" for name in SECRET_VARS}
    for name, value in seeded.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HARNESSED_STUB_DUMP_ENV", "1")
    spec = spec_for(tmp_path, input="stdin")
    result = cli_runner.run(spec, task="ignored", acceptance="done")
    child_env = json.loads(result.summary)
    present = [name for name in seeded if name in child_env]
    assert present == []
    for value in seeded.values():
        assert value not in child_env.values()


# ---- 6. the delegations row: read, then write, before the work --------------------------------


def test_a_cli_delegation_writes_exactly_one_delegations_row(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    spec = spec_for(tmp_path, input="stdin")
    cli_runner.run(spec, task="do it", acceptance="done", store=store)
    recorded = rows(store)
    assert len(recorded) == 1
    assert recorded[0][1] == ledger.SUCCEEDED


def test_an_identical_cli_delegation_twice_spawns_exactly_one_process(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    spec = spec_for(tmp_path, input="stdin")
    with watch_spawns() as spy:
        first = cli_runner.run(spec, task="do it", acceptance="done", store=store)
        second = cli_runner.run(spec, task="do it", acceptance="done", store=store)
    matched = spy.matching(str(STUB_PATH))
    assert len(matched) == 1
    assert first.as_dict() == second.as_dict()


def test_a_succeeded_hash_publishes_cached_on_the_second_call(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    bus = EventBus()
    observer = RecordingObserver()
    bus.subscribe(observer)
    spec = spec_for(tmp_path, input="stdin")
    cli_runner.run(spec, task="do it", acceptance="done", store=store, bus=bus)
    assert "acp.delegation.cached" not in observer.names
    cli_runner.run(spec, task="do it", acceptance="done", store=store, bus=bus)
    published = observer.named("acp.delegation.cached")
    assert len(published) == 1
    assert published[0].payload["status"] == "succeeded"


def module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def function_named(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined")


def call_names(node: ast.AST) -> list[str]:
    found = [
        (child.lineno, f"{child.func.value.id}.{child.func.attr}")
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
    ]
    return [name for _, name in sorted(found)]


def test_the_read_comes_before_the_write_which_comes_before_the_spawn() -> None:
    """The pattern verbatim, as in ``acp/delegate.py``: read, persist, then the destructive work."""
    order = call_names(function_named(module_ast(CLI_RUNNER_PATH), "run"))
    assert order.index("ledger.get") < order.index("ledger.begin")
    assert order.index("ledger.begin") < order.index("subprocess.Popen")


def test_cli_runner_imports_only_what_the_import_table_allows() -> None:
    """``acp -> events, memory, config``, the leaves, and the rest of ``acp`` itself."""
    permitted = (
        "harnessed.acp",
        "harnessed.config",
        "harnessed.errors",
        "harnessed.events.bus",
        "harnessed.memory",  # ticket 217: the jail reason is scrubbed where published
    )
    for node in ast.walk(module_ast(CLI_RUNNER_PATH)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        base = node.module
        if base != "harnessed" and not base.startswith("harnessed."):
            continue
        assert any(base == p or base.startswith(p + ".") for p in permitted), base
