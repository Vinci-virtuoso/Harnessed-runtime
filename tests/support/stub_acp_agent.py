"""A real ACP agent process, speaking real newline-delimited JSON-RPC over stdio.

Architecture open question 11. Non-``live`` ACP tests spawn this file via ``sys.executable``
rather than patching a mock into the client, because the assertion FR-22 exists to make - a
protocol version mismatch fails loudly *at* ``initialize`` and never silently mid-run - is only
worth anything against a real handshake on a real transport with a wrong version on the far
side. It is also what makes FR-23's spawn-exactly-once claim measurable: the spawn spy counts
process creations, and a process is what this is.

The wire is hand-rolled rather than driven through the SDK's ``Agent`` interface on purpose.
``bad_version`` has to put a value on the wire that the SDK's own typed response would refuse,
and ``hang`` has to stop answering at a point of our choosing; both need byte-level control of
the frame. The SDK is still the source of truth for the *right* version, imported below.

The failure modes come from ``HARNESSED_STUB_MODE``: ``ok`` (the default), ``hang``, ``crash``,
``bad_version``, ``internal_error``, ``quota_error`` and ``grok_quota``. This is a test-support
child process, so it reads its own environment directly - the single-reader rule governs the
harness package, not the processes the harness spawns.

Every write is flushed immediately. A buffered notification that arrives only at exit reads to
the client as a protocol bug rather than as buffering, and ``crash`` mode in particular must
get its mid-flight update out before it dies.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION

__all__ = [
    "COVERAGE_VARS",
    "COVERAGE_VARS_SUFFIX",
    "DUMP_ENV_NAMES_VAR",
    "GRANDCHILD_VAR",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "METHOD_NOT_FOUND",
    "MODES",
    "PERMISSION_OPTIONS",
    "QUOTA_ERROR_CODE",
    "REGISTRY_VAR",
    "STOP_REASON",
    "WRONG_PROTOCOL_VERSION",
    "resolve_mode",
    "resolve_protocol_version",
    "session_id_for",
]

# The whole mode vocabulary the architecture names. Anything else is a typo, and a typo must be
# loud: a stub that silently degraded to `ok` would turn a mis-spelled failure-mode test into a
# green one asserting nothing.
QUOTA_ERROR_CODE = -32000
MODES = ("ok", "hang", "crash", "bad_version", "internal_error", "quota_error", "grok_quota")

# `bad_version` is shorthand for setting HARNESSED_STUB_PROTOCOL_VERSION to something wrong.
# Derived from the SDK's value so it can never accidentally become the right one.
WRONG_PROTOCOL_VERSION = PROTOCOL_VERSION + 1000

# JSON-RPC 2.0 reserved codes.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: The recorded refusal ``grok_quota`` mode replays (ticket 194).
GROK_QUOTA_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "acp" / "grok-quota-refusal.json"
)

STOP_REASON = "end_turn"
STUB_MESSAGE = "stub agent finished the delegated unit of work"
TOOL_TITLE = "stub tool call"

# The two options the permission matrix is exercised against. Order is part of the contract.
PERMISSION_OPTIONS = ("allow_once", "reject_once")

# Ticket 175. With ``--auth-methods`` on its command line, ``initialize`` advertises this login,
# the way an agent with a vendor login does; without it the list is empty, an agent saying nothing.
AUTH_METHODS = ({"id": "agent-login", "name": "Log in", "description": "the stub's own login"},)

# Ticket 090. A path here makes the stub start a grandchild that writes its own pid to that path
# and then sleeps, so a test can ask whether the agent's whole tree died rather than its root.
GRANDCHILD_VAR = "HARNESSED_STUB_GRANDCHILD"
GRANDCHILD_WAIT_S = 30.0
# Ticket 117. The variables that make pytest-cov's and coverage's ``.pth`` hooks start coverage in
# a child. The stub and its grandchild each write the ones they see next to the grandchild's pid
# file, so a test can prove neither started under coverage.
COVERAGE_VARS = (
    "COV_CORE_SOURCE",
    "COV_CORE_CONFIG",
    "COV_CORE_DATAFILE",
    "COVERAGE_PROCESS_START",
)
COVERAGE_VARS_SUFFIX = ".coverage-vars"
_GRANDCHILD_CODE = (
    "import json, os, sys, time\n"
    "target = sys.argv[1]\n"
    f"seen = [name for name in {COVERAGE_VARS!r} if name in os.environ]\n"
    f"with open(target + '.grandchild{COVERAGE_VARS_SUFFIX}', 'w') as handle:\n"
    "    json.dump(seen, handle)\n"
    "with open(target + '.tmp', 'w') as handle:\n"
    "    handle.write(str(os.getpid()))\n"
    "os.replace(target + '.tmp', target)\n"
    "time.sleep(600)\n"
)

# Ticket 090. A folder here gets one empty file per stub, named by its pid, so the test session
# that set it can find every stub it started. Not ``HARNESSED_STUB_*``: ``spawn_stub`` strips that
# prefix from the child's environment, and those stubs must register too.
REGISTRY_VAR = "HARNESSED_TEST_STUB_REGISTRY"

# Ticket 213. ``1`` makes the agent's reply carry the names of its own environment, so a test can
# prove which variables a delegation's child was given.
DUMP_ENV_NAMES_VAR = "HARNESSED_STUB_DUMP_ENV_NAMES"


def write_and_commit() -> None:
    """Ticket 244: ``HARNESSED_STUB_WRITE`` names a file written under the cwd, and
    ``HARNESSED_STUB_COMMIT=1`` commits it, so a test can read what the repository became."""
    name = os.environ.get("HARNESSED_STUB_WRITE", "").strip()
    if not name:
        return
    target = Path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("written by the stub\n", encoding="utf-8")
    if os.environ.get("HARNESSED_STUB_COMMIT", "").strip() == "1":
        identity = ["-c", "user.name=stub", "-c", "user.email=stub@example.invalid"]
        subprocess.run(["git", "add", "--", name], check=True, capture_output=True)
        subprocess.run(
            ["git", *identity, "commit", "-q", "-m", "stub change"],
            check=True,
            capture_output=True,
        )


def resolve_mode() -> str:
    """The failure mode this process runs in. Unset or empty means ``ok``; unknown is fatal."""
    mode = os.environ.get("HARNESSED_STUB_MODE", "").strip() or "ok"
    if mode not in MODES:
        raise ValueError(f"unknown HARNESSED_STUB_MODE {mode!r}; expected one of {MODES}")
    return mode


def resolve_protocol_version() -> int:
    """The version ``initialize`` answers with.

    Precedence is explicit variable, then the ``bad_version`` shorthand, then the SDK's value.
    The variable wins over the mode because the mode is only a shorthand for setting it.
    """
    raw = os.environ.get("HARNESSED_STUB_PROTOCOL_VERSION", "").strip()
    if raw:
        return int(raw)
    if resolve_mode() == "bad_version":
        return WRONG_PROTOCOL_VERSION
    return PROTOCOL_VERSION


def asks_permission() -> bool:
    """True when the stub should route its tool call through ``session/request_permission``."""
    return os.environ.get("HARNESSED_STUB_ASK_PERMISSION", "").strip() == "1"


def permission_tool() -> str | None:
    """The ACP tool kind the stub's permission request names (``HARNESSED_STUB_PERMISSION_TOOL``).

    Unset, the request carries only its title, which is what every pre-ticket-234 test relied on.
    """
    return os.environ.get("HARNESSED_STUB_PERMISSION_TOOL", "").strip() or None


def session_id_for(cwd: str) -> str:
    """A session id that is a pure function of the cwd, so two runs agree without shared state."""
    return "sess_" + hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]


def tool_call_id_for(session_id: str) -> str:
    """The one tool call this stub ever makes, named deterministically from its session."""
    return "tool_" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


class Stub:
    """One process, one stdio channel, one dispatch table."""

    def __init__(self, mode: str, protocol_version: int, ask_permission: bool) -> None:
        self.mode = mode
        self.protocol_version = protocol_version
        self.ask_permission = ask_permission
        self.sessions: set[str] = set()
        self.meta: dict[str, Any] | None = None
        self.next_id = 1

    # ---- the wire ----------------------------------------------------------------------

    def write(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        """A notification carries no id. That is the only thing distinguishing it from a call."""
        self.write({"jsonrpc": "2.0", "method": method, "params": params})

    def ask(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Make a request *of the client* and block until its answer comes back."""
        identity = self.next_id
        self.next_id += 1
        self.write({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
        for line in sys.stdin:
            if not line.strip():
                continue
            message: dict[str, Any] = json.loads(line)
            if message.get("id") == identity and "method" not in message:
                answer: dict[str, Any] = message.get("result", {})
                return answer
        return {}

    # ---- the methods -------------------------------------------------------------------

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
            },
            "authMethods": list(AUTH_METHODS) if "--auth-methods" in sys.argv[1:] else [],
        }

    def session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise StubError(INVALID_PARAMS, "session/new needs a cwd")
        session_id = session_id_for(cwd)
        self.sessions.add(session_id)
        # Ticket 213: what ``session/new`` carried under ``_meta``, echoed in the agent message.
        meta = params.get("_meta")
        self.meta = meta if isinstance(meta, dict) else None
        return {"sessionId": session_id}

    def session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        if session_id not in self.sessions:
            raise StubError(INVALID_PARAMS, f"unknown session {session_id!r}")
        if self.mode == "internal_error":
            # Grok's observed shape (decision 69, FR-93): a bare JSON-RPC "Internal error", none
            # of the three named failures delegate.py used to catch.
            raise StubError(INTERNAL_ERROR, "Internal error")
        if self.mode == "quota_error":
            # Ticket 229: a fixture of the shape (code, message, data), not a claim about any
            # vendor's. ``HARNESSED_STUB_ERROR_NOTE`` lets a test put text, a secret say, in the
            # message.
            note = os.environ.get("HARNESSED_STUB_ERROR_NOTE", "")
            raise StubError(
                QUOTA_ERROR_CODE,
                f"Usage limit reached{note}",
                {"retry_after": "2026-10-04T00:00:00Z"},
            )
        if self.mode == "grok_quota":
            # Ticket 194: the operator's recorded refusal, replayed byte for byte.
            recorded = json.loads(GROK_QUOTA_FIXTURE.read_text(encoding="utf-8"))
            raise StubError(recorded["code"], recorded["message"], recorded["data"])
        tool_call_id = tool_call_id_for(str(session_id))

        self.notify(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_call_id,
                    "title": TOOL_TITLE,
                    "kind": "other",
                    "status": "pending",
                    "rawInput": {},
                },
            },
        )

        if self.mode == "crash":
            # Mid-flight, after the first update is already on the wire and flushed.
            sys.exit(3)

        # HARNESSED_STUB_WORK_S: the stub "works" this long after announcing its tool call, then
        # reports the call finished. It is what lets a test tell a step published live from one
        # flushed at the end.
        work_s = float(os.environ.get("HARNESSED_STUB_WORK_S", "").strip() or "0")
        if work_s > 0:
            time.sleep(work_s)
            self.notify(
                "session/update",
                {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tool_call_id,
                        "status": "completed",
                    },
                },
            )

        option_id: str | None = None
        if self.ask_permission:
            outcome = self.ask(
                "session/request_permission",
                {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": tool_call_id,
                        "title": TOOL_TITLE,
                        "rawInput": {"command": "ls"},
                        **({} if permission_tool() is None else {"kind": permission_tool()}),
                    },
                    "options": [
                        {"optionId": name, "name": name.replace("_", " "), "kind": name}
                        for name in PERMISSION_OPTIONS
                    ],
                },
            )
            option_id = outcome.get("outcome", {}).get("optionId")

        write_and_commit()
        self.notify(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": self.message()},
                },
            },
        )
        result: dict[str, Any] = {"stopReason": STOP_REASON}
        if self.ask_permission:
            # Echoed so a test can prove the answer was read off the wire, not assumed.
            result["_meta"] = {"permissionOptionId": option_id}
        return result

    def message(self) -> str:
        """The agent's reply: the fixed line, or JSON of what ticket 213's tests ask it to echo."""
        echo: dict[str, Any] = {}
        if self.meta is not None:
            echo["meta"] = self.meta
        if os.environ.get(DUMP_ENV_NAMES_VAR, "").strip() == "1":
            echo["env_names"] = sorted(os.environ)
            echo["env_stub"] = {
                k: v for k, v in os.environ.items() if k.startswith("HARNESSED_STUB_")
            }
        return json.dumps(echo, sort_keys=True) if echo else STUB_MESSAGE

    # ---- the loop ----------------------------------------------------------------------

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "initialize": self.initialize,
            "session/new": self.session_new,
            "session/prompt": self.session_prompt,
        }
        handler = handlers.get(method)
        if handler is None:
            raise StubError(METHOD_NOT_FOUND, f"unknown method {method!r}")
        return handler(params)

    def serve(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            message: dict[str, Any] = json.loads(line)
            if "method" not in message:
                # A stray response to a request we are no longer waiting on. Nothing to do.
                continue
            identity = message.get("id")
            try:
                result = self.dispatch(message["method"], message.get("params") or {})
            except StubError as error:
                self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": identity,
                        "error": {
                            "code": error.code,
                            "message": error.message,
                            **({} if error.data is None else {"data": error.data}),
                        },
                    }
                )
                continue
            if identity is not None:
                self.write({"jsonrpc": "2.0", "id": identity, "result": result})


class StubError(Exception):
    """A JSON-RPC error the stub answers with instead of dying."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def hang_forever() -> None:
    """Never answer anything, and stay alive so the process-tree kill has something to kill."""
    threading.Event().wait()


def register() -> None:
    """Leave this process's pid where the test session that started it will look."""
    folder = os.environ.get(REGISTRY_VAR, "").strip()
    if folder:
        (Path(folder) / str(os.getpid())).touch()


def start_grandchild() -> None:
    """Start the grandchild and wait until it has written its pid, so a test can always read it."""
    target = os.environ.get(GRANDCHILD_VAR, "").strip()
    if not target:
        return
    seen = [name for name in COVERAGE_VARS if name in os.environ]
    Path(target + ".stub" + COVERAGE_VARS_SUFFIX).write_text(json.dumps(seen), encoding="utf-8")
    subprocess.Popen(
        [sys.executable, "-c", _GRANDCHILD_CODE, target],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + GRANDCHILD_WAIT_S
    while not Path(target).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def main() -> None:
    # The parent talks UTF-8 on both pipes; Windows would otherwise pick the console codepage.
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    register()
    start_grandchild()
    mode = resolve_mode()
    if mode == "hang":
        hang_forever()
    Stub(
        mode=mode,
        protocol_version=resolve_protocol_version(),
        ask_permission=asks_permission(),
    ).serve()


if __name__ == "__main__":
    main()
