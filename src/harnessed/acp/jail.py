"""The platform jail a ``kind = "cli"`` child runs in, where there is one (FR-99, ticket 214).

:func:`detect` answers which kind this host offers, and the answer is what every label says:

- ``bwrap``: POSIX with ``bwrap`` on ``PATH``. :func:`wrap` puts the argv behind a bubblewrap
  filesystem jail: the whole tree read-only, a fresh ``/dev``, ``/proc`` and ``/tmp``, the working
  directory writable only when ``allow_write`` is true, a private pid namespace and
  ``--die-with-parent``, so a killed tree leaves no grandchild.
- ``job_object``: Windows. :class:`JobObject` is process containment only
  (``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, breakaway denied): a grandchild that escapes the tree
  kill dies with the job. It stops no file write; the snapshot check in ``cli_runner`` is still the
  only file-write control there.
- ``none``: anything else, macOS included (:data:`MACOS_REASON`, decision 93: ``sandbox-exec`` is
  deprecated, so no jail is chosen). Decision 62 holds exactly as it did: the directory is pinned,
  a write inside it is detected after the fact, and nothing is stopped.

**The network is not jailed, by design** (no ``--unshare-net``): an ``openrouter`` entry reaches
its metering proxy on loopback, and that is the proxy's whole point (FR-113).

Standard library only (``architecture.md`` import table: this module is a leaf of ``acp``).
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final

from harnessed.host import SeamLine

__all__ = [
    "BWRAP",
    "JOB_OBJECT",
    "MACOS_REASON",
    "NONE",
    "Jail",
    "JobObject",
    "describe",
    "detect",
    "reset",
    "wrap",
]

BWRAP: Final = "bwrap"
JOB_OBJECT: Final = "job_object"
NONE: Final = "none"

#: Why macOS answers ``none`` (decision 93): said through the same served fields as every host.
MACOS_REASON: Final = "no jail is chosen for macOS: sandbox-exec is deprecated by Apple"

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: Final = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x2000
_PROCESS_SET_QUOTA_TERMINATE: Final = 0x0100 | 0x0001


@dataclass(frozen=True, slots=True)
class Jail:
    """Which jail this host offers; ``kind`` is one of ``bwrap``, ``job_object``, ``none``."""

    kind: str
    #: Why a host that has ``bwrap`` answered ``none``: one line, unscrubbed (consumers scrub it).
    reason: str = ""


#: What ``bwrap`` must be able to do for :func:`wrap` to work: start, under a private pid namespace.
_SELF_TEST: Final = (
    "bwrap", "--ro-bind", "/", "/", "--unshare-pid", "--die-with-parent", "--", "true",
)  # fmt: skip
_SELF_TEST_TIMEOUT_S: Final = 5

_DETECTED: Jail | None = None


def reset() -> None:
    """Forget the cached self-test, so the next :func:`detect` measures again (tests)."""
    global _DETECTED
    _DETECTED = None


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _self_test() -> Jail:
    """Run ``bwrap`` once for real: a host can have it installed and refuse every launch."""
    try:
        done = subprocess.run(
            list(_SELF_TEST), capture_output=True, timeout=_SELF_TEST_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return Jail(NONE, reason="timed out")
    except OSError as exc:
        return Jail(NONE, reason=_first_line(str(exc)) or type(exc).__name__)
    if done.returncode == 0:
        return Jail(BWRAP)
    stderr = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else ""
    return Jail(NONE, reason=_first_line(stderr) or f"self-test exited {done.returncode}")


def detect() -> Jail:
    """The jail this host has, measured: ``bwrap`` only after its self-test succeeds (once).

    ``job_object`` on Windows; ``none`` with a reason where ``bwrap`` is absent or refuses.
    """
    global _DETECTED
    if sys.platform == "win32":
        return Jail(JOB_OBJECT)
    if sys.platform == "darwin":
        return Jail(NONE, reason=MACOS_REASON)
    if shutil.which("bwrap") is None:
        return Jail(NONE, reason="bwrap not on PATH")
    if _DETECTED is None:
        _DETECTED = _self_test()
    return _DETECTED


def describe() -> SeamLine:
    """The jail line of ``doctor``'s platform section: the kind, and why it is ``none``."""
    found = detect()
    if found.kind == JOB_OBJECT:
        return SeamLine("jail", "job_object (stops no write)", None)
    return SeamLine("jail", found.kind, found.reason or None)


def _posix(path: str) -> str:
    """``path`` with POSIX separators, as a Windows host hands it back."""
    return path.replace("\\", "/")


def wrap(argv: list[str], cwd: str, allow_write: bool) -> list[str]:
    """``argv`` behind a ``bwrap`` filesystem jail pinned to ``cwd``."""
    bind = "--bind" if allow_write else "--ro-bind"
    # `--tmpfs /tmp` hides anything under /tmp, so what must stay visible is bound after it: the
    # executable's directory first (read-only), then the cwd, so a cwd that is that directory,
    # or inside it, keeps its own mode.
    # The argv is a POSIX command line whatever host builds it, so every path is POSIX-separated.
    resolved = shutil.which(argv[0]) if argv else None
    exe_dir = str(PurePosixPath(_posix(resolved)).parent) if resolved else ""
    cwd = _posix(cwd)
    under_tmp = exe_dir == "/tmp" or exe_dir.startswith("/tmp/")
    exe_bind = ["--ro-bind", exe_dir, exe_dir] if under_tmp else []
    return [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        *exe_bind,
        bind, cwd, cwd,
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
        "--chdir", cwd,
        "--",
        *argv,
    ]  # fmt: skip


class JobObject:
    """A Windows Job Object that kills every process in it when it closes, breakaway denied."""

    def __init__(self) -> None:
        self._handle: int | None = None
        self._kernel32: Any = None
        if sys.platform != "win32":
            return
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [  # noqa: RUF012
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]  # noqa: RUF012

        class _Extended(ctypes.Structure):
            _fields_ = [  # noqa: RUF012
                ("BasicLimitInformation", _Basic),
                ("IoInfo", _Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]  # fmt: skip
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32 = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _Extended()
        # KILL_ON_JOB_CLOSE only: no BREAKAWAY_OK flag, so a child cannot leave the job.
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(handle)
            return
        self._handle = handle

    def assign(self, pid: int) -> bool:
        """Put process ``pid`` in the job; ``False`` when the job or the assignment failed."""
        if self._handle is None:
            return False
        kernel32 = self._kernel32
        process = kernel32.OpenProcess(_PROCESS_SET_QUOTA_TERMINATE, False, pid)
        if not process:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(self._handle, process))
        finally:
            kernel32.CloseHandle(process)

    def close(self) -> None:
        """Close the job's handle, which kills every process still in it."""
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
