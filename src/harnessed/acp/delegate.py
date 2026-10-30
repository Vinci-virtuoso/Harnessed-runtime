"""Spawn, handshake, prompt, and hand back a summary small enough for a model to read (FR-22).

This module and :mod:`~harnessed.acp.client` are the only two that touch the protocol. The shape
is deliberately one function: :func:`delegate` takes a unit of work and answers with a
:class:`Delegation`. There is no session object, no turn API and no cancel handle, because the
model calling ``delegate_to_agent`` is handing off work, not driving a sub-agent.

**What the parent model gets back is five fields.** Status, session id, files changed, summary and
acceptance - a few hundred characters. The full transcript, which is where the tool titles, the
raw inputs and the permission traffic live, goes to ``<home>/acp/<id>.json`` and to episodic
memory. Putting a sub-agent's whole conversation into the parent's context is how a delegation
stops being a delegation.

**Four named failures, and a fifth that has no name, all of them values by the time the loop sees
them.** A missing CLI is a :class:`DelegationError` naming the exact install command. A protocol
version the agent does not share is an :class:`AcpVersionMismatch` raised at ``initialize`` and
never later. A run past its ceiling is a :class:`DelegationTimeout` whose message carries the
partial summary, raised after the process tree is killed by its recorded pid. An agent that closes
the connection before answering is a :class:`DelegationError` quoting the end of its own stderr
(ticket 088). Anything else - a bare JSON-RPC "Internal error" with no further detail is the one
observed in the wild - is caught after those four and re-raised as a :class:`DelegationError`
naming only the scrubbed class of what was caught (ticket 152, decision 69). The tool registry
turns each of them into an error ``ToolResult``, so the loop sees a failed tool rather than a
crashed harness.

**Why the pid is recorded rather than the process handed around.** The coroutine owns the
``Process`` object on the background loop; the calling thread only ever learns an integer. When
the ceiling fires, the calling thread is the one still running, so it is the one that kills the
tree - ``taskkill /T /F`` on Windows, the process group elsewhere. Killing the tree is what
unblocks the coroutine, not the other way round.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import ctypes
import functools
import json
import os
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import acp
from acp import PROTOCOL_VERSION

from harnessed import clock, ids, proc
from harnessed.acp import ledger, pending, repo, runtime, turns
from harnessed.acp.client import DelegationClient, PermissionAsked
from harnessed.acp.config import BILLING_OPENROUTER, AgentSpec, quota_markers_for
from harnessed.acp.permissions import Decision, Prompter, row_of
from harnessed.config import child_env
from harnessed.errors import (
    AcpVersionMismatch,
    DelegationError,
    DelegationTimeout,
    DirtyTreeRefused,
    PermissionAnswered,
    QuotaRefused,
)
from harnessed.events.bus import EventBus
from harnessed.memory import episodic, redact

__all__ = [
    "Delegation",
    "alive",
    "delegate",
    "disallowed_tools",
    "kill_tree",
    "refuse_dirty",
    "request_payload",
    "session_meta",
]

_STARTED: Final = "acp.delegation.started"
_FINISHED: Final = "acp.delegation.finished"
_REQUEST: Final = "acp.permission.request"
_DECISION: Final = "acp.permission.decision"
_CACHED: Final = "acp.delegation.cached"
_TOOL_CALLED: Final = "acp.tool.called"
_TOOL_FINISHED: Final = "acp.tool.finished"

#: The episode kind a delegation transcript is filed under in ``mem_episodic``.
EPISODE_KIND: Final = "delegation"

#: How long the tree kill waits for the child to actually die before giving up on politeness.
_KILL_TIMEOUT_S: Final = 10.0

#: How much of a dying agent's stderr its error quotes: the last lines, each one cut short.
_STDERR_LINES: Final = 20
_STDERR_LINE_CHARS: Final = 160
_STDERR_CHUNK: Final = 1 << 16
#: How long a closed connection waits for the child to exit and its stderr to reach EOF.
_STDERR_GRACE_S: Final = 2.0

#: Executables Windows can only run through cmd.exe, and what cmd.exe does to a token.
_BATCH_SUFFIXES: Final = frozenset({".cmd", ".bat"})
_CMD_SPLITS: Final = frozenset(" \t&|<>^(),;=")
_CMD_UNSAFE: Final = frozenset('"%\r\n')
_MAX_PATH: Final = 260


@dataclass(frozen=True, slots=True)
class Delegation:
    """The compact structured summary. Exactly what the parent model is allowed to see."""

    status: str
    session_id: str
    files_changed: tuple[str, ...]
    summary: str
    acceptance: str
    #: ``True`` when the handshake advertised a login and the session opened on it, ``None`` when
    #: the agent said nothing (ticket 175). ``agents test`` reads it; the parent model never does,
    #: so it stays out of :meth:`as_dict`.
    login: bool | None = None

    def as_dict(self) -> dict[str, object]:
        """The five fields as JSON, which is what the tool returns as its content."""
        return {
            "status": self.status,
            "session_id": self.session_id,
            "files_changed": list(self.files_changed),
            "summary": self.summary,
            "acceptance": self.acceptance,
        }


def delegate(
    spec: AgentSpec,
    task: str,
    acceptance: str,
    *,
    home: Path,
    bus: EventBus | None = None,
    store: sqlite3.Connection | None = None,
    interactive: bool = False,
    prompter: Prompter | None = None,
    door: pending.Door | None = None,
    on_spawn: Callable[[int], None] | None = None,
    message_seq: int | None = None,
    hop: int = 0,
    proxy_env: Mapping[str, str] | None = None,
    allow_dirty: bool = False,
) -> Delegation:
    """Hand ``task`` to ``spec``'s agent and return the summary of what came back.

    ``on_spawn`` is handed the child's pid the moment it exists. It is how a caller - the timeout
    test above all - observes the process the ceiling is about, without this module having to
    hand a live handle across a thread boundary.

    ``message_seq`` and ``hop`` name the routed message and chain hop the delegation answers
    (decision 61); they land on its ``agent_turns`` rows, and are ``None`` and ``0`` for a
    delegation the ``delegate_to_agent`` tool starts.

    ``proxy_env`` is given for an ``openrouter`` entry only (FR-113): the metering proxy's
    loopback URL and placeholder key are laid over the child's environment (MUST rule 33). Every
    child, with or without it, gets this process's environment less every ``SECRET_VARS`` value
    (decision 62; ticket 213), so a subscription entry gets no base URL and no credential.

    A working tree with uncommitted changes refuses the delegation (``DirtyTreeRefused``, the
    agent never launched) unless ``allow_dirty`` says the operator wants it anyway (FR-146).

    ``store`` is what makes this function idempotent, and without one it is not: no ledger, no
    done-state, and the agent runs again. With one, the ledger pattern runs verbatim except that
    the row is written before the work rather than after, because the work is destructive
    (FR-23). A hash already marked succeeded never reaches ``runtime.submit``.
    """
    executable = shutil.which(spec.command)
    if executable is None:
        raise DelegationError(
            f"the agent {spec.name!r} runs {spec.command!r}, which is not on PATH. "
            f"{spec.install_hint()}"
        )

    cwd = spec.cwd or str(Path.cwd())
    key = ledger.task_hash(agent=spec.name, cwd=cwd, task=task, acceptance=acceptance)
    attempt = 1
    if store is not None:
        recorded = ledger.get(store, key)
        if recorded is not None:
            settled = _from_ledger(recorded, bus)
            if settled is not None:
                return settled
            attempt = recorded.attempts + 1
        ledger.begin(
            store, key, agent=spec.name, cwd=cwd, task=task, acceptance=acceptance, attempts=attempt
        )

    door = door or pending.Door()
    # An answer door needs a ledger to hold the row and a bus to name the session it belongs to.
    policy = replace(
        spec.policy(interactive=interactive or door.interactive),
        answer_door=door.answer_door and store is not None and bus is not None,
    )
    client = DelegationClient(policy=policy, prompter=prompter or door.prompter)
    waits = _Waits(spec, client, bus, store, door, message_seq, hop, key)
    finish = functools.partial(_finish, waits=waits)
    pid = _Pid(on_spawn)
    journal = _TurnJournal(key, task, message_seq, hop)
    _publish(bus, _STARTED, {"agent": spec.name, "acceptance": acceptance})

    begun = repo.start(cwd)
    refuse_dirty(spec, begun, store, key, attempt, bus, allow_dirty=allow_dirty)
    try:
        session_id = ""
        try:
            session_id = runtime.submit(
                lambda: _converse(executable, spec, task, client, pid, proxy_env),
                spec.timeout_s,
                # Ticket 060: the kill happens inside the ceiling, not after it. ``submit`` calls
                # this the moment the ceiling expires and then waits for the coroutine to actually
                # stop, so by the time ``_finish`` reads ``client`` below nothing is still writing
                # to it. Killing here and reading immediately afterwards is what let a torn partial
                # reach the raised error and, worse, ``ledger.settle``.
                on_timeout=lambda: kill_tree(pid.value),
                # Each tool call the agent starts or finishes is published while it is still
                # working, from this thread, so the console shows the agent's steps live
                # (acp/client.py).
                on_wait=waits.tick,
                # A request open with the operator pauses the delegation's own timeout (ticket 234).
                pausing=waits.pausing,
            )
        except DelegationTimeout as exc:
            result = finish(
                spec, "timed_out", session_id, acceptance, client, home, bus, store, journal
            )
            failed = DelegationTimeout(
                f"{exc}. Partial result: {json.dumps(result.as_dict())}", settled=exc.settled
            )
            if store is not None:
                ledger.settle(
                    store, key, status=ledger.FAILED, attempts=attempt, failure=str(failed)
                )
            raise failed from exc
        except AcpVersionMismatch as exc:
            kill_tree(pid.value)
            finish(
                spec, "version_mismatch", session_id, acceptance, client, home, bus, store, journal
            )
            if store is not None:
                ledger.settle(store, key, status=ledger.FAILED, attempts=attempt, failure=str(exc))
            raise
        except DelegationError as exc:
            # Ticket 088: an agent that closed the connection, or could not be launched at all. The
            # message already carries the child's own last words, so it is recorded as it stands.
            kill_tree(pid.value)
            finish(spec, "failed", session_id, acceptance, client, home, bus, store, journal)
            if store is not None:
                ledger.settle(store, key, status=ledger.FAILED, attempts=attempt, failure=str(exc))
            raise
        except Exception as exc:
            # Ticket 152, decision 69, FR-93: a bare JSON-RPC "Internal error" is none of the three
            # named failures above. Ticket 229 keeps the agent's own code and message (and data),
            # under ``redact.scrub`` and a one-line cap, because that is what the operator needs to
            # act.
            kill_tree(pid.value)
            detail = _failure_detail(exc)
            if _is_quota_refusal(spec, exc):
                # Ticket 194, decision 63: the agent's own recorded marker, matched on data and
                # never on wording. It settles under its own reason, ahead of the generic path
                # below.
                finish(
                    spec,
                    ledger.QUOTA_REFUSED,
                    session_id,
                    acceptance,
                    client,
                    home,
                    bus,
                    store,
                    journal,
                    failure=detail,
                )
                if store is not None:
                    ledger.settle(
                        store,
                        key,
                        status=ledger.FAILED,
                        attempts=attempt,
                        failure=f"{ledger.QUOTA_REFUSED}: {detail}",
                    )
                raise QuotaRefused(f"the agent {spec.name!r} refused for quota: {detail}") from exc
            finish(
                spec,
                "error",
                session_id,
                acceptance,
                client,
                home,
                bus,
                store,
                journal,
                failure=detail,
            )
            if store is not None:
                ledger.settle(store, key, status=ledger.FAILED, attempts=attempt, failure=detail)
            if detail == redact.scrub(type(exc).__name__):
                raise DelegationError(
                    f"the agent {spec.name!r} failed with an unnamed error ({detail})"
                ) from exc
            raise DelegationError(f"the agent {spec.name!r} failed: {detail}") from exc

        result = finish(
            spec, "succeeded", session_id, acceptance, client, home, bus, store, journal
        )
        if store is not None:
            ledger.settle(
                store, key, status=ledger.SUCCEEDED, attempts=attempt, summary=result.as_dict()
            )
        return result
    finally:
        if store is not None:
            became = repo.finish(begun)
            if became is not None:
                repo.write(store, key, attempt, became)


def refuse_dirty(
    spec: AgentSpec,
    begun: repo.Start | None,
    store: sqlite3.Connection | None,
    key: str,
    attempt: int,
    bus: EventBus | None,
    *,
    allow_dirty: bool,
) -> None:
    """Raise :class:`DirtyTreeRefused` for a dirty tree nobody allowed, after recording why.

    The ledger row settles ``failed`` with :data:`ledger.DIRTY_TREE` and the attempt is not
    counted (nothing ran), ``repo_records`` keeps the dirty paths, and ``finished`` carries the
    reason. A clean tree, no repository, or ``allow_dirty`` returns quietly.
    """
    if begun is None or not begun.dirty or allow_dirty:
        return
    message = (
        f"the working tree at {begun.cwd} has uncommitted changes, so the agent {spec.name!r} "
        f"was not started (reason: {ledger.DIRTY_TREE}). Commit or stash them, or pass "
        f"--allow-dirty."
    )
    if store is not None:
        repo.write(store, key, attempt, repo.refuse(begun))
        ledger.settle(
            store,
            key,
            status=ledger.FAILED,
            attempts=attempt - 1,
            failure=f"{ledger.DIRTY_TREE}: {message}",
        )
    _publish(bus, _FINISHED, {"agent": spec.name, "status": "failed", "reason": ledger.DIRTY_TREE})
    raise DirtyTreeRefused(message)


def alive(pid: int | None) -> bool:
    """Whether ``pid`` still names a running process. ``None`` never was one (``proc.alive``)."""
    return proc.alive(pid)


def kill_tree(pid: int | None) -> None:
    """Kill ``pid`` and everything under it, so a hung agent leaves no orphaned children."""
    if pid is None or not alive(pid):
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            check=False,
            timeout=_KILL_TIMEOUT_S,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        # The tree died between the liveness check and here, which is the outcome we wanted.
        return


# ---- what the ledger already knows ---------------------------------------------------------------


def _from_ledger(recorded: ledger.Record, bus: EventBus | None) -> Delegation | None:
    """The answer the ledger can give on its own, or ``None`` when the agent must really run.

    Two rows answer for themselves. A succeeded one hands back the summary it recorded, which is
    the whole point of FR-23: the repository was already changed once and must not be changed
    again. An exhausted one - two attempts made, none of them finished - hands back its recorded
    failure as the error the tool layer turns into a failed ``ToolResult``, which is what
    "returned immediately" means to a loop that only ever sees tool results.
    """
    if recorded.status == ledger.SUCCEEDED:
        _publish(bus, _CACHED, {"task_hash": recorded.task_hash, "status": recorded.status})
        summary = recorded.summary
        return Delegation(
            status=str(summary["status"]),
            session_id=str(summary["session_id"]),
            files_changed=tuple(str(name) for name in list(summary["files_changed"])),  # type: ignore[call-overload]
            summary=str(summary["summary"]),
            acceptance=str(summary["acceptance"]),
        )
    if recorded.exhausted:
        _publish(bus, _CACHED, {"task_hash": recorded.task_hash, "status": recorded.status})
        raise DelegationError(
            f"the delegation {recorded.task_hash} has already been attempted "
            f"{recorded.attempts} times against {recorded.agent!r} and will not be attempted "
            f"again. The recorded failure was: {recorded.failure}"
        )
    return None


# ---- the protocol, all of it -------------------------------------------------------------------


class _Pid:
    """The one integer that crosses the thread boundary, written once by the coroutine."""

    def __init__(self, on_spawn: Callable[[int], None] | None) -> None:
        self.value: int | None = None
        self._on_spawn = on_spawn

    def record(self, pid: int) -> None:
        self.value = pid
        if self._on_spawn is not None:
            self._on_spawn(pid)


async def _converse(
    executable: str,
    spec: AgentSpec,
    task: str,
    client: DelegationClient,
    pid: _Pid,
    proxy_env: Mapping[str, str] | None = None,
) -> str:
    """Spawn the agent, complete the handshake, prompt once, and return the session id.

    stderr is a pipe drained for the whole run, never DEVNULL: a child that dies before answering
    leaves ``Connection closed`` as the only symptom otherwise, and its own reason - a shell that
    split a path, an adapter that would not start - is exactly what the operator needs (MUST
    rule 12). Draining is what keeps a talkative child from filling the pipe and stalling.
    """
    process = await _spawn(executable, spec, proxy_env)
    pid.record(process.pid)
    said = _StderrTail()
    draining = asyncio.ensure_future(said.drain(process.stderr))
    connection = acp.connect_to_agent(client, process.stdin, process.stdout)
    try:
        answer = await connection.initialize(protocol_version=PROTOCOL_VERSION)
        if answer.protocol_version != PROTOCOL_VERSION:
            raise AcpVersionMismatch(
                f"the agent {spec.name!r} answered initialize with protocol version "
                f"{answer.protocol_version}, and this harness speaks {PROTOCOL_VERSION}. "
                f"Upgrade the agent, or pin one that speaks {PROTOCOL_VERSION}."
            )
        client.auth_methods = tuple(method.id for method in answer.auth_methods or ())
        session = await connection.new_session(
            cwd=spec.cwd or str(Path.cwd()), **session_meta(spec)
        )
        await connection.prompt(session_id=session.session_id, prompt=[acp.text_block(task)])
        return str(session.session_id)
    except ConnectionError as exc:
        raise DelegationError(await _closed(spec, process, draining, said)) from exc
    finally:
        # Ticket 090: the same tree kill as every failing path, and before the connection closes.
        # ``process.kill()`` ends only the direct child - ``cmd.exe`` or a venv launcher - and
        # closing stdin first lets the root exit on its own, after which Windows can no longer
        # find the tree beneath it.
        if process.returncode is None:
            await asyncio.to_thread(kill_tree, process.pid)
        await connection.close()
        draining.cancel()


def disallowed_tools(spec: AgentSpec) -> tuple[str, ...]:
    """The vendor tool names to refuse at launch: every ``deny`` kind, plus in ``allowlist`` mode
    every mapped kind not in ``allow`` (``deny`` mode refuses every mapped kind). Names come from
    ``spec.tool_names``; this function holds no vendor word.
    """
    kinds = list(spec.deny)
    if spec.mode == "deny":
        kinds.extend(spec.tool_names)
    elif spec.mode == "allowlist":
        kinds.extend(kind for kind in spec.tool_names if kind not in spec.allow)
    names: dict[str, None] = {}
    for kind in kinds:
        names.update(dict.fromkeys(spec.tool_names.get(kind, ())))
    if spec.mode == "allowlist":
        # One vendor tool can serve two kinds (Bash is both execute and delete): a kind the
        # operator allowed and did not deny keeps its name, whatever an unallowed kind shares.
        for kind in spec.allow:
            if kind not in spec.deny:
                for name in spec.tool_names.get(kind, ()):
                    names.pop(name, None)
    return tuple(names)


def session_meta(spec: AgentSpec) -> dict[str, Any]:
    """The keyword arguments ``session/new`` carries as ``_meta``; empty when none are declared.

    The ``acp`` SDK's ``new_session(cwd, **kwargs)`` sends every extra keyword as a key of the
    request's ``_meta`` table, so the entry's ``session_meta`` is splatted. The derived
    ``disallowedTools`` is unioned into ``claudeCode.options`` only when that table exists.
    """
    if not spec.session_meta:
        return {}
    meta: dict[str, Any] = copy.deepcopy(dict(spec.session_meta))
    claude = meta.get("claudeCode")
    if isinstance(claude, dict):
        options = claude.setdefault("options", {})
        written = options.get("disallowedTools", [])
        union = dict.fromkeys([*written, *disallowed_tools(spec)])
        options["disallowedTools"] = list(union)
    return meta


async def _spawn(
    executable: str, spec: AgentSpec, proxy_env: Mapping[str, str] | None = None
) -> asyncio.subprocess.Process:
    """Start the agent with all three streams piped, and with ``proxy_env`` when it has one.

    A ``.cmd`` or ``.bat`` executable on Windows runs through ``System32\\cmd.exe`` named
    explicitly. Handed to ``CreateProcess`` directly, a batch file runs through whatever
    ``ComSpec`` says, and any parent process can set that: a ``powershell.exe`` there split
    ``C:\\Program Files\\nodejs\\npx.CMD`` at its space and the adapter never started.
    """
    streams: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": spec.cwd,
        **process_group(),
    }
    # Every acp child, whatever its billing, gets the parent's environment less SECRET_VARS
    # (decision 62), with the proxy overlay when one exists (MUST rule 33).
    streams["env"] = {**child_env(), **(proxy_env or {})}
    if is_batch(executable):
        # With ``executable`` given, subprocess runs exactly that program and never reads
        # ComSpec; it wraps the line as ``/c "<line>"``, which cmd.exe unwraps back to it.
        return await asyncio.create_subprocess_shell(
            batch_line(spec.name, executable, spec.args), executable=cmd_exe(), **streams
        )
    return await asyncio.create_subprocess_exec(executable, *spec.args, **streams)


def is_batch(executable: str) -> bool:
    """Whether Windows can only run ``executable`` through cmd.exe (a ``.cmd`` or ``.bat``)."""
    return sys.platform == "win32" and Path(executable).suffix.casefold() in _BATCH_SUFFIXES


def cmd_exe() -> str:
    """``%SystemRoot%\\System32\\cmd.exe``, asked of Windows rather than of the environment."""
    if sys.platform != "win32":  # the check mypy narrows on, so ctypes' win32 names type-check
        raise OSError("a Windows-only call reached on " + sys.platform)
    buffer = ctypes.create_unicode_buffer(_MAX_PATH)
    ctypes.windll.kernel32.GetSystemDirectoryW(buffer, _MAX_PATH)
    return str(Path(buffer.value) / "cmd.exe")


def spawn_batch(
    name: str, executable: str, args: Sequence[str], streams: dict[str, Any]
) -> subprocess.Popen[bytes]:
    """Start the batch file ``executable`` through cmd.exe: the one launch the core never spells."""
    return subprocess.Popen(
        batch_line(name, executable, tuple(args)), shell=True, executable=cmd_exe(), **streams
    )


def batch_line(name: str, executable: str, args: tuple[str, ...]) -> str:
    """The command line cmd.exe runs, each token quoted where cmd.exe would otherwise split it."""
    tokens = [executable, *args]
    for token in tokens:
        if any(char in token for char in _CMD_UNSAFE):
            raise DelegationError(
                f"the agent {name!r} runs the batch file {executable}, and cmd.exe cannot "
                f"pass {token!r} to it intact. Point 'command' at the program the batch file "
                f"wraps instead."
            )
    return " ".join(
        f'"{token}"' if not token or any(char in token for char in _CMD_SPLITS) else token
        for token in tokens
    )


class _StderrTail:
    """The last few lines a child wrote to stderr, each bounded, everything else discarded."""

    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=_STDERR_LINES)

    async def drain(self, stream: asyncio.StreamReader | None) -> None:
        """Read until EOF. Chunked rather than by line, so a megabyte with no newline is fine."""
        if stream is None:
            return
        pending = ""
        while chunk := await stream.read(_STDERR_CHUNK):
            *complete, pending = (pending + chunk.decode("utf-8", errors="replace")).split("\n")
            self._lines.extend(line.rstrip("\r")[:_STDERR_LINE_CHARS] for line in complete)
            pending = pending[:_STDERR_LINE_CHARS]
        if pending.strip():
            self._lines.append(pending.rstrip("\r"))

    def text(self) -> str:
        """What the error quotes: scrubbed, because a child may well print a key it was given."""
        return redact.scrub("\n".join(line for line in self._lines if line.strip()))


async def _closed(
    spec: AgentSpec,
    process: asyncio.subprocess.Process,
    draining: asyncio.Future[None],
    said: _StderrTail,
) -> str:
    """Why the agent went away, in its own words where it left any."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), _STDERR_GRACE_S)
    await asyncio.wait({draining}, timeout=_STDERR_GRACE_S)
    code = process.returncode
    ended = "is still running" if code is None else f"exited with code {code}"
    tail = said.text()
    quoted = f"The end of its stderr:\n{tail}" if tail else "It wrote nothing to stderr."
    return f"the agent {spec.name!r} closed the connection before answering and {ended}. {quoted}"


def process_group() -> dict[str, Any]:
    """Put the child in its own group where a platform has them, so the tree kill has a target."""
    if sys.platform == "win32":
        return {}
    return {"start_new_session": True}


# ---- flushing what the coroutine collected ------------------------------------------------------


def _finish(
    spec: AgentSpec,
    status: str,
    session_id: str,
    acceptance: str,
    client: DelegationClient,
    home: Path,
    bus: EventBus | None,
    store: sqlite3.Connection | None,
    journal: _TurnJournal,
    failure: str | None = None,
    *,
    waits: _Waits,
) -> Delegation:
    """Publish, persist and summarise, on the calling thread, whatever the delegation collected.

    Called on every path including the failing ones, which is what makes a timed-out delegation
    still leave a partial transcript on disk and a ``finished`` event with its real status.
    Steps the last wait did not reach are published first, so every step precedes ``finished``.
    Permission requests are *not* published here: they went out live, from the waiting thread, as
    they were asked and answered (ticket 234, known defect (e)); only a request that arrived after
    the last tick is flushed.
    """
    waits.flush()
    result = Delegation(
        status=status,
        session_id=session_id,
        files_changed=tuple(client.files_changed),
        summary=client.summary(),
        acceptance=acceptance,
        # An agent that advertised a login and then opened a session accepted the one it holds.
        login=True if client.auth_methods and session_id else None,
    )
    # A refusal for quota reads ``quota_refused: <detail>`` in the transcript, as in the ledger.
    recorded = failure
    if failure is not None and status == ledger.QUOTA_REFUSED:
        recorded = f"{status}: {failure}"
    _write_transcript(home, spec, result, client, store, bus, recorded)
    _journal_turns(spec, result, client, store, bus, journal)
    _publish(
        bus,
        _FINISHED,
        {
            "agent": spec.name,
            "status": status,
            "session_id": session_id,
            "files_changed": list(result.files_changed),
            **({} if failure is None else {"reason": failure}),
        },
    )
    return result


_FAILURE_CAP: Final = 300


def _is_quota_refusal(spec: AgentSpec, exc: BaseException) -> bool:
    """Whether ``exc`` is a JSON-RPC error whose ``data`` carries a quota marker of the entry."""
    if not isinstance(exc, acp.RequestError):
        return False
    data = getattr(exc, "data", None)
    if not isinstance(data, dict):
        return False
    return any(data.get(key) in values for key, values in quota_markers_for(spec.name).items())


def _failure_detail(exc: BaseException) -> str:
    """One scrubbed line of at most 300 characters saying what an unnamed failure was (ticket 229).

    An agent's JSON-RPC error reads ``<code> <message>`` and `` (<data>)`` as compact JSON when it
    has any; any other exception reads ``<ClassName>: <first line>``, or the class name alone.
    Read by attribute, defensively: the SDK class is outside this repository.
    """
    name = type(exc).__name__
    if isinstance(exc, acp.RequestError):
        message = exc.args[0] if exc.args else ""
        detail = f"{getattr(exc, 'code', '')} {message}".strip()
        data = getattr(exc, "data", None)
        if data is not None:
            try:
                detail += " (" + json.dumps(data, separators=(",", ":"), default=str) + ")"
            except (TypeError, ValueError):
                detail += f" ({data})"
    else:
        text = str(exc).strip()
        first = text.splitlines()[0].strip() if text else ""
        detail = f"{name}: {first}" if first else name
    line = " ".join(redact.scrub(detail).split())
    return line if len(line) <= _FAILURE_CAP else line[: _FAILURE_CAP - 3] + "..."


def _write_transcript(
    home: Path,
    spec: AgentSpec,
    result: Delegation,
    client: DelegationClient,
    store: sqlite3.Connection | None,
    bus: EventBus | None,
    failure: str | None = None,
) -> None:
    """Put the full transcript where an operator can read it, and nowhere near the parent model."""
    body = json.dumps(
        {
            "agent": spec.name,
            "status": result.status,
            "session_id": result.session_id,
            "acceptance": result.acceptance,
            "transcript": client.transcript,
            **({} if failure is None else {"failure": failure}),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    scrubbed = redact.scrub(body)
    folder = home / "acp"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{ids.new_id('dlg')}.json").write_text(scrubbed, encoding="utf-8")
    if store is not None and bus is not None:
        episodic.put(store, bus.run_id, EPISODE_KIND, scrubbed)


@dataclass(frozen=True, slots=True)
class _TurnJournal:
    """What a delegation's ``agent_turns`` rows carry beyond its transcript."""

    task_hash: str
    task: str
    message_seq: int | None
    hop: int


def _journal_turns(
    spec: AgentSpec,
    result: Delegation,
    client: DelegationClient,
    store: sqlite3.Connection | None,
    bus: EventBus | None,
    journal: _TurnJournal,
) -> None:
    """Journal the transcript's turns as ``agent_turns`` rows - never as ``turns`` (FR-112).

    An ``openrouter`` entry is skipped: its model traffic is journaled by the metering proxy
    (FR-113), and a second row per turn from the transcript would count it twice (decision 63).
    """
    if store is None or bus is None or spec.billing == BILLING_OPENROUTER:
        return
    turns.record(
        store,
        bus.run_id,
        turns.from_transcript(
            client.transcript,
            billing=spec.billing,
            request=journal.task,
            complete=result.status == "succeeded",
        ),
        entry=spec.name,
        billing=spec.billing,
        source="acp",
        message_seq=journal.message_seq,
        hop=journal.hop,
        task_hash=journal.task_hash,
    )


def request_payload(agent: str, row: pending.Row) -> dict[str, object]:
    """The ``acp.permission.request`` payload for ``row``: the one spelling, live and recorded."""
    return {
        "agent": agent,
        "tool": row.tool_kind,
        "permission_id": row.permission_id,
        "tool_kind": row.tool_kind,
        "description": row.description,
        "ceiling_at": row.ceiling_at,
        "message_seq": None if row.message_seq is None else int(row.message_seq),
        "hop": None if row.hop is None else int(row.hop),
    }


@dataclass
class _Open:
    """A request the operator has not answered, or has answered and the run not yet resumed."""

    asked: PermissionAsked
    row: pending.Row
    #: The row as answered, once it is; ``None`` while the operator has not said.
    answered: pending.Row | None = None


class _Waits:
    """The calling thread's half of every permission request (decisions 74 and 75, FR-130).

    ``client.py`` runs on the ACP loop and may neither publish nor write, so a request arrives here
    on ``client.asked`` and everything that follows happens on this thread, on each ``on_wait``
    tick: the row is recorded (``acp/pending.py::record``), ``acp.permission.request`` published
    at once, and an open request is polled until a person answers it, the ceiling passes, or the
    process is stopping. Whoever answers, the decision is published here and the agent's future is
    resolved with it; an answer waits on ``Door.on_answered`` (the run lease) tick by tick first.
    """

    def __init__(
        self,
        spec: AgentSpec,
        client: DelegationClient,
        bus: EventBus | None,
        store: sqlite3.Connection | None,
        door: pending.Door,
        message_seq: int | None = None,
        hop: int | None = None,
        task_hash: str | None = None,
    ) -> None:
        self.task_hash = task_hash
        self.message_seq = message_seq
        self.hop = hop if message_seq is not None else None
        self.spec = spec
        self.client = client
        self.bus = bus
        self.store = store
        self.door = door
        self.open: dict[str, _Open] = {}

    def pausing(self) -> bool:
        """Whether a request is open, which holds the delegation's own timeout still."""
        return bool(self.open)

    def tick(self) -> None:
        """One ``on_wait`` slice: steps, new requests, then every open request's progress."""
        _publish_steps(self.spec, self.client, self.bus)
        self._take()
        self._settle()

    def flush(self) -> None:
        """At the end: steps first, then any request that arrived after the last tick."""
        _publish_steps(self.spec, self.client, self.bus)
        self._take()

    # ---- a new request ------------------------------------------------------------------

    def _take(self) -> None:
        while True:
            try:
                asked = self.client.asked.get_nowait()
            except queue.Empty:
                return
            if asked.answered.cancelled():
                continue
            if asked.decision is not None:
                answer, rule, via = row_of(asked.decision)
                row = self._record(asked, None, answer, rule, via)
                self._request(row)
                self._decided(row, asked.decision)
                continue
            ceiling = clock.now() + timedelta(seconds=self.door.ceiling_s)
            row = self._record(asked, clock.isoformat(ceiling), None, None, None)
            self._request(row)
            self.open[row.permission_id] = _Open(asked, row)
            if self.door.on_pending is not None:
                self.door.on_pending(row.permission_id)

    def _record(
        self,
        asked: PermissionAsked,
        ceiling_at: str | None,
        answer: str | None,
        rule: str | None,
        via: str | None,
    ) -> pending.Row:
        if self.store is None or self.bus is None:
            now = clock.isoformat(clock.now())
            return pending.Row(
                ids.new_id("perm"), "", self.spec.name, asked.tool_kind, asked.description or None,
                now, ceiling_at, answer, rule, via, None if answer is None else now,
                None if self.message_seq is None else str(self.message_seq),
                None if self.hop is None else str(self.hop),
                self.task_hash,
            )  # fmt: skip
        return pending.record(
            self.store,
            session_id=self.bus.run_id,
            agent=self.spec.name,
            tool_kind=asked.tool_kind,
            description=asked.description or None,
            ceiling_at=ceiling_at,
            answer=answer,
            rule=rule,
            answered_via=via,
            message_seq=self.message_seq,
            hop=self.hop,
            task_hash=self.task_hash,
            raw_input=asked.raw_input,
            warning=None if asked.decision is None else asked.decision.warning,
        )

    def _request(self, row: pending.Row) -> None:
        _publish(self.bus, _REQUEST, request_payload(self.spec.name, row))

    def _decided(self, row: pending.Row, decision: Decision) -> None:
        _publish(
            self.bus,
            _DECISION,
            {
                "agent": self.spec.name,
                "tool": row.tool_kind,
                "allowed": decision.allowed,
                "rule": decision.rule,
                "warning": decision.warning,
                "permission_id": row.permission_id,
                "answer": row.answer,
                "answered_via": row.answered_via,
            },
        )

    # ---- an open request ----------------------------------------------------------------

    def _settle(self) -> None:
        for permission_id, item in list(self.open.items()):
            if item.asked.answered.cancelled():
                del self.open[permission_id]  # the agent went away; ticket 243's sweep expires it
                continue
            if item.answered is None and not self._answered(item):
                continue
            stopping = self.door.stopping is not None and self.door.stopping()
            if (
                not stopping
                and self.door.on_answered is not None
                and not self.door.on_answered(permission_id)
            ):
                continue  # another run holds the lease; ask again next tick
            assert item.answered is not None
            item.asked.answered.set_result(_decision_of(item.answered))
            del self.open[permission_id]

    def _answered(self, item: _Open) -> bool:
        """Read the row; deny it at the ceiling or on shutdown; publish once it is answered."""
        assert self.store is not None
        row = pending.get(self.store, item.row.permission_id)
        assert row is not None
        if row.answer is None:
            if self.door.stopping is not None and self.door.stopping():
                row = self._deny(row, "operator")
            elif row.ceiling_at is not None and clock.now() >= clock.parse(row.ceiling_at):
                row = self._deny(row, "ceiling")
            else:
                return False
        item.answered = row
        self._decided(row, _decision_of(row))
        return True

    def _deny(self, row: pending.Row, rule: str) -> pending.Row:
        """The waiting thread's own answer, by one compare-and-set; a lost race reads who won."""
        assert self.store is not None
        try:
            return pending.answer(self.store, row.permission_id, "deny", rule=rule, via=None)
        except PermissionAnswered:
            fresh = pending.get(self.store, row.permission_id)
            assert fresh is not None
            return fresh


def _decision_of(row: pending.Row) -> Decision:
    """The matrix's own words for an answered row: the event and the agent read one vocabulary."""
    allowed = row.answer == "approve"
    if row.rule == "ceiling":
        return Decision(allowed=allowed, rule="ask_timeout")
    return Decision(allowed=allowed, rule="ask_allowed" if allowed else "ask_denied")


def _publish_steps(spec: AgentSpec, client: DelegationClient, bus: EventBus | None) -> None:
    """Publish every step the client has queued since the last call, oldest first."""
    while True:
        try:
            step = client.steps.get_nowait()
        except queue.Empty:
            return
        phase = step.pop("phase")
        name = _TOOL_CALLED if phase == "called" else _TOOL_FINISHED
        _publish(bus, name, {"agent": spec.name, **step})


def _publish(bus: EventBus | None, name: str, payload: dict[str, object]) -> None:
    """Announce one moment of the delegation, when there is a bus listening."""
    if bus is not None:
        bus.publish(name, payload)
