"""The repository record: what a repository became under a delegation (FR-145, decision 80).

A delegation into a real repository used to record what the agent said and nothing about what the
repository became. This module reads git twice, when the delegation starts and when it ends, and
stores the difference as rows (``repo_records`` and ``repo_paths``, migration 021), never as JSON.

**Read-only, and it says so.** Every call is ``git -C <cwd>`` through :func:`subprocess.run` with
:func:`~harnessed.config.child_env`, ``timeout=10``, captured output and no shell. The only
commands are ``rev-parse``, ``diff`` and ``status``; a test scans this file's literals and finds no
``push``, ``merge`` or ``commit``. A working directory outside a work tree, a repository with no
commit, no ``git`` on ``PATH`` or a git that does not answer in time gives ``None``: the record is
absent, never a half-filled one.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from typing import Final

from harnessed import clock
from harnessed.config import child_env
from harnessed.memory import db, redact

__all__ = ["Record", "Start", "finish", "refuse", "start", "write"]

_TIMEOUT_S: Final = 10

_INSERT: Final = (
    "INSERT OR REPLACE INTO repo_records (task_hash, attempt, head_before, head_after, "
    "head_before_short, head_after_short, branch, diff_stat, changed_count, started_dirty, "
    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_DELETE_PATHS: Final = "DELETE FROM repo_paths WHERE task_hash = ? AND attempt = ?"
_INSERT_PATH: Final = (
    "INSERT INTO repo_paths (task_hash, attempt, position, path, kind) VALUES (?, ?, ?, ?, ?)"
)
#: The ``repo_paths.kind`` of a path the tree already had changed when a delegation was refused.
KIND_DIRTY: Final = "dirty"


@dataclass(frozen=True, slots=True)
class Start:
    """The repository as a delegation found it."""

    cwd: str
    head: str
    head_short: str
    branch: str
    dirty: bool
    #: The paths ``git status --porcelain`` printed, as it printed them, in its order.
    dirty_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Record:
    """What the repository became: git's own words for it."""

    head_before: str
    head_after: str
    head_before_short: str
    head_after_short: str
    branch: str
    diff_stat: str
    paths: tuple[str, ...]
    started_dirty: bool
    #: Set only on a refusal: the paths that made the tree dirty (:data:`KIND_DIRTY` rows).
    dirty_paths: tuple[str, ...] = ()


def _git(cwd: str, *args: str) -> str | None:
    """One read-only git call, its stdout, or ``None`` for any failure."""
    try:
        done = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            env={**child_env(), "GIT_OPTIONAL_LOCKS": "0"},
            timeout=_TIMEOUT_S,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace")


def _untracked(porcelain: str) -> list[str]:
    return [entry[3:] for entry in porcelain.split("\0") if entry.startswith("?? ")]


def _dirty_paths(porcelain: str) -> tuple[str, ...]:
    """Every path of ``status --porcelain=v1 -z`` as git printed it, a rename's origin skipped."""
    found: list[str] = []
    entries = iter(porcelain.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        found.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            next(entries, None)
    return tuple(found)


def start(cwd: str) -> Start | None:
    """Read the head, its short form and the branch; ``None`` outside a repository."""
    head = _git(cwd, "rev-parse", "HEAD")
    short = _git(cwd, "rev-parse", "--short", "HEAD")
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if head is None or short is None or branch is None or status is None:
        return None
    return Start(
        cwd, head.strip(), short.strip(), branch.strip(), bool(status), _dirty_paths(status)
    )


def refuse(begun: Start) -> Record:
    """The record of a delegation refused for a dirty tree: the head unmoved, the dirty paths."""
    return Record(
        head_before=begun.head,
        head_after=begun.head,
        head_before_short=begun.head_short,
        head_after_short=begun.head_short,
        branch=begun.branch,
        diff_stat="",
        paths=(),
        started_dirty=True,
        dirty_paths=begun.dirty_paths,
    )


def finish(begun: Start | None) -> Record | None:
    """Read what the repository became since ``begun``; ``None`` when there is no record."""
    if begun is None:
        return None
    cwd = begun.cwd
    head = _git(cwd, "rev-parse", "HEAD")
    short = _git(cwd, "rev-parse", "--short", "HEAD")
    names = _git(cwd, "diff", "--name-only", "-z", begun.head)
    status = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    stat = _git(cwd, "diff", "--shortstat", begun.head)
    if head is None or short is None or names is None or status is None or stat is None:
        return None
    paths = dict.fromkeys([*(name for name in names.split("\0") if name), *_untracked(status)])
    return Record(
        head_before=begun.head,
        head_after=head.strip(),
        head_before_short=begun.head_short,
        head_after_short=short.strip(),
        branch=begun.branch,
        diff_stat=stat.strip(),
        paths=tuple(paths),
        started_dirty=begun.dirty,
    )


def write(conn: sqlite3.Connection, task_hash: str, attempt: int, record: Record) -> None:
    """Store ``record``; ``changed_count`` is the number of path rows written here."""
    writes: list[tuple[str, tuple[object, ...]]] = [
        (_DELETE_PATHS, (task_hash, attempt)),
        (
            _INSERT,
            (
                task_hash,
                attempt,
                record.head_before,
                record.head_after,
                record.head_before_short,
                record.head_after_short,
                redact.scrub(record.branch),
                redact.scrub(record.diff_stat),
                len(record.paths),
                int(record.started_dirty),
                clock.isoformat(clock.now()),
            ),
        ),
    ]
    writes.extend(
        (_INSERT_PATH, (task_hash, attempt, position, redact.scrub(path), "changed"))
        for position, path in enumerate(record.paths)
    )
    writes.extend(
        (_INSERT_PATH, (task_hash, attempt, position, redact.scrub(path), KIND_DIRTY))
        for position, path in enumerate(record.dirty_paths, start=len(record.paths))
    )
    db.write(conn, tuple(writes))
