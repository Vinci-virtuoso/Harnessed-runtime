"""No test reads the operator's setup (FR-159, decision 71, MUST rule 35).

The v5 walk found two tests green on a clean clone and red on the operator's PC because they read
his own ``agents.toml`` and probe ledger. This module closes the class rather than the instances:
a ``sys.addaudithook`` watches every ``open`` and ``sqlite3.connect`` while a test that is not
marked ``live`` runs, and fails it with :class:`IsolationBreach` when the path is inside

- the repository's ``.harnessed/``, ``agents.toml`` or ``.env``,
- ``Path.home() / ".harnessed"``, or
- the ``HARNESSED_HOME`` the session started with (captured at import, before ``clean_env``
  clears it).

Paths compare through ``os.path.normcase(os.path.realpath())`` and ``os.path.commonpath``, so
drive-letter case, ``\\`` against ``/`` and a symlinked temporary directory cannot hide a breach.
A test that spawns the harness (``-m harnessed`` or the ``harnessed`` script) must also give it a
``cwd`` outside the repository and a ``HARNESSED_HOME`` outside the protected paths.

An audit hook cannot be removed, so it is installed once and consults ``_current``: the node id of
the armed test, or ``None`` between tests.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

__all__ = ["IsolationBreach", "isolation_guard", "protected_paths"]

REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``HARNESSED_HOME`` as the session started, before any fixture could clear or repoint it.
SESSION_HARNESSED_HOME = os.environ.get("HARNESSED_HOME")


class IsolationBreach(RuntimeError):
    """A test touched the operator's setup. Outside ``HarnessError``, so nothing renders it away."""


def _canonical(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(path))


def protected_paths() -> tuple[str, ...]:
    """The canonical spelling of every path a non-live test may not open."""
    roots: list[str | os.PathLike[str]] = [
        REPO_ROOT / ".harnessed",
        REPO_ROOT / "agents.toml",
        REPO_ROOT / ".env",
    ]
    with contextlib.suppress(RuntimeError):  # no resolvable home directory
        roots.append(Path.home() / ".harnessed")
    if SESSION_HARNESSED_HOME:
        roots.append(SESSION_HARNESSED_HOME)
    return tuple(dict.fromkeys(_canonical(root) for root in roots))


_PROTECTED = protected_paths()
_REPO = _canonical(REPO_ROOT)

#: The node id of the test the guard is armed for, or ``None``.
_current: str | None = None


def _inside(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:  # different drives, or an absolute path against a relative one
        return False


def _breached(path: object) -> str | None:
    """The canonical path when it falls inside a protected one, else ``None``."""
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str | os.PathLike) or path == "":
        return None
    try:
        candidate = _canonical(path)
    except (OSError, ValueError):
        return None
    return candidate if any(_inside(candidate, root) for root in _PROTECTED) else None


def _split_command_line(line: str) -> list[str]:
    """The argv a Windows command line spells: the inverse of ``subprocess.list2cmdline``.

    On Windows ``Popen`` audits the command line already joined into one string, so the guard has
    to split it again. Backslashes are literal unless a double quote follows them, and a doubled
    run of them before a quote halves.
    """
    argv: list[str] = []
    current: list[str] = []
    started = quoted = False
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\":
            run = len(line[i:]) - len(line[i:].lstrip("\\"))
            i += run
            if line[i : i + 1] == '"':
                current.append("\\" * (run // 2))
                if run % 2:
                    current.append('"')
                    i += 1
            else:
                current.append("\\" * run)
            started = True
            continue
        if char == '"':
            quoted, started = not quoted, True
        elif char in " \t" and not quoted:
            if started:
                argv.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
        i += 1
    if started:
        argv.append("".join(current))
    return argv


def _runs_harness(args: object) -> bool:
    if isinstance(args, list | tuple):
        argv = [os.fsdecode(a) for a in args]
    else:
        argv = _split_command_line(str(args))
    if not argv:
        return False
    if PureWindowsPath(argv[0]).stem.casefold() == "harnessed":  # reads either separator
        return True
    return any(flag == "-m" and nxt == "harnessed" for flag, nxt in itertools.pairwise(argv))


def _env_value(env: object, name: str) -> str | None:
    if isinstance(env, Mapping):
        value = env.get(name)
        return None if value is None else os.fsdecode(value)
    for item in env if isinstance(env, list | tuple) else ():
        text = os.fsdecode(item)
        if text.startswith(name + "="):
            return text[len(name) + 1 :]
    return None


def _check_spawn(test: str, args: object, cwd: object, env: object) -> None:
    if not _runs_harness(args):
        return
    if cwd is None or _inside(_canonical(os.fspath(cwd)), _REPO):  # type: ignore[arg-type]
        raise IsolationBreach(
            f"{test} spawns the harness with cwd {cwd!r}: give it a cwd outside the repository "
            f"(monkeypatch.chdir(tmp_path) or cwd=tmp_path), or it reads the operator's files."
        )
    home = None if env is None else _env_value(env, "HARNESSED_HOME")
    if not home or _breached(home) is not None:
        raise IsolationBreach(
            f"{test} spawns the harness with HARNESSED_HOME {home!r}: give it its own home "
            f"(tmp_harnessed_home), outside the protected paths."
        )


def _hook(event: str, args: tuple[Any, ...]) -> None:
    test = _current
    if test is None:
        return
    if event == "open" or event == "sqlite3.connect":
        hit = _breached(args[0])
        if hit is not None:
            raise IsolationBreach(
                f"{test} opened {hit}, which is the operator's setup. Give the test its own "
                f"home and cwd (tmp_harnessed_home, monkeypatch.chdir(tmp_path)); never exempt it."
            )
    elif event == "subprocess.Popen":
        _check_spawn(test, args[1], args[2], args[3])


sys.addaudithook(_hook)


@pytest.fixture(autouse=True)
def isolation_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Arm the guard for every test that is not marked ``live``."""
    global _current
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    _current = request.node.nodeid
    try:
        yield
    finally:
        _current = None
