"""``kind = "cli"``: a bounded, unconfined subprocess, never a coroutine (FR-99, decision 62).

Every ``acp`` delegation speaks the Agent Client Protocol over stdio and is driven from
:mod:`~harnessed.acp.delegate` on the background asyncio loop. A ``cli`` entry is the opposite
shape on purpose: an operator's own script or a harness with no ACP adapter, run once, synchronous,
with no handshake and no session. The task goes on argv (``spec.input == "argv"``, replacing a
``{task}`` token in ``spec.args``) or on stdin; stdin is otherwise closed at once, so a reader gets
EOF rather than a hang. stdout is the reply, capped at :data:`MAX_REPLY_BYTES` with the cut stated.
A run past ``spec.timeout_s`` ends in :func:`~harnessed.acp.delegate.kill_tree` - the same tree
kill an ACP delegation uses, reused unchanged - and a :class:`~harnessed.errors.DelegationTimeout`.
It writes the same ``delegations`` row an ACP delegation writes, through the same
:mod:`~harnessed.acp.ledger` functions and the same read-then-write-before-the-work pattern
(``state-and-idempotency.md``, MUST rule 6).

**The jail, where the platform has one, and it says which** (:mod:`~harnessed.acp.jail`, ticket
214): ``bwrap`` on a POSIX host that has it makes the filesystem read-only outside the working
directory and denies a write inside it unless ``allow_write``; a Windows Job Object kills a
grandchild that escapes the tree kill; elsewhere there is no jail. The kind is published on
``acp.delegation.started`` as ``jail`` and recorded in the ``delegations`` summary. The network is
not jailed (an ``openrouter`` entry's loopback proxy must stay reachable). The detection below runs
on every platform regardless.

**Detection, the part that needs no platform.** The working directory is pinned, and with
``spec.allow_write == False`` this module lists it before and after (path, size, mtime of every
file) and fails the delegation with reason ``wrote_files``, naming the first changed path, when
anything inside it changed. That **detects** a write inside the directory after the fact and
**stops none** - the child has already run to completion by the time the listing is compared. It
neither stops nor sees a write outside the directory where the jail kind is not ``bwrap``, which is
why the Models page calls such an entry "Unconfined" rather than sandboxed (MUST rule 12: the
claim is a test, not a docstring).

**The child never sees a value in** :data:`~harnessed.config.SECRET_VARS` (MUST rule 33). Its
environment is the parent's with every one of those names removed, never a value substituted for
another kind of agent's proxy - that arrives only for a ``billing = "openrouter"`` entry, as
``proxy_env``: its metering proxy's loopback URL and a placeholder key (FR-113).
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from harnessed.acp import jail as jail_mod
from harnessed.acp import ledger, repo
from harnessed.acp.config import AgentSpec
from harnessed.acp.delegate import (
    Delegation,
    is_batch,
    kill_tree,
    process_group,
    refuse_dirty,
    spawn_batch,
)
from harnessed.config import child_env
from harnessed.errors import DelegationError, DelegationTimeout
from harnessed.events.bus import EventBus
from harnessed.memory import redact

__all__ = ["MAX_REPLY_BYTES", "run"]

_STARTED: Final = "acp.delegation.started"
_FINISHED: Final = "acp.delegation.finished"
_CACHED: Final = "acp.delegation.cached"

#: The stdout cap this module enforces (FR-99). One mebibyte, not one million bytes.
MAX_REPLY_BYTES: Final = 1 << 20

#: One file's identity for the write-jail snapshot: enough to catch a truncate-and-rewrite that
#: happens to land on the same size, because mtime moves even when size does not.
_Snapshot = dict[str, tuple[int, float]]


def run(
    spec: AgentSpec,
    task: str,
    acceptance: str,
    *,
    bus: EventBus | None = None,
    store: sqlite3.Connection | None = None,
    on_spawn: Callable[[int], None] | None = None,
    proxy_env: Mapping[str, str] | None = None,
    allow_dirty: bool = False,
) -> Delegation:
    """Run ``spec`` (``kind == "cli"``) against ``task`` and return the compact summary.

    ``proxy_env`` is laid over the child's scrubbed environment: an ``openrouter`` entry's
    ``base_url_var`` and ``key_var``, pointing at its metering proxy (FR-113), and nothing else.

    ``on_spawn`` is handed the child's pid the moment it exists, the same contract
    :func:`~harnessed.acp.delegate.delegate` offers - it is how the timeout test below observes the
    process the ceiling is about without a live handle crossing a boundary.
    """
    executable = _resolve_executable(spec)
    cwd = spec.cwd or str(Path.cwd())
    key = ledger.task_hash(agent=spec.name, cwd=cwd, task=task, acceptance=acceptance)

    attempt = 1
    if store is not None:
        recorded = ledger.get(store, key)
        if recorded is not None and recorded.status == ledger.SUCCEEDED:
            _publish(bus, _CACHED, {"task_hash": recorded.task_hash, "status": recorded.status})
            return _delegation_from_summary(recorded.summary)
        if recorded is not None:
            attempt = recorded.attempts + 1
        ledger.begin(
            store, key, agent=spec.name, cwd=cwd, task=task, acceptance=acceptance, attempts=attempt
        )

    detected = jail_mod.detect()
    kind = detected.kind
    reason = redact.scrub(detected.reason)
    started: dict[str, object] = {"agent": spec.name, "acceptance": acceptance, "jail": kind}
    if reason:
        started["jail_reason"] = reason
    _publish(bus, _STARTED, started)

    begun = repo.start(cwd)  # before the snapshot: a status read may touch the index
    refuse_dirty(spec, begun, store, key, attempt, bus, allow_dirty=allow_dirty)
    argv = _argv_for(spec, task)
    stdin_data = task.encode("utf-8") if spec.input == "stdin" else b""
    env = {**child_env(), **(proxy_env or {})}
    before = None if spec.allow_write else _snapshot(cwd)

    streams: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        **process_group(),
    }
    job: jail_mod.JobObject | None = None
    if kind == jail_mod.BWRAP:
        process = subprocess.Popen(
            jail_mod.wrap([executable, *argv], cwd, spec.allow_write), **streams
        )
    else:
        if kind == jail_mod.JOB_OBJECT:
            job = jail_mod.JobObject()
        if is_batch(executable):
            process = spawn_batch(spec.name, executable, argv, streams)
        else:
            process = subprocess.Popen([executable, *argv], **streams)
        if job is not None and not job.assign(process.pid):
            kind = jail_mod.NONE  # the job did not take: say so rather than claim containment
    if on_spawn is not None:
        on_spawn(process.pid)

    try:
        return _finish(
            spec,
            process,
            stdin_data,
            before,
            cwd,
            kind,
            reason,
            store,
            key,
            attempt,
            bus,
            acceptance,
        )
    finally:
        if job is not None:
            job.close()  # KILL_ON_JOB_CLOSE: nothing the child left behind outlives the run
        if store is not None:
            became = repo.finish(begun)
            if became is not None:
                repo.write(store, key, attempt, became)


def _finish(
    spec: AgentSpec,
    process: subprocess.Popen[bytes],
    stdin_data: bytes,
    before: _Snapshot | None,
    cwd: str,
    kind: str,
    reason: str,
    store: sqlite3.Connection | None,
    key: str,
    attempt: int,
    bus: EventBus | None,
    acceptance: str,
) -> Delegation:
    """Wait for the child, then settle the ledger row and publish how it ended."""
    try:
        raw_reply, raw_stderr = process.communicate(input=stdin_data, timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        kill_tree(process.pid)
        process.communicate()
        failure = (
            f"the agent {spec.name!r} exceeded its {spec.timeout_s}s timeout and its process "
            f"tree was killed (reason: timeout)"
        )
        if store is not None:
            ledger.settle(store, key, status=ledger.FAILED, attempts=attempt, failure=failure)
        _publish(bus, _FINISHED, {"agent": spec.name, "status": "failed", "reason": "timeout"})
        raise DelegationTimeout(failure) from None

    if before is not None:
        changed = _first_changed(before, _snapshot(cwd))
        if changed is not None:
            failure = (
                f"the agent {spec.name!r} wrote {changed!r} inside its working directory, and "
                f"allow_write = false forbids that (reason: wrote_files)"
            )
            if store is not None:
                ledger.settle(store, key, status=ledger.FAILED, attempts=attempt, failure=failure)
            _publish(
                bus,
                _FINISHED,
                {"agent": spec.name, "status": "failed", "reason": "wrote_files", "path": changed},
            )
            raise DelegationError(failure)

    if process.returncode != 0:
        tail = raw_stderr.decode("utf-8", errors="replace").strip()[-300:]
        failure = (
            f"the agent {spec.name!r} exited with code {process.returncode} "
            f"(reason: exit_code){': ' + tail if tail else ''}"
        )
        if store is not None:
            ledger.settle(store, key, status=ledger.FAILED, attempts=attempt, failure=failure)
        _publish(
            bus,
            _FINISHED,
            {"agent": spec.name, "status": "failed", "reason": "exit_code"},
        )
        raise DelegationError(failure)

    result = Delegation(
        status="succeeded",
        session_id="",
        files_changed=(),
        summary=_capped_reply(raw_reply),
        acceptance=acceptance,
    )
    if store is not None:
        ledger.settle(
            store,
            key,
            status=ledger.SUCCEEDED,
            attempts=attempt,
            summary={
                **result.as_dict(),
                "jail": kind,
                **({"jail_reason": reason} if reason else {}),
            },
        )
    _publish(bus, _FINISHED, {"agent": spec.name, "status": "succeeded"})
    return result


# ---- the child's world -----------------------------------------------------------------------


def _resolve_executable(spec: AgentSpec) -> str:
    executable = shutil.which(spec.command)
    if executable is None:
        raise DelegationError(
            f"the agent {spec.name!r} runs {spec.command!r}, which is not on PATH. "
            f"{spec.install_hint()}"
        )
    return executable


def _argv_for(spec: AgentSpec, task: str) -> tuple[str, ...]:
    """The task on argv, replacing ``{task}`` in every arg, or untouched when it goes on stdin."""
    if spec.input == "stdin":
        return spec.args
    return tuple(arg.replace("{task}", task) for arg in spec.args)


# ---- the jail: detection, never prevention ----------------------------------------------------


def _snapshot(cwd: str) -> _Snapshot:
    """Every regular file under ``cwd``, relative path to (size, mtime)."""
    base = Path(cwd)
    snapshot: _Snapshot = {}
    for path in sorted(base.rglob("*")):
        try:
            if path.is_file():
                info = path.stat()
                snapshot[str(path.relative_to(base))] = (info.st_size, info.st_mtime)
        except FileNotFoundError:  # removed between listing and stat
            continue
    return snapshot


def _first_changed(before: _Snapshot, after: _Snapshot) -> str | None:
    """The first path, in sorted order, present in only one snapshot or changed between them."""
    for name in sorted(set(before) | set(after)):
        if before.get(name) != after.get(name):
            return name
    return None


# ---- the reply --------------------------------------------------------------------------------


def _capped_reply(raw: bytes) -> str:
    """stdout, decoded, cut at :data:`MAX_REPLY_BYTES` with the cut stated (never silent)."""
    if len(raw) <= MAX_REPLY_BYTES:
        return raw.decode("utf-8", errors="replace")
    return (
        raw[:MAX_REPLY_BYTES].decode("utf-8", errors="replace")
        + f"\n...[cut: reply exceeded {MAX_REPLY_BYTES} bytes]"
    )


# ---- what the ledger already knows ------------------------------------------------------------


def _delegation_from_summary(summary: dict[str, object]) -> Delegation:
    """Rebuild the compact summary a succeeded row already recorded, as ``delegate.py`` does."""
    return Delegation(
        status=str(summary["status"]),
        session_id=str(summary["session_id"]),
        files_changed=tuple(str(name) for name in list(summary["files_changed"])),  # type: ignore[call-overload]
        summary=str(summary["summary"]),
        acceptance=str(summary["acceptance"]),
    )


def _publish(bus: EventBus | None, name: str, payload: dict[str, object]) -> None:
    if bus is not None:
        bus.publish(name, payload)
