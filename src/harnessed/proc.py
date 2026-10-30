"""Process identity and liveness: the leaf the run lease and the delegation reaper both stand on.

A pid alone names a process only until the kernel reuses it, so a lease that stored nothing else
would call a stranger its holder. :func:`identity` pairs the pid with the process's own start
time, which a reused pid never shares: ``GetProcessTimes`` on Windows, field 22 of
``/proc/<pid>/stat`` on Linux, ``proc_pidinfo`` over ``libproc`` on macOS. Any other POSIX system
offers no cheap, unforgeable start time, so
the identity there is the pid only, with ``started_at`` ``None`` and :attr:`Identity.reason` saying
why; that is a weaker claim, stated rather than guessed.

A leaf module: it imports nothing internal to ``harnessed``, so every package may reach it.
"""

from __future__ import annotations

import ctypes
import functools
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from harnessed.host import SeamLine

__all__ = ["Identity", "alive", "describe", "holds", "identity"]

# OpenProcess access right, GetExitCodeProcess's "still running" code, and the error a
# process we may not query leaves behind (winnt.h / minwinbase.h / winerror.h).
_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_STILL_ACTIVE: Final = 259
_ERROR_ACCESS_DENIED: Final = 5

# <sys/proc_info.h>: the flavor that returns a ``proc_bsdinfo``, and where libproc lives.
_PROC_PIDTBSDINFO: Final = 3
_LIBPROC: Final = "/usr/lib/libproc.dylib"

NO_START_TIME: Final = (
    "this platform offers no process start time, so the pid alone names the holder"
)


class _ProcBsdInfo(ctypes.Structure):
    """``proc_bsdinfo`` from ``<sys/proc_info.h>``, field by field (136 bytes)."""

    _fields_ = (
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    )


@dataclass(frozen=True, slots=True)
class Identity:
    """One process: its pid, its start time when the platform has one, and why not otherwise."""

    pid: int
    started_at: str | None
    reason: str | None = None


def identity(pid: int) -> Identity | None:
    """The identity of the live process ``pid``, or ``None`` when there is no such process."""
    if not alive(pid):
        return None
    if sys.platform == "win32":
        return Identity(pid, _windows_start(pid))
    if sys.platform.startswith("linux"):
        started = _linux_start(pid)
        if started is not None:
            return Identity(pid, started)
    if sys.platform == "darwin":
        started = _darwin_start(pid)
        if started is not None:
            return Identity(pid, started)
    return Identity(pid, None, NO_START_TIME)


def describe() -> SeamLine:
    """The process identity line of ``doctor``'s platform section, by the same branches."""
    if sys.platform == "win32":
        return SeamLine("process identity", "GetProcessTimes", None)
    if sys.platform.startswith("linux"):
        return SeamLine("process identity", "/proc/<pid>/stat", None)
    if sys.platform == "darwin":
        return SeamLine("process identity", "proc_pidinfo", None)
    return SeamLine("process identity", "pid only", NO_START_TIME)


def holds(recorded_pid: int, recorded_started_at: str | None) -> bool:
    """Whether the process a record names is still the one running under that pid."""
    current = identity(recorded_pid)
    if current is None:
        return False
    return recorded_started_at is None or current.started_at in (None, recorded_started_at)


def alive(pid: int | None) -> bool:
    """Whether ``pid`` still names a running process. ``None`` never was one."""
    if pid is None:
        return False
    if sys.platform == "win32":
        return _alive_on_windows(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _linux_start(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat`` (clock ticks since boot), read past the ``comm`` field."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # ``comm`` is parenthesised and may hold spaces and parentheses; the last ``)`` ends it.
    fields = text[text.rfind(")") + 2 :].split()
    # After ``comm`` the fields start at 3 (state), so field 22 is index 19.
    return fields[19] if len(fields) > 19 else None


def _bsdinfo_start(returned: int, info: _ProcBsdInfo) -> str | None:
    """The start time if ``proc_pidinfo`` filled the whole struct; any other return is ``None``."""
    if returned != ctypes.sizeof(info):
        return None
    return f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"


def _darwin_start(pid: int) -> str | None:
    """``proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ...)``'s start time, or ``None`` if unreadable."""
    try:
        libproc = ctypes.CDLL(_LIBPROC, use_errno=True)
        libproc.proc_pidinfo.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = _ProcBsdInfo()
        returned = libproc.proc_pidinfo(
            pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
    except (OSError, AttributeError):
        return None
    return _bsdinfo_start(returned, info)


@functools.cache
def _kernel32() -> Any:
    """kernel32 with the process calls typed, so a handle is never truncated to 32 bits."""
    if sys.platform != "win32":  # the check mypy narrows on, so ctypes' win32 names type-check
        raise OSError("a Windows-only call reached on " + sys.platform)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.GetProcessTimes.argtypes = (ctypes.c_void_p,) + (
        ctypes.POINTER(ctypes.c_ulonglong),
    ) * 4
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    return kernel32


def _open_process(pid: int) -> int | None:
    """A query-only handle to ``pid``, or ``None`` with the reason left in the last error."""
    handle: int | None = _kernel32().OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return handle or None


def _windows_start(pid: int) -> str | None:
    """The process's creation FILETIME from ``GetProcessTimes``, as a decimal string."""
    handle = _open_process(pid)
    if handle is None:
        return None
    kernel32 = _kernel32()
    try:
        created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        return str(created.value) if ok else None
    finally:
        kernel32.CloseHandle(handle)


def _alive_on_windows(pid: int) -> bool:
    """Ask the kernel, not ``tasklist``: at ~140 ms a spawn, the reaper's checks cost minutes."""
    if sys.platform != "win32":  # the check mypy narrows on, so ctypes' win32 names type-check
        raise OSError("a Windows-only call reached on " + sys.platform)
    handle = _open_process(pid)
    if handle is None:
        # A process we may not query still exists; any other failure means there is none.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    kernel32 = _kernel32()
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
