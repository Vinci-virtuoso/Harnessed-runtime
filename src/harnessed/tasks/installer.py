"""The scheduler seam: the one place a platform picks how the OS tick is installed (decision 87,
MUST rule 42; FR-142, decision 79).

``harnessed schedule install`` puts **one** entry in the operating system's scheduler that runs
``harnessed task run --due`` every :data:`TICK_MINUTES`; the ledger decides what is due. A
:class:`SchedulerBackend` renders, installs, uninstalls and queries that entry, each with a
``dry_run`` that prints and writes nothing. :class:`TaskSchedulerBackend` (Windows, an XML task),
:class:`LaunchdBackend` (macOS, one plist), :class:`SystemdUserBackend` (Linux, a service and a
timer) and :class:`CrontabBackend` (Linux where no user systemd answers, one marked line, with the
``reason``) implement the same protocol (decision 92) and nothing above :func:`backend_for` changes.

This module is the only one that names ``schtasks``, ``crontab``, ``launchctl``, ``systemctl`` or
``loginctl``. The entry carries **no secret and no environment**: the scheduled process reads
``.env`` from its working directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol
from xml.sax.saxutils import escape

from harnessed import config
from harnessed.errors import ConfigError, StorageError
from harnessed.host import SeamLine

__all__ = [
    "ARGUMENTS",
    "CrontabBackend",
    "Entry",
    "LaunchdBackend",
    "Rendered",
    "SchedulerBackend",
    "SystemdUserBackend",
    "TaskSchedulerBackend",
    "TICK_MINUTES",
    "WHERE_CRONTAB",
    "WHERE_LAUNCHD",
    "WHERE_SYSTEMD",
    "WHERE_TASK_SCHEDULER",
    "WHERES",
    "backend_for",
    "check_home",
    "current_backend",
    "timeout_for",
]

TICK_MINUTES: Final = 15
ARGUMENTS: Final = "-m harnessed task run --due"
TASK_NAME: Final = "Harnessed\\due"
MARKER: Final = "# harnessed-due"

#: The four words ``schedule_installs."where"`` admits (migration 027); the vocabulary test reads
#: them from here. :class:`LaunchdBackend` and :class:`SystemdUserBackend` record under the last
#: two.
WHERE_TASK_SCHEDULER: Final = "task_scheduler"
WHERE_CRONTAB: Final = "crontab"
WHERE_LAUNCHD: Final = "launchd"
WHERE_SYSTEMD: Final = "systemd"
WHERES: Final = (WHERE_TASK_SCHEDULER, WHERE_CRONTAB, WHERE_LAUNCHD, WHERE_SYSTEMD)

#: ``(argv, stdin) -> (exit code, stdout)``: the one way a backend reaches the real tool, injected
#: by every test so none writes a real scheduler.
Runner = Callable[[list[str], str | None], tuple[int, str]]


@dataclass(frozen=True, slots=True)
class Entry:
    """What the entry runs: the installing interpreter, its working directory and the home."""

    python: str
    cwd: str
    home: str


@dataclass(frozen=True, slots=True)
class Rendered:
    """An entry as it would be written: the command that writes it, its body and its fingerprint."""

    entry_name: str
    command: str
    body: str
    python: str
    arguments: str
    working_dir: str
    fingerprint: str


class SchedulerBackend(Protocol):
    """One operating system's way of holding the tick (decision 87)."""

    where: str
    label: str
    #: Why this backend is a fallback (``None`` when it is the platform's own), shown by ``doctor``.
    reason: str | None

    def render(self, entry: Entry) -> Rendered: ...

    def install(self, entry: Entry, *, dry_run: bool) -> Rendered: ...

    def uninstall(self, *, dry_run: bool) -> bool: ...

    def query(self) -> str | None: ...

    def after_install(self) -> tuple[list[str], str | None]:
        """Lines to show once installed, and a warning when the entry may never fire."""
        ...


PROBE_SECONDS: Final = 5
_COMMAND_SECONDS: Final = 60
_SYSTEMD_PROBE: Final = ["systemctl", "--user", "show-environment"]


def timeout_for(argv: list[str]) -> int:
    """Seconds the real runner waits: the systemd probe must answer in 5, anything else in 60."""
    return PROBE_SECONDS if argv == _SYSTEMD_PROBE else _COMMAND_SECONDS


def _run(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
    try:
        done = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_for(argv),
        )
    except subprocess.TimeoutExpired as exc:
        raise StorageError(f"{argv[0]} did not answer in {timeout_for(argv)} s") from exc
    except OSError as exc:
        raise StorageError(f"could not run {argv[0]}: {exc}") from exc
    return done.returncode, done.stdout


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- Windows: Task Scheduler -----------------------------------------------------------------

_NS: Final = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
_DECLARATION: Final = re.compile(r"^\s*<\?xml[^>]*\?>")


def _fingerprint_xml(xml: str) -> str:
    """The parts of a task XML the record answers for, hashed: Task Scheduler rewrites the rest."""
    root = ET.fromstring(_DECLARATION.sub("", xml))
    found = []
    tags = (
        "Command",
        "Arguments",
        "WorkingDirectory",
        "Interval",
        "DisallowStartIfOnBatteries",
        "StopIfGoingOnBatteries",
        "WakeToRun",
    )
    for tag in tags:
        element = root.find(f".//{_NS}{tag}")
        if element is None:
            element = root.find(f".//{tag}")
        found.append("" if element is None or element.text is None else element.text.strip())
    return _sha("\n".join(found))


class TaskSchedulerBackend:
    """Windows: one XML Task Scheduler entry under the operator's own account."""

    where = "task_scheduler"
    label = "Task Scheduler"
    reason: str | None = None

    def __init__(self, runner: Runner = _run) -> None:
        self._run = runner

    def render(self, entry: Entry) -> Rendered:
        xml = (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            "  <Triggers>\n"
            "    <TimeTrigger>\n"
            f"      <Repetition><Interval>PT{TICK_MINUTES}M</Interval></Repetition>\n"
            "      <StartBoundary>2000-01-01T00:00:00</StartBoundary>\n"
            "      <Enabled>true</Enabled>\n"
            "    </TimeTrigger>\n"
            "  </Triggers>\n"
            '  <Principals><Principal id="Author">\n'
            "    <LogonType>InteractiveToken</LogonType>\n"
            "    <RunLevel>LeastPrivilege</RunLevel>\n"
            "  </Principal></Principals>\n"
            "  <Settings>\n"
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
            "    <StartWhenAvailable>true</StartWhenAvailable>\n"
            "    <WakeToRun>false</WakeToRun>\n"
            "    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>\n"
            "  </Settings>\n"
            '  <Actions Context="Author"><Exec>\n'
            f"    <Command>{escape(entry.python)}</Command>\n"
            f"    <Arguments>{ARGUMENTS}</Arguments>\n"
            f"    <WorkingDirectory>{escape(entry.cwd)}</WorkingDirectory>\n"
            "  </Exec></Actions>\n"
            "</Task>\n"
        )
        return Rendered(
            entry_name=TASK_NAME,
            command=f'schtasks /Create /TN "{TASK_NAME}" /XML <file> /F',
            body=xml,
            python=entry.python,
            arguments=ARGUMENTS,
            working_dir=entry.cwd,
            fingerprint=_fingerprint_xml(xml),
        )

    def install(self, entry: Entry, *, dry_run: bool) -> Rendered:
        rendered = self.render(entry)
        if dry_run:
            return rendered
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "harnessed-due.xml"
            path.write_bytes(rendered.body.encode("utf-16"))
            code, out = self._run(
                ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"], None
            )
        if code != 0:
            raise StorageError(f"schtasks /Create failed (exit {code}): {out.strip()}")
        return rendered

    def after_install(self) -> tuple[list[str], str | None]:
        """The ``Next Run Time`` and ``Power Management`` lines as ``schtasks`` prints them."""
        _, out = self._run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"], None)
        shown = [
            line.rstrip()
            for line in out.splitlines()
            if line.strip().startswith(("Next Run Time", "Power Management"))
        ]
        next_run = next((x for x in shown if x.startswith("Next Run Time")), None)
        if next_run is None or next_run.partition(":")[2].strip().upper() == "N/A":
            return shown, f"{TASK_NAME} has no next run time and may never fire."
        return shown, None

    def uninstall(self, *, dry_run: bool) -> bool:
        if dry_run:
            return True
        if self.query() is None:
            return False
        code, out = self._run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], None)
        if code != 0:
            raise StorageError(f"schtasks /Delete failed (exit {code}): {out.strip()}")
        return True

    def query(self) -> str | None:
        code, out = self._run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], None)
        if code != 0 or not out.strip():
            return None
        return _fingerprint_xml(out)


# ---- crontab: the reported fallback ---------------------------------------------------------


def _quoted(path: str) -> str:
    return "'" + str(PurePosixPath(path)).replace("'", "'\\''") + "'"


class CrontabBackend:
    """Linux and macOS: one crontab line, found again by its marker."""

    where = "crontab"
    label = "crontab"

    def __init__(self, runner: Runner = _run, *, reason: str | None = None) -> None:
        self._run = runner
        self.reason = reason

    def render(self, entry: Entry) -> Rendered:
        line = (
            f"*/{TICK_MINUTES} * * * * cd {_quoted(entry.cwd)} && {_quoted(entry.python)} "
            f"{ARGUMENTS} >> {_quoted(str(PurePosixPath(entry.home) / 'schedule.log'))} 2>&1 "
            f"{MARKER}"
        )
        return Rendered(
            entry_name="crontab " + MARKER,
            command="crontab -",
            body=line,
            python=entry.python,
            arguments=ARGUMENTS,
            working_dir=entry.cwd,
            fingerprint=_sha(line),
        )

    def after_install(self) -> tuple[list[str], str | None]:
        return [], None

    def _lines(self) -> list[str]:
        code, out = self._run(["crontab", "-l"], None)
        return out.splitlines() if code == 0 else []

    def _write(self, lines: list[str]) -> None:
        text = "".join(f"{line}\n" for line in lines)
        code, out = self._run(["crontab", "-"], text)
        if code != 0:
            raise StorageError(f"crontab - failed (exit {code}): {out.strip()}")

    def install(self, entry: Entry, *, dry_run: bool) -> Rendered:
        rendered = self.render(entry)
        if dry_run:
            return rendered
        kept = [line for line in self._lines() if not line.rstrip().endswith(MARKER)]
        self._write([*kept, rendered.body])
        return rendered

    def uninstall(self, *, dry_run: bool) -> bool:
        if dry_run:
            return True
        lines = self._lines()
        kept = [line for line in lines if not line.rstrip().endswith(MARKER)]
        if len(kept) == len(lines):
            return False
        self._write(kept)
        return True

    def query(self) -> str | None:
        for line in self._lines():
            if line.rstrip().endswith(MARKER):
                return _sha(line.rstrip())
        return None


# ---- macOS: launchd --------------------------------------------------------------------------

LAUNCHD_LABEL: Final = "harnessed.due"
_PLIST_ARGUMENTS: Final = ["-m", "harnessed", "task", "run", "--due"]


def _uid() -> int:
    getter = getattr(os, "getuid", None)
    return int(getter()) if getter is not None else 0


def _canonical(fields: dict[str, object]) -> str:
    return _sha(json.dumps(fields, sort_keys=True, separators=(",", ":")))


def _launchd_fields(data: dict[str, object]) -> str:
    return _canonical(
        {key: data.get(key) for key in ("ProgramArguments", "WorkingDirectory", "StartInterval")}
    )


class LaunchdBackend:
    """macOS: one LaunchAgent plist, bootstrapped into the operator's ``gui/<uid>`` domain."""

    where = "launchd"
    label = "launchd"
    reason: str | None = None

    def __init__(self, runner: Runner = _run) -> None:
        self._run = runner

    @property
    def _plist(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    @property
    def _service(self) -> str:
        return f"gui/{_uid()}/{LAUNCHD_LABEL}"

    def render(self, entry: Entry) -> Rendered:
        log = str(PurePosixPath(entry.home) / "schedule.log")
        data: dict[str, object] = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [str(PurePosixPath(entry.python)), *_PLIST_ARGUMENTS],
            "WorkingDirectory": str(PurePosixPath(entry.cwd)),
            "StartInterval": TICK_MINUTES * 60,
            "RunAtLoad": False,
            "ProcessType": "Background",
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
        body = plistlib.dumps(data, sort_keys=True).decode("utf-8")
        return Rendered(
            entry_name=str(self._plist),
            command=f"launchctl bootstrap gui/{_uid()} {self._plist}",
            body=body,
            python=entry.python,
            arguments=ARGUMENTS,
            working_dir=entry.cwd,
            fingerprint=_launchd_fields(data),
        )

    def install(self, entry: Entry, *, dry_run: bool) -> Rendered:
        rendered = self.render(entry)
        if dry_run:
            return rendered
        self._plist.parent.mkdir(parents=True, exist_ok=True)
        self._plist.write_text(rendered.body, encoding="utf-8")
        self._run(["launchctl", "bootout", self._service], None)  # "not loaded" is fine
        code, out = self._run(["launchctl", "bootstrap", f"gui/{_uid()}", str(self._plist)], None)
        if code != 0:
            raise StorageError(f"launchctl bootstrap failed (exit {code}): {out.strip()}")
        return rendered

    def uninstall(self, *, dry_run: bool) -> bool:
        if dry_run:
            return True
        if not self._plist.is_file():
            return False
        self._run(["launchctl", "bootout", self._service], None)
        self._plist.unlink()
        return True

    def query(self) -> str | None:
        try:
            data = plistlib.loads(self._plist.read_bytes())
        except FileNotFoundError:
            return None
        except (plistlib.InvalidFileException, ValueError, OSError):
            return ""
        return _launchd_fields(data)

    def after_install(self) -> tuple[list[str], str | None]:
        """The state and run-interval lines of one ``launchctl print``."""
        code, out = self._run(["launchctl", "print", self._service], None)
        if code != 0:
            return [], f"{self._service} is not loaded in launchd and may never fire."
        shown = [
            line.strip()
            for line in out.splitlines()
            if line.strip().startswith(("state =", "run interval ="))
        ]
        return shown, None


# ---- Linux: a systemd user timer --------------------------------------------------------------

SERVICE_FILE: Final = "harnessed-due.service"
TIMER_FILE: Final = "harnessed-due.timer"
ON_CALENDAR: Final = f"*:0/{TICK_MINUTES}"


def _systemd_quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _systemd_fields(exec_start: str, working_directory: str, on_calendar: str) -> str:
    return _canonical(
        {
            "ExecStart": exec_start,
            "WorkingDirectory": working_directory,
            "OnCalendar": on_calendar,
        }
    )


def _unit_value(text: str, key: str) -> str:
    prefix = f"{key}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


class SystemdUserBackend:
    """Linux with a user systemd: a oneshot service and a 15-minute timer; never enable-linger."""

    where = "systemd"
    label = "systemd user timer"
    reason: str | None = None

    def __init__(self, runner: Runner = _run) -> None:
        self._run = runner

    @property
    def _folder(self) -> Path:
        return config.xdg_config_home() / "systemd" / "user"

    def _units(self, entry: Entry) -> tuple[str, str, str]:
        exec_start = f"{_systemd_quote(str(PurePosixPath(entry.python)))} {ARGUMENTS}"
        cwd = str(PurePosixPath(entry.cwd))
        log = f"append:{PurePosixPath(entry.home) / 'schedule.log'}"
        service = (
            "[Unit]\nDescription=Harnessed: run due tasks\n\n"
            f"[Service]\nType=oneshot\nWorkingDirectory={cwd}\nExecStart={exec_start}\n"
            f"StandardOutput={log}\nStandardError={log}\n"
        )
        timer = (
            f"[Unit]\nDescription=Harnessed: every {TICK_MINUTES} minutes\n\n"
            f"[Timer]\nOnCalendar={ON_CALENDAR}\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )
        return service, timer, _systemd_fields(exec_start, cwd, ON_CALENDAR)

    def render(self, entry: Entry) -> Rendered:
        service, timer, fingerprint = self._units(entry)
        body = f"# {self._folder / SERVICE_FILE}\n{service}\n# {self._folder / TIMER_FILE}\n{timer}"
        return Rendered(
            entry_name=f"{SERVICE_FILE} + {TIMER_FILE}",
            command="systemctl --user enable --now harnessed-due.timer",
            body=body,
            python=entry.python,
            arguments=ARGUMENTS,
            working_dir=entry.cwd,
            fingerprint=fingerprint,
        )

    def _systemctl(self, *args: str) -> None:
        code, out = self._run(["systemctl", "--user", *args], None)
        if code != 0:
            raise StorageError(f"systemctl --user {args[0]} failed (exit {code}): {out.strip()}")

    def install(self, entry: Entry, *, dry_run: bool) -> Rendered:
        rendered = self.render(entry)
        if dry_run:
            return rendered
        service, timer, _ = self._units(entry)
        self._folder.mkdir(parents=True, exist_ok=True)
        (self._folder / SERVICE_FILE).write_text(service, encoding="utf-8")
        (self._folder / TIMER_FILE).write_text(timer, encoding="utf-8")
        self._systemctl("daemon-reload")
        self._systemctl("enable", "--now", TIMER_FILE)
        return rendered

    def uninstall(self, *, dry_run: bool) -> bool:
        if dry_run:
            return True
        files = [self._folder / SERVICE_FILE, self._folder / TIMER_FILE]
        if not any(f.is_file() for f in files):
            return False
        self._run(["systemctl", "--user", "disable", "--now", TIMER_FILE], None)
        for found in files:
            found.unlink(missing_ok=True)
        self._systemctl("daemon-reload")
        return True

    def query(self) -> str | None:
        try:
            service = (self._folder / SERVICE_FILE).read_text(encoding="utf-8")
            timer = (self._folder / TIMER_FILE).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return _systemd_fields(
            _unit_value(service, "ExecStart"),
            _unit_value(service, "WorkingDirectory"),
            _unit_value(timer, "OnCalendar"),
        )

    def after_install(self) -> tuple[list[str], str | None]:
        """The timer's next run, and a warning when linger is off (never turned on here)."""
        _, out = self._run(["systemctl", "--user", "list-timers", TIMER_FILE, "--no-pager"], None)
        shown = [line.rstrip() for line in out.splitlines() if line.strip()]
        _, lingered = self._run(["loginctl", "show-user", str(_uid()), "--property=Linger"], None)
        if lingered.strip() == "Linger=no":
            return shown, (
                "runs only while you are logged in; loginctl enable-linger keeps it running"
            )
        return shown, None


# ---- the one place a platform picks ----------------------------------------------------------


def backend_for(platform: str, *, runner: Runner = _run) -> SchedulerBackend:
    """The backend for ``platform`` (a ``sys.platform`` value): the one platform branch."""
    if platform == "win32":
        return TaskSchedulerBackend(runner)
    if platform == "darwin":
        return LaunchdBackend(runner)
    if platform == "linux":
        try:
            code, out = runner(list(_SYSTEMD_PROBE), None)
        except StorageError as exc:
            code, out = 1, str(exc)
        if code == 0:
            return SystemdUserBackend(runner)
        first = next((line.strip() for line in out.splitlines() if line.strip()), "no output")
        return CrontabBackend(runner, reason=f"no user systemd answered: {first}")
    return CrontabBackend(runner)


def describe(backend: SchedulerBackend | None = None) -> SeamLine:
    """The scheduler line of ``doctor``'s platform section: the backend, and why a fallback."""
    chosen = current_backend() if backend is None else backend
    return SeamLine("scheduler", type(chosen).__name__, chosen.reason)


def current_backend() -> SchedulerBackend:
    """The backend for the platform this process runs on."""
    return backend_for(sys.platform)


def check_home() -> str:
    """The current home, once it is shown the scheduled process would resolve the same one.

    That process starts with no shell variables, so it reads ``HARNESSED_HOME`` from ``.env`` in
    its working directory or falls back to the default; a ``HARNESSED_HOME`` set only in this
    shell would not reach it. Raises :class:`~harnessed.errors.ConfigError` naming the variable.
    """
    current = config.load().home.resolve()
    scheduled = config.dotenv_home().resolve()
    if current != scheduled:
        raise ConfigError(
            f"HARNESSED_HOME is {current} here but the scheduled process would resolve "
            f"{scheduled} (it reads .env in {Path.cwd()} and your shell's variables do not "
            f"reach it): put HARNESSED_HOME={current} in that .env, then install again."
        )
    return str(current)
