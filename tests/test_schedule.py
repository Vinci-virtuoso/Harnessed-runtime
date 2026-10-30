"""Ticket 242 (FR-142, decisions 79 and 87): due tasks run from the operating system's scheduler.

The ledger holds the rules; the OS holds one fixed tick that runs ``harnessed task run --due``.
Nothing here writes the real scheduler: every installer test is a dry run or an injected runner.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.support.task_world import CannedProvider, ScriptedDecider, answer, install

from harnessed import clock
from harnessed.cli import doctor_cmd, task_run
from harnessed.cli.app import main
from harnessed.errors import ConfigError, ParameterError, SurfaceNotFound
from harnessed.memory import db
from harnessed.tasks import installer, schedule, store

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "242"
NOON = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)  # a Thursday


def capture(name: str, text: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


@pytest.fixture
def home(clean_env: None, tmp_harnessed_home: Path, frozen_clock: Any) -> Path:
    return tmp_harnessed_home


@pytest.fixture
def offset(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(clock, "utc_offset_minutes", lambda: 60)
    return 60


def cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
    code = 0
    try:
        main()
    except SystemExit as exited:
        code = int(exited.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def with_conn[T](use: Callable[[Any], T]) -> T:
    conn = db.connect()
    try:
        return use(conn)
    finally:
        conn.close()


class FakeSystem:
    """The scheduler's own tool, injected: ``crontab`` and ``schtasks`` as a dict."""

    def __init__(self) -> None:
        self.crontab = ""
        self.task: str | None = None
        self.verbose = (
            "TaskName:                 \\Harnessed\\due\n"
            "Next Run Time:            2026-10-05 12:15:00\n"
            "Power Management:         No Start On Batteries\n"
        )
        self.calls: list[list[str]] = []
        self.systemd = True
        self.linger = "no"
        self.launchd_loaded = True

    def __call__(self, argv: list[str], stdin: str | None = None) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[:2] == ["crontab", "-l"]:
            return (0, self.crontab) if self.crontab else (1, "")
        if argv == ["crontab", "-"]:
            self.crontab = stdin or ""
            return 0, ""
        if argv[:2] == ["schtasks", "/Create"]:
            self.task = Path(argv[argv.index("/XML") + 1]).read_bytes().decode("utf-16")
            return 0, ""
        if argv[:2] == ["schtasks", "/Query"] and "/V" in argv:
            return 0, self.verbose
        if argv[:2] == ["schtasks", "/Query"]:
            return (0, self.task) if self.task else (1, "")
        if argv[:2] == ["schtasks", "/Delete"]:
            self.task = None
            return 0, ""
        if argv[0] == "systemctl":
            if argv[1:] == ["--user", "show-environment"]:
                return (0, "HOME=/h\n") if self.systemd else (1, "Failed to connect to bus\nmore\n")
            if "list-timers" in argv:
                return 0, "NEXT  LEFT\nThu 2026-01-01 12:15:00 UTC 15min harnessed-due.timer\n"
            return 0, ""
        if argv[0] == "loginctl":
            return 0, f"Linger={self.linger}\n"
        if argv[0] == "launchctl":
            if argv[1] == "print":
                if not self.launchd_loaded:
                    return 113, "Could not find service\n"
                return 0, "state = not running\n\trun interval = 900 seconds\n\tother = x\n"
            return 0, ""
        raise AssertionError(argv)


ENTRY = installer.Entry(python="C:\\venv\\Scripts\\python.exe", cwd="C:\\work\\repo", home="C:\\h")
POSIX_ENTRY = installer.Entry(python="/venv/bin/python", cwd="/work/repo", home="/work/.harnessed")


# ---- the rules ------------------------------------------------------------------------------


def test_add_every_stores_a_row_with_words_offset_and_next_due(home: Path, offset: int) -> None:
    row = with_conn(lambda conn: schedule.add(conn, every="6h"))
    assert row["schedule_id"].startswith("sch_") and len(row["schedule_id"]) == 4 + 26
    assert (row["rule_kind"], row["rule_value"], row["rule_words"]) == ("every", "6h", "Every 6 h")
    assert row["utc_offset_minutes"] == "60"
    assert row["next_due_at"] == clock.isoformat(NOON + timedelta(hours=6))
    assert row["last_run_at"] is None


def test_every_below_fifteen_minutes_or_malformed_is_refused(home: Path) -> None:
    for bad in ("10m", "0h", "soon", "6"):
        with pytest.raises(ParameterError):
            with_conn(lambda conn, bad=bad: schedule.add(conn, every=bad))
    with pytest.raises(ParameterError):
        with_conn(lambda conn: schedule.add(conn))
    with pytest.raises(ParameterError):
        with_conn(lambda conn: schedule.add(conn, every="1h", cron="0 7 * * *"))


def test_cron_is_matched_in_the_stored_fixed_offset_and_worded(home: Path, offset: int) -> None:
    row = with_conn(lambda conn: schedule.add(conn, cron="0 7 * * 1-5"))
    assert row["rule_words"] == "Weekdays at 07:00"
    # Thursday 13:00 local is past 07:00; the next weekday 07:00 (+01:00) is Friday 06:00Z.
    assert row["next_due_at"] == "2026-01-02T06:00:00Z"
    # Friday 06:00Z -> Monday: Saturday and Sunday are skipped
    after = datetime(2026, 1, 2, 6, 0, tzinfo=UTC)
    assert schedule.next_match("cron", "0 7 * * 1-5", after, 60) == datetime(
        2026, 1, 5, 6, 0, tzinfo=UTC
    )


def test_next_match_is_a_bounded_deterministic_scan() -> None:
    after = NOON
    assert schedule.next_match("cron", "*/20 * * * *", after, 0) == after + timedelta(minutes=20)
    assert (
        schedule.next_match("cron", "30 2 29 2 *", after, 0)
        == datetime(2028, 2, 29, 2, 30, tzinfo=UTC)
        or schedule.next_match("cron", "30 2 29 2 *", after, 0) is None
    )
    # a rule that never matches within 366 days is None, not a hang
    assert schedule.next_match("cron", "0 0 31 2 *", after, 0) is None
    assert schedule.next_match("cron", "0 0 1 1 *", after, 0) == datetime(
        2027, 1, 1, 0, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("bad", ["* * * *", "61 * * * *", "* 24 * * *", "a b c d e", "*/0 * * * *"])
def test_a_malformed_cron_is_refused(home: Path, bad: str) -> None:
    with pytest.raises(ParameterError):
        with_conn(lambda conn: schedule.add(conn, cron=bad))


def test_schedule_verbs_add_list_remove_with_json(
    home: Path, offset: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = cli(monkeypatch, capsys, "schedule", "add", "--every", "6h", "--json")
    assert code == 0, err
    added = json.loads(out)
    assert added["rule_words"] == "Every 6 h" and added["schedule_id"].startswith("sch_")
    code, out, _ = cli(monkeypatch, capsys, "schedule", "add", "--cron", "0 7 * * 1-5")
    assert code == 0 and "Weekdays at 07:00" in out
    code, out, _ = cli(monkeypatch, capsys, "schedule", "list", "--json")
    listed = json.loads(out)["schedules"]
    assert [s["rule_kind"] for s in listed] == ["every", "cron"]
    capture("schedule-list.json", out)
    code, out, _ = cli(monkeypatch, capsys, "schedule", "remove", added["schedule_id"], "--json")
    assert code == 0 and json.loads(out) == {"schedule_id": added["schedule_id"], "removed": "true"}
    code, out, _ = cli(monkeypatch, capsys, "schedule", "list", "--json")
    assert [s["rule_kind"] for s in json.loads(out)["schedules"]] == ["cron"]
    with pytest.raises(SurfaceNotFound):
        with_conn(lambda conn: schedule.remove(conn, added["schedule_id"]))


# ---- the installer: the seam, dry runs, idempotency ------------------------------------------


def test_backend_for_picks_by_the_injected_platform() -> None:
    assert isinstance(installer.backend_for("win32"), installer.TaskSchedulerBackend)
    assert isinstance(installer.backend_for("darwin"), installer.LaunchdBackend)
    assert isinstance(installer.backend_for("freebsd"), installer.CrontabBackend)
    assert installer.backend_for("win32").where == "task_scheduler"
    assert installer.backend_for("darwin").where == "launchd"
    up = installer.backend_for("linux", runner=FakeSystem())
    assert isinstance(up, installer.SystemdUserBackend)
    assert up.where == "systemd" and up.reason is None
    down = FakeSystem()
    down.systemd = False
    fallback = installer.backend_for("linux", runner=down)
    assert isinstance(fallback, installer.CrontabBackend) and fallback.where == "crontab"
    assert fallback.reason == "no user systemd answered: Failed to connect to bus"
    assert "launchd is v7" not in fallback.label


def test_the_default_runner_gives_the_systemd_probe_five_seconds() -> None:
    assert installer.timeout_for(["systemctl", "--user", "show-environment"]) == 5
    assert installer.timeout_for(["crontab", "-l"]) != 5


def test_the_windows_dry_run_prints_the_command_and_the_xml_and_writes_nothing() -> None:
    system = FakeSystem()
    backend = installer.backend_for("win32", runner=system)
    rendered = backend.install(ENTRY, dry_run=True)
    assert system.calls == [] and system.task is None
    assert rendered.command == 'schtasks /Create /TN "Harnessed\\due" /XML <file> /F'
    xml = rendered.body
    for needle in (
        "<Command>C:\\venv\\Scripts\\python.exe</Command>",
        "<Arguments>-m harnessed task run --due</Arguments>",
        "<WorkingDirectory>C:\\work\\repo</WorkingDirectory>",
        "<Interval>PT15M</Interval>",
        "<LogonType>InteractiveToken</LogonType>",
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "<StartWhenAvailable>true</StartWhenAvailable>",
        "<ExecutionTimeLimit>PT2H</ExecutionTimeLimit>",
    ):
        assert needle in xml, needle
    lowered = xml.lower()
    for forbidden in ("<environment", "password", "key", "secret", "token>sk-"):
        assert forbidden not in lowered.replace("interactivetoken", ""), forbidden
    capture("dry-run-windows.txt", rendered.command + "\n" + xml)


def test_the_posix_dry_run_prints_the_one_crontab_line_with_its_marker() -> None:
    system = FakeSystem()
    system.systemd = False
    backend = installer.backend_for("linux", runner=system)
    rendered = backend.install(POSIX_ENTRY, dry_run=True)
    assert system.crontab == ""
    assert rendered.body == (
        "*/15 * * * * cd '/work/repo' && '/venv/bin/python' -m harnessed task run --due "
        ">> '/work/.harnessed/schedule.log' 2>&1 # harnessed-due"
    )
    capture("dry-run-posix.txt", rendered.body)


def test_each_backend_is_asserted_on_every_host_for_install_uninstall_and_query(
    user_home: Path,
) -> None:
    for platform, entry, systemd in (
        ("win32", ENTRY, True),
        ("linux", POSIX_ENTRY, True),
        ("linux", POSIX_ENTRY, False),
        ("darwin", POSIX_ENTRY, True),
    ):
        system = FakeSystem()
        system.systemd = systemd
        backend = installer.backend_for(platform, runner=system)
        assert backend.query() is None
        rendered = backend.install(entry, dry_run=False)
        assert backend.query() == rendered.fingerprint
        assert backend.install(entry, dry_run=False).fingerprint == rendered.fingerprint
        assert backend.uninstall(dry_run=False) is True
        assert backend.query() is None
        assert backend.uninstall(dry_run=False) is False


def test_crontab_install_replaces_only_the_marker_line() -> None:
    system = FakeSystem()
    system.systemd = False
    system.crontab = "0 1 * * * backup\n*/15 * * * * old # harnessed-due\n"
    backend = installer.backend_for("linux", runner=system)
    backend.install(POSIX_ENTRY, dry_run=False)
    lines = system.crontab.splitlines()
    assert (
        lines[0] == "0 1 * * * backup" and len(lines) == 2 and lines[1].endswith("# harnessed-due")
    )
    backend.uninstall(dry_run=False)
    assert system.crontab == "0 1 * * * backup\n"


def test_install_and_uninstall_are_idempotent_and_recorded(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem()
    system.systemd = False
    monkeypatch.setattr(
        installer, "current_backend", lambda: installer.backend_for("linux", runner=system)
    )
    for _ in range(2):
        code, out, err = cli(monkeypatch, capsys, "schedule", "install")
        assert code == 0, err
        assert "# harnessed-due" in out
    assert system.crontab.count("# harnessed-due") == 1
    installs = with_conn(
        lambda c: c.execute('SELECT "where", uninstalled_at FROM schedule_installs').fetchall()
    )
    assert [tuple(r) for r in installs] == [("crontab", None)]
    for _ in range(2):
        code, out, err = cli(monkeypatch, capsys, "schedule", "uninstall")
        assert code == 0, err
    assert system.crontab == ""
    installs = with_conn(
        lambda c: c.execute("SELECT uninstalled_at FROM schedule_installs").fetchall()
    )
    assert len(installs) == 1 and installs[0][0] == clock.isoformat(NOON)


def test_the_cli_dry_run_writes_nothing_and_records_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem()
    monkeypatch.setattr(
        installer, "current_backend", lambda: installer.backend_for("win32", runner=system)
    )
    code, out, err = cli(monkeypatch, capsys, "schedule", "install", "--dry-run")
    assert code == 0, err
    assert 'schtasks /Create /TN "Harnessed\\due" /XML <file> /F' in out
    assert (
        system.calls == []
        and with_conn(lambda c: c.execute("SELECT * FROM schedule_installs").fetchall()) == []
    )


def test_install_refuses_naming_the_variable_when_the_scheduled_home_differs(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # no .env here: the scheduled process would resolve `.harnessed`
    system = FakeSystem()
    monkeypatch.setattr(
        installer, "current_backend", lambda: installer.backend_for("linux", runner=system)
    )
    with pytest.raises(ConfigError, match="HARNESSED_HOME"):
        installer.check_home()
    code, _, err = cli(monkeypatch, capsys, "schedule", "install")
    probe = ["systemctl", "--user", "show-environment"]
    assert code != 0 and "HARNESSED_HOME" in err and [c for c in system.calls if c != probe] == []


def test_the_home_check_passes_when_the_dotenv_names_the_same_home(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(f"HARNESSED_HOME={home}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Path(installer.check_home()) == home


# ---- task run --due ---------------------------------------------------------------------------


def seed_task(text: str, step: int = 0) -> str:
    clock.freeze(NOON + timedelta(seconds=step))
    row = with_conn(lambda c: store.queue(c, text, via="cli"))
    clock.freeze(NOON)
    return str(row["task_id"])


def test_due_runs_each_due_schedules_queued_tasks_in_queue_order_then_advances(
    home: Path, offset: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = seed_task("one", 0), seed_task("two", 1)
    sch = with_conn(lambda c: schedule.add(c, every="15m"))
    clock.freeze(NOON + timedelta(minutes=15))
    ran: list[tuple[str, str, str | None]] = []

    def fake_run(task_id: str, **kwargs: Any) -> dict[str, str | None]:
        ran.append((task_id, kwargs["started_by"], kwargs["schedule_id"]))
        with_conn(
            lambda c: c.execute("UPDATE tasks SET state = 'done' WHERE task_id = ?", (task_id,))
            and c.commit()
        )
        return {}

    result = task_run.run_due(run=fake_run)
    assert ran == [
        (first, "schedule", sch["schedule_id"]),
        (second, "schedule", sch["schedule_id"]),
    ]
    assert result.ran == (first, second)
    after = with_conn(lambda c: schedule.rows(c))[0]
    assert after["last_run_at"] == clock.isoformat(NOON + timedelta(minutes=15))
    assert after["next_due_at"] == clock.isoformat(NOON + timedelta(minutes=30))
    capture("due-run.json", json.dumps({"ran": list(result.ran), "after": after}, indent=2))


def test_due_with_nothing_due_runs_nothing(home: Path, offset: int) -> None:
    seed_task("one")
    with_conn(lambda c: schedule.add(c, every="6h"))
    ran: list[str] = []
    result = task_run.run_due(run=lambda task_id, **kw: ran.append(task_id) or {})
    assert ran == [] and result.ran == ()
    # the schedule did not advance: it was not due
    assert with_conn(lambda c: schedule.rows(c))[0]["last_run_at"] is None


def test_due_through_the_cli_exits_zero_with_nothing_due(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = cli(monkeypatch, capsys, "task", "run", "--due")
    assert code == 0, err


def test_a_real_task_started_by_a_schedule_is_stamped_with_it(
    home: Path, offset: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, ScriptedDecider([answer("groq_chat"), answer("done")]), CannedProvider())
    task_id = seed_task("tidy the notes")
    sch = with_conn(lambda c: schedule.add(c, every="15m"))
    clock.freeze(NOON + timedelta(minutes=15))
    result = task_run.run_due()
    assert result.ran == (task_id,)
    row = with_conn(lambda c: store.get(c, task_id))
    assert row is not None
    assert (row["state"], row["started_by"], row["schedule_id"]) == (
        "done",
        "schedule",
        sch["schedule_id"],
    )


def test_run_due_refuses_a_task_id_and_a_missing_one(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = cli(monkeypatch, capsys, "task", "run", "task_x", "--due")
    assert code != 0 and "--due" in err
    code, _, err = cli(monkeypatch, capsys, "task", "run")
    assert code != 0


# ---- the schedules read -----------------------------------------------------------------------


def test_the_listing_serves_installed_where_and_a_reason_beside_each_null(
    home: Path, offset: int
) -> None:
    system = FakeSystem()
    system.systemd = False
    sch = with_conn(lambda c: schedule.add(c, cron="0 7 * * 1-5"))
    body = with_conn(lambda c: schedule.listing_payload(schedule.listing(c)))
    only = body["schedules"][0]
    assert only["schedule_id"] == sch["schedule_id"]
    assert (only["installed"], only["where"]) == ("false", None)
    assert only["where_reason"] and only["last_run_at"] is None and only["last_run_at_reason"]
    assert only["next_due_at"] == "2026-01-02T06:00:00Z" and only["next_due_at_reason"] is None
    backend = installer.backend_for("linux", runner=system)
    rendered = backend.install(POSIX_ENTRY, dry_run=False)
    with_conn(lambda c: schedule.record_install(c, backend.where, rendered))
    only = with_conn(lambda c: schedule.listing_payload(schedule.listing(c)))["schedules"][0]
    assert (only["installed"], only["where"], only["where_reason"]) == ("true", "crontab", None)
    system.calls.clear()
    with_conn(lambda c: schedule.listing(c))
    assert system.calls == [], "the read never runs schtasks or crontab"


# ---- doctor -----------------------------------------------------------------------------------


def test_doctor_line_in_its_three_states(home: Path) -> None:
    system = FakeSystem()
    system.systemd = False
    backend = installer.backend_for("linux", runner=system)

    not_installed = doctor_cmd.schedule_line(backend=backend)
    assert not_installed.status == "ok" and "not installed" in not_installed.detail

    rendered = backend.install(POSIX_ENTRY, dry_run=False)
    with_conn(lambda c: schedule.record_install(c, backend.where, rendered))
    ok = doctor_cmd.schedule_line(backend=backend)
    assert ok.status == "ok" and "installed" in ok.detail and "never ran" in ok.detail

    system.crontab = ""
    missing = doctor_cmd.schedule_line(backend=backend)
    assert missing.status == "warn" and "missing from crontab" in missing.detail
    assert "harnessed schedule install" in missing.detail

    system.crontab = "*/15 * * * * something else # harnessed-due\n"
    changed = doctor_cmd.schedule_line(backend=backend)
    assert changed.status == "warn" and "changed" in changed.detail
    assert all(call[:2] != ["crontab", "-"] for call in system.calls[-2:])
    capture(
        "doctor-lines.txt",
        "\n".join(f"{c.status}: {c.detail}" for c in (not_installed, ok, missing, changed)),
    )


def test_doctor_names_the_macos_backend_and_never_fails_the_report(home: Path) -> None:
    backend = installer.backend_for("darwin", runner=FakeSystem())
    line = doctor_cmd.schedule_line(backend=backend)
    assert line.detail.startswith("launchd:") and "cron" not in line.detail
    assert line.status != "fail"


def test_doctor_names_the_fallback_backend_and_its_reason(home: Path) -> None:
    system = FakeSystem()
    system.systemd = False
    line = doctor_cmd.schedule_line(backend=installer.backend_for("linux", runner=system))
    assert "crontab (no user systemd answered: Failed to connect to bus)" in line.detail
    line = doctor_cmd.schedule_line(backend=installer.backend_for("linux", runner=FakeSystem()))
    assert line.detail.startswith("systemd user timer:")


# ---- ticket 243: one run at a time, a due task never runs twice, an orphan is closed ------------

EVIDENCE_243 = Path(__file__).resolve().parents[1] / "evidence" / "243"


def capture_243(name: str, text: str) -> None:
    EVIDENCE_243.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_243 / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def dead_pid() -> int:
    import subprocess

    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done.pid


def test_overlap_a_live_lease_holder_means_in_flight_and_nothing_runs(
    home: Path,
    offset: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from harnessed.tasks import lease

    seed_task("one")
    with_conn(lambda c: schedule.add(c, every="15m"))
    clock.freeze(NOON + timedelta(minutes=15))
    with_conn(lambda c: lease.acquire(c, "run_holder"))  # this live process holds it
    code, out, err = cli(monkeypatch, capsys, "task", "run", "--due")
    assert code == 0, err
    assert "in flight: waiting for the next tick" in out
    assert with_conn(lambda c: store.queued_ids(c)) != ()
    assert with_conn(lambda c: schedule.rows(c))[0]["last_run_at"] is None, "still due"
    capture_243("in-flight.txt", out)


def test_overlap_two_invocations_against_one_ledger_run_exactly_one_task(
    home: Path, offset: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, ScriptedDecider([answer("groq_chat"), answer("done")]), CannedProvider())
    task_id = seed_task("tidy the notes")
    with_conn(lambda c: schedule.add(c, every="15m"))
    clock.freeze(NOON + timedelta(minutes=15))
    first = task_run.run_due()
    second = task_run.run_due()
    assert first.ran == (task_id,)
    assert second.ran == () and not second.in_flight
    started = with_conn(
        lambda c: c.execute("SELECT COUNT(*) FROM events WHERE name = 'task.started'").fetchone()[0]
    )
    assert started == 1
    capture_243("two-invocations.json", json.dumps({"first": first.ran, "second": second.ran}))


def test_in_flight_due_refusal_names_the_running_task_and_what_to_do(
    home: Path,
    offset: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from harnessed.tasks import lease

    seed_task("queued one")
    running = seed_task("water the\nplants")
    with_conn(
        lambda c: store.begin_message(
            c, running, run_id="run_running_task", message_seq=1, author="task", first=True
        )
    )
    with_conn(lambda c: schedule.add(c, every="15m"))
    clock.freeze(NOON + timedelta(minutes=15))
    with_conn(lambda c: lease.acquire(c, "run_running_task"))
    sentence = (
        f"a task is running: {running} water the plants; "
        f"wait for it, or harnessed task cancel {running}"
    )
    code, out, err = cli(monkeypatch, capsys, "task", "run", "--due")
    assert code == 0, err
    assert out.strip() == sentence
    code, out, err = cli(monkeypatch, capsys, "task", "run", "--due", "--json")
    assert code == 0, err
    body = json.loads(out)
    assert (body["in_flight"], body["task_id"], body["text"], body["message"]) == (
        "true",
        running,
        "water the plants",
        sentence,
    )


def test_overlap_the_loser_of_the_lease_inside_a_tick_stops_in_flight(
    home: Path, offset: int
) -> None:
    from harnessed.errors import RunInFlight

    one, two = seed_task("one", 0), seed_task("two", 1)
    with_conn(lambda c: schedule.add(c, every="15m"))
    clock.freeze(NOON + timedelta(minutes=15))
    ran: list[str] = []

    def lose(task_id: str, **kwargs: Any) -> dict[str, str | None]:
        ran.append(task_id)
        raise RunInFlight("run_other")

    result = task_run.run_due(run=lose)
    assert ran == [one] and result.in_flight and result.ran == ()
    assert two in with_conn(lambda c: store.queued_ids(c))


def test_overlap_the_lease_is_looked_at_again_between_tasks(home: Path, offset: int) -> None:
    from harnessed.tasks import lease

    one, two = seed_task("one", 0), seed_task("two", 1)
    with_conn(lambda c: schedule.add(c, every="15m"))
    clock.freeze(NOON + timedelta(minutes=15))
    ran: list[str] = []

    def fake_run(task_id: str, **kwargs: Any) -> dict[str, str | None]:
        ran.append(task_id)
        with_conn(
            lambda c: c.execute("UPDATE tasks SET state = 'done' WHERE task_id = ?", (task_id,))
            and c.commit()
        )
        with_conn(lambda c: lease.acquire(c, "run_console"))  # someone else takes it meanwhile
        return {}

    result = task_run.run_due(run=fake_run)
    assert ran == [one] and result.in_flight and result.ran == (one,)
    assert two in with_conn(lambda c: store.queued_ids(c))


def test_overlap_a_reused_pid_reads_as_not_alive_and_a_missing_start_time_says_why(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from harnessed import proc
    from harnessed.tasks import lease

    me = proc.identity(os.getpid())
    assert me is not None
    if me.started_at is not None:
        assert not proc.holds(os.getpid(), "1")
        with_conn(lambda c: lease.acquire(c, "run_a"))
        with_conn(lambda c: c.execute("UPDATE run_lease SET started_at = '1'") and c.commit())
        assert with_conn(lease.holder) is None, "a reused pid is not the holder"
    monkeypatch.setattr(proc, "identity", lambda pid: proc.Identity(pid, None, proc.NO_START_TIME))
    with_conn(lambda c: c.execute("DELETE FROM run_lease") and c.commit())
    with_conn(lambda c: lease.acquire(c, "run_b"))
    held = with_conn(lease.holder)
    assert held is not None and held.started_at is None
    assert held.started_at_reason == proc.NO_START_TIME


def test_alive_macos_size_check_yields_none_with_the_reason_on_a_wrong_return() -> None:
    import ctypes

    from harnessed import proc

    info = proc._ProcBsdInfo()
    info.pbi_start_tvsec, info.pbi_start_tvusec = 1700000000, 42
    size = ctypes.sizeof(info)
    assert size == 136
    assert proc._bsdinfo_start(size, info) == "1700000000.000042"
    for wrong in (-1, 0, size - 1, size + 1):
        assert proc._bsdinfo_start(wrong, info) is None
    reasons = [proc.Identity(1, proc._bsdinfo_start(0, info), proc.NO_START_TIME).reason]
    assert reasons == [proc.NO_START_TIME]


@pytest.mark.skipif(sys.platform != "darwin", reason="ci: macos job only")
def test_alive_macos_a_child_pid_is_held_until_it_ends_and_a_forged_start_is_not() -> None:
    import subprocess

    from harnessed import proc

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        recorded = proc.identity(child.pid)
        assert recorded is not None and recorded.started_at is not None
        assert proc.holds(child.pid, recorded.started_at)
        assert not proc.holds(child.pid, "1.000000"), "a live pid with another start is a stranger"
    finally:
        child.kill()
        child.wait()
    assert not proc.holds(child.pid, recorded.started_at)


def test_overlap_deciding_what_is_due_among_a_thousand_tasks_is_fast(
    home: Path, offset: int
) -> None:
    import time

    def seed(conn: Any) -> None:
        for n in range(1000):
            store.queue(conn, f"task {n}", via="cli")
            schedule.add(conn, every="15m") if n < 1000 else None

    with_conn(seed)
    clock.freeze(NOON + timedelta(minutes=15))

    def decide(conn: Any) -> float:
        began = time.perf_counter()
        found = schedule.due(conn)
        queued = store.queued_ids(conn)
        took = time.perf_counter() - began
        assert len(found) == 1000 and len(queued) == 1000
        return took

    took = min(with_conn(decide) for _ in range(3))
    assert took < 0.2, f"{took:.3f}s"
    capture_243("due-decision-timing.txt", f"1000 tasks, 1000 schedules: {took * 1000:.1f} ms")


def seed_running(state: str, pid: int | None, started: str | None, session: str) -> str:
    task_id = seed_task(f"orphan {session}")

    def plant(conn: Any) -> None:
        conn.execute(
            "UPDATE tasks SET state = ?, session_id = ?, pid = ?, pid_started_at = ? "
            "WHERE task_id = ?",
            (state, session, pid, started, task_id),
        )
        conn.commit()

    with_conn(plant)
    return task_id


def test_orphan_sweep_closes_a_running_task_whose_process_died(home: Path, offset: int) -> None:
    gone = seed_running("running", dead_pid(), None, "run_dead")
    import os

    from harnessed import proc

    me = proc.identity(os.getpid())
    assert me is not None
    alive = seed_running("running", os.getpid(), me.started_at, "run_alive")
    closed = with_conn(task_run.sweep)
    assert closed == (gone,)
    row = with_conn(lambda c: store.get(c, gone))
    assert row is not None and (row["state"], row["stop_reason"]) == ("failed", "orphaned")
    assert row["finished_at"] is not None
    live = with_conn(lambda c: store.get(c, alive))
    assert live is not None and live["state"] == "running"
    events = with_conn(
        lambda c: c.execute(
            "SELECT run_id, seq, name FROM events WHERE name = 'task.finished'"
        ).fetchall()
    )
    assert len(events) == 1 and events[0][1] == 1
    assert (
        with_conn(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ?", (events[0][0],)
            ).fetchone()[0]
        )
        == 1
    ), "the first and only event of its own session"
    capture_243("orphan-sweep.json", json.dumps({"closed": list(closed), "state": row["state"]}))


def test_orphan_sweep_expires_the_pending_request_of_a_dead_waiter(home: Path, offset: int) -> None:
    from harnessed.acp import pending

    task_id = seed_running("needs_human", dead_pid(), None, "run_waiter")
    row = with_conn(
        lambda c: pending.record(
            c, session_id="run_waiter", agent="a", tool_kind="edit", description=None
        )
    )
    with_conn(
        lambda c: c.execute(
            "UPDATE tasks SET pending_permission_id = ?, pause_reason = 'permission' "
            "WHERE task_id = ?",
            (row.permission_id, task_id),
        )
        and c.commit()
    )
    assert with_conn(task_run.sweep) == (task_id,)
    expired = with_conn(lambda c: pending.get(c, row.permission_id))
    assert expired is not None and expired.state == "expired" and expired.rule == "ceiling"
    task = with_conn(lambda c: store.get(c, task_id))
    assert task is not None and (task["state"], task["stop_reason"]) == ("failed", "orphaned")


def test_orphan_sweep_runs_first_in_due_and_leaves_a_paused_uncertain_task_alone(
    home: Path, offset: int
) -> None:
    gone = seed_running("running", dead_pid(), None, "run_dead")
    paused = seed_running("needs_human", dead_pid(), None, "run_paused")  # no pending permission
    result = task_run.run_due()
    assert result.ran == ()
    after = with_conn(lambda c: store.get(c, gone))
    assert after is not None and after["stop_reason"] == "orphaned"
    still = with_conn(lambda c: store.get(c, paused))
    assert still is not None and still["state"] == "needs_human"


# ---- 262: the tick runs on battery, and install says when it next runs -----------------------

_SETTINGS_ORDER = (
    "MultipleInstancesPolicy",
    "DisallowStartIfOnBatteries",
    "StopIfGoingOnBatteries",
    "StartWhenAvailable",
    "WakeToRun",
    "ExecutionTimeLimit",
)


def test_the_windows_xml_allows_battery_in_the_schema_order_and_changes_the_fingerprint() -> None:
    import xml.etree.ElementTree as ET

    rendered = installer.backend_for("win32", runner=FakeSystem()).render(ENTRY)
    body = rendered.body.split("?>", 1)[1]
    root = ET.fromstring(body)
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    settings = root.find(f"{ns}Settings")
    assert settings is not None
    assert [c.tag.removeprefix(ns) for c in settings] == list(_SETTINGS_ORDER)
    values = {c.tag.removeprefix(ns): c.text for c in settings}
    assert values["DisallowStartIfOnBatteries"] == "false"
    assert values["StopIfGoingOnBatteries"] == "false"
    assert values["WakeToRun"] == "false"
    old = body.replace("    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n", "")
    old = old.replace("    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n", "")
    old = old.replace("    <WakeToRun>false</WakeToRun>\n", "")
    assert installer._fingerprint_xml(old) != rendered.fingerprint


def _windows_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], system: FakeSystem
) -> tuple[int, str, str]:
    monkeypatch.setattr(
        installer, "current_backend", lambda: installer.backend_for("win32", runner=system)
    )
    return cli(monkeypatch, capsys, "schedule", "install")


def test_windows_install_prints_the_next_run_and_power_lines_verbatim(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem()
    code, out, err = _windows_install(monkeypatch, capsys, system)
    assert code == 0, err
    assert "Next Run Time:            2026-10-05 12:15:00" in out
    assert "Power Management:         No Start On Batteries" in out
    queries = [c for c in system.calls if c[:2] == ["schtasks", "/Query"] and "/V" in c]
    assert queries == [["schtasks", "/Query", "/TN", "Harnessed\\due", "/V", "/FO", "LIST"]]


@pytest.mark.parametrize("verbose", ["Next Run Time:            N/A\n", "TaskName: x\n"])
def test_windows_install_warns_and_exits_zero_when_it_may_never_fire(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verbose: str,
) -> None:
    system = FakeSystem()
    system.verbose = verbose
    code, out, err = _windows_install(monkeypatch, capsys, system)
    assert code == 0, err
    assert "Harnessed\\due" in err + out and "never" in (err + out).lower()


def test_the_crontab_install_runs_no_verbose_query(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem()
    system.systemd = False
    monkeypatch.setattr(
        installer, "current_backend", lambda: installer.backend_for("linux", runner=system)
    )
    code, _, err = cli(monkeypatch, capsys, "schedule", "install")
    assert code == 0, err
    assert [c for c in system.calls if c[0] == "schtasks"] == []


# ---- ticket 320: launchd and a systemd user timer ----------------------------------------------

EVIDENCE_320 = Path(__file__).resolve().parents[1] / "evidence" / "320"


@pytest.fixture
def user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    fake = tmp_path / "userhome"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return fake


def test_launchd_renders_one_plist_with_decision_92s_keys(user_home: Path) -> None:
    import plistlib

    system = FakeSystem()
    backend = installer.backend_for("darwin", runner=system)
    rendered = backend.install(POSIX_ENTRY, dry_run=True)
    assert system.calls == [] and not (user_home / "Library").exists()
    data = plistlib.loads(rendered.body.encode("utf-8"))
    assert data == {
        "Label": "harnessed.due",
        "ProgramArguments": ["/venv/bin/python", "-m", "harnessed", "task", "run", "--due"],
        "WorkingDirectory": "/work/repo",
        "StartInterval": 900,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": "/work/.harnessed/schedule.log",
        "StandardErrorPath": "/work/.harnessed/schedule.log",
    }
    assert "EnvironmentVariables" not in rendered.body
    assert rendered.body == backend.render(POSIX_ENTRY).body
    capture_320("dry-run-launchd.txt", rendered.command + "\n" + rendered.body)


def test_launchd_install_boots_out_then_bootstraps_and_uninstall_deletes(user_home: Path) -> None:
    import os

    system = FakeSystem()
    backend = installer.backend_for("darwin", runner=system)
    rendered = backend.install(POSIX_ENTRY, dry_run=False)
    plist = user_home / "Library" / "LaunchAgents" / "harnessed.due.plist"
    uid = os.getuid() if hasattr(os, "getuid") else 0
    assert plist.is_file() and backend.query() == rendered.fingerprint
    assert system.calls == [
        ["launchctl", "bootout", f"gui/{uid}/harnessed.due"],
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
    ]
    # a change to a field Harnessed owns moves the fingerprint; one it does not own does not
    import plistlib

    data = plistlib.loads(plist.read_bytes())
    data["RunAtLoad"] = True
    plist.write_bytes(plistlib.dumps(data))
    assert backend.query() == rendered.fingerprint
    data["StartInterval"] = 60
    plist.write_bytes(plistlib.dumps(data))
    assert backend.query() != rendered.fingerprint
    assert backend.uninstall(dry_run=False) is True and not plist.exists()
    assert backend.uninstall(dry_run=False) is False and backend.query() is None


def test_launchd_after_install_prints_once_and_warns_when_not_found(user_home: Path) -> None:
    system = FakeSystem()
    backend = installer.backend_for("darwin", runner=system)
    shown, caution = backend.after_install()
    assert caution is None and any("state = not running" in x for x in shown)
    assert any("run interval = 900" in x for x in shown)
    assert [c[1] for c in system.calls] == ["print"]
    system.launchd_loaded = False
    shown, caution = backend.after_install()
    assert shown == [] and caution is not None and "harnessed.due" in caution


def test_systemd_renders_the_service_and_timer_under_xdg(
    user_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = FakeSystem()
    backend = installer.backend_for("linux", runner=system)
    rendered = backend.install(POSIX_ENTRY, dry_run=True)
    assert system.calls[-1] == ["systemctl", "--user", "show-environment"]
    assert not (user_home / ".config").exists()
    for needle in (
        "Type=oneshot",
        "WorkingDirectory=/work/repo",
        'ExecStart="/venv/bin/python" -m harnessed task run --due',
        "StandardOutput=append:/work/.harnessed/schedule.log",
        "StandardError=append:/work/.harnessed/schedule.log",
        "OnCalendar=*:0/15",
        "Persistent=true",
        "WantedBy=timers.target",
    ):
        assert needle in rendered.body, needle
    assert "Environment" not in rendered.body
    capture_320("dry-run-systemd.txt", rendered.command + "\n" + rendered.body)

    system.calls.clear()
    backend.install(POSIX_ENTRY, dry_run=False)
    folder = user_home / ".config" / "systemd" / "user"
    assert (folder / "harnessed-due.service").is_file() and (
        folder / "harnessed-due.timer"
    ).is_file()
    assert system.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "harnessed-due.timer"],
    ]
    elsewhere = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(elsewhere))
    installer.backend_for("linux", runner=system).install(POSIX_ENTRY, dry_run=False)
    assert (elsewhere / "systemd" / "user" / "harnessed-due.timer").is_file()


def test_systemd_query_fingerprints_owned_fields_and_uninstall_reverses(user_home: Path) -> None:
    system = FakeSystem()
    backend = installer.backend_for("linux", runner=system)
    assert backend.query() is None
    rendered = backend.install(POSIX_ENTRY, dry_run=False)
    folder = user_home / ".config" / "systemd" / "user"
    assert backend.query() == rendered.fingerprint
    timer = folder / "harnessed-due.timer"
    timer.write_text(timer.read_text().replace("Persistent=true", "Persistent=false"))
    assert backend.query() == rendered.fingerprint
    timer.write_text(timer.read_text().replace("*:0/15", "*:0/5"))
    assert backend.query() != rendered.fingerprint
    system.calls.clear()
    assert backend.uninstall(dry_run=False) is True
    assert system.calls == [
        ["systemctl", "--user", "disable", "--now", "harnessed-due.timer"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not list(folder.glob("harnessed-due.*"))
    assert backend.uninstall(dry_run=False) is False


def test_systemd_after_install_lists_the_timer_and_warns_on_linger_no_never_enabling_it(
    user_home: Path,
) -> None:
    system = FakeSystem()
    backend = installer.backend_for("linux", runner=system)
    shown, caution = backend.after_install()
    assert any("harnessed-due.timer" in x for x in shown)
    assert caution is not None and "loginctl enable-linger" in caution
    system.linger = "yes"
    assert backend.after_install()[1] is None
    assert all("enable-linger" not in " ".join(c) for c in system.calls)


def test_the_cli_records_where_as_launchd_or_systemd_and_no_secret_is_rendered(
    home: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRET-VALUE")
    for platform, word in (("darwin", "launchd"), ("linux", "systemd")):
        system = FakeSystem()
        monkeypatch.setattr(
            installer,
            "current_backend",
            lambda p=platform, s=system: installer.backend_for(p, runner=s),
        )
        code, out, err = cli(monkeypatch, capsys, "schedule", "install", "--dry-run")
        assert code == 0, err
        assert "sk-SECRET" not in out and "OPENAI" not in out
        code, out, err = cli(monkeypatch, capsys, "schedule", "install")
        assert code == 0, err
        assert "sk-SECRET" not in out
        newest = with_conn(schedule.last_install)
        assert newest["where"] == word
        code, _, err = cli(monkeypatch, capsys, "schedule", "uninstall")
        assert code == 0, err


def capture_320(name: str, text: str) -> None:
    EVIDENCE_320.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_320 / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
