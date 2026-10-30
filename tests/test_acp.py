"""The stub ACP agent's own protocol surface (architecture open question 11).

Every test here spawns ``tests/support/stub_acp_agent.py`` as a **real child process** via
``sys.executable`` and talks newline-delimited JSON-RPC to it over a real pipe. Nothing is
mocked, because the assertions this file exists to make possible - FR-22's "a protocol version
mismatch fails loudly at ``initialize``" and FR-23's "the agent is spawned exactly once" - are
meaningless against a mock object. Ticket 021 extends this file with the FR-22 assertions
themselves; this ticket proves the far side of the wire behaves as the architecture says.

Two shapes carry the whole file. ``StubProcess`` is the client half: a reader thread drains the
child's stdout onto a queue so every read has a timeout, because a stub bug must fail a test in
seconds rather than wedge the suite. ``spawn_stub`` is the context manager that owns the child's
lifetime and kills the whole process tree in its ``finally``, for every mode and not just
``hang``, so a hung stub can never leak into the next test.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from acp import PROTOCOL_VERSION

from harnessed import config, proc
from harnessed.acp import cli_runner, permissions
from harnessed.acp import config as acp_config
from harnessed.acp import delegate as acp_delegate
from harnessed.acp import runtime as acp_runtime
from harnessed.errors import (
    AcpVersionMismatch,
    AgentNotFoundError,
    ConfigError,
    DelegationError,
    DelegationTimeout,
    QuotaRefused,
)
from harnessed.events.bus import EventBus
from harnessed.memory import db, episodic
from harnessed.providers.types import ToolCall
from harnessed.tools import default_registry
from harnessed.tools.builtin import delegate as delegate_tool
from support import stub_acp_agent as stub
from support.spies import RecordingObserver, SpawnSpy, watch_spawns

REPO_ROOT = Path(__file__).resolve().parents[1]
STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_acp_agent.py"

# Ticket 117: under `--cov` a stub agent that inherits coverage's variables starts too slowly.
pytestmark = pytest.mark.usefixtures("no_child_coverage")

# Every read is bounded. Generous enough for a cold interpreter start on Windows, short enough
# that the hang test costs about a second rather than a minute.
READ_TIMEOUT = 10.0
HANG_TIMEOUT = 1.5


class StubTimeout(RuntimeError):
    """The stub produced no message within the deadline. ``hang`` mode's expected outcome."""


@dataclass
class StubProcess:
    """A live stub child plus the primitives a test needs to talk to it.

    ``send`` and ``read_message`` are deliberately separate rather than a single blocking
    ``request``: with ``HARNESSED_STUB_ASK_PERMISSION=1`` the traffic between a ``session/prompt``
    and its response is a notification, an inbound *request* from the agent, and another
    notification, so a helper that assumed the next message was the response would be wrong for
    exactly the case the permission matrix is about. ``request`` exists only for the two
    handshake calls whose next message really is their answer.
    """

    process: subprocess.Popen[str]
    inbox: queue.Queue[str | None]
    next_id: int = 1
    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.sent.append(message)
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method: str, params: dict[str, Any]) -> int:
        """Write a request, return the id it was written under."""
        identity = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
        return identity

    def respond(self, identity: int, result: dict[str, Any]) -> None:
        """Answer a request the *agent* made of us."""
        self.send({"jsonrpc": "2.0", "id": identity, "result": result})

    def read_message(self, timeout: float = READ_TIMEOUT) -> dict[str, Any]:
        try:
            line = self.inbox.get(timeout=timeout)
        except queue.Empty:
            raise StubTimeout(f"no message within {timeout}s") from None
        if line is None:
            raise StubTimeout("the stub closed its stdout")
        parsed: dict[str, Any] = json.loads(line)
        return parsed

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call and read the answer. Safe only where nothing interleaves."""
        identity = self.call(method, params)
        message = self.read_message()
        assert message["id"] == identity, message
        return message

    def drain_until_response(self, identity: int) -> list[dict[str, Any]]:
        """Every message up to and including the response to ``identity``, in arrival order."""
        seen: list[dict[str, Any]] = []
        while True:
            message = self.read_message()
            seen.append(message)
            if message.get("id") == identity and "method" not in message:
                return seen


def _drain(handle: Any, inbox: queue.Queue[str | None]) -> None:
    """Push every line the child writes onto the queue, then a ``None`` sentinel at EOF."""
    for line in handle:
        if line.strip():
            inbox.put(line)
    inbox.put(None)


def _kill_tree(process: subprocess.Popen[str]) -> None:
    """Kill the child and all under it. ``taskkill /T /F`` on Windows, the group elsewhere."""
    if process.poll() is None:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(process.pid, 9)
    try:
        process.wait(timeout=READ_TIMEOUT)
    except subprocess.TimeoutExpired:  # pragma: no cover - the kill above is unconditional
        process.kill()


def stub_env(**env: str) -> dict[str, str]:
    """This process's environment without ``HARNESSED_STUB_*`` or coverage's hooks, plus ``env``."""
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HARNESSED_STUB_") and key not in stub.COVERAGE_VARS
    }
    child_env.update(env)
    return child_env


@contextmanager
def spawn_stub(**env: str) -> Iterator[StubProcess]:
    """Spawn the stub with exactly ``env`` on top of a minimal environment, and always kill it.

    The environment is built explicitly rather than inherited-and-patched: ``HARNESSED_STUB_*``
    is not one of conftest's documented variables, so a developer's shell must not be able to
    change what a test observes.
    """
    child_env = stub_env(**env)
    extra: dict[str, Any] = {} if sys.platform == "win32" else {"start_new_session": True}
    process = subprocess.Popen(
        [sys.executable, str(STUB_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=child_env,
        cwd=str(REPO_ROOT),
        **extra,
    )
    inbox: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(target=_drain, args=(process.stdout, inbox), daemon=True)
    reader.start()
    try:
        yield StubProcess(process=process, inbox=inbox)
    finally:
        _kill_tree(process)
        reader.join(timeout=READ_TIMEOUT)


def initialize(client: StubProcess) -> dict[str, Any]:
    """Send the handshake, return its result object."""
    message = client.request(
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}},
    )
    result: dict[str, Any] = message["result"]
    return result


def new_session(client: StubProcess, cwd: str) -> str:
    """Handshake, open a session on ``cwd``, return the session id."""
    initialize(client)
    message = client.request("session/new", {"cwd": cwd, "mcpServers": []})
    session_id: str = message["result"]["sessionId"]
    return session_id


# ---- initialize ------------------------------------------------------------------------------


def test_initialize_returns_the_sdk_protocol_version_and_capabilities() -> None:
    with spawn_stub() as client:
        result = initialize(client)
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert isinstance(result["agentCapabilities"], dict)


def test_initialize_honours_the_protocol_version_override() -> None:
    with spawn_stub(HARNESSED_STUB_PROTOCOL_VERSION="4242") as client:
        result = initialize(client)
    assert result["protocolVersion"] == 4242


def test_bad_version_mode_returns_a_wrong_protocol_version() -> None:
    with spawn_stub(HARNESSED_STUB_MODE="bad_version") as client:
        result = initialize(client)
    assert result["protocolVersion"] == stub.WRONG_PROTOCOL_VERSION
    assert result["protocolVersion"] != PROTOCOL_VERSION


def test_the_explicit_override_beats_the_bad_version_shorthand() -> None:
    """``bad_version`` is shorthand for setting the variable, so the variable wins when set."""
    with spawn_stub(
        HARNESSED_STUB_MODE="bad_version", HARNESSED_STUB_PROTOCOL_VERSION="7"
    ) as client:
        result = initialize(client)
    assert result["protocolVersion"] == 7


def test_internal_error_mode_answers_the_prompt_with_a_bare_json_rpc_error() -> None:
    """Grok's observed shape: no exception the client's own three named errors would catch."""
    with spawn_stub(HARNESSED_STUB_MODE="internal_error") as client:
        session_id = new_session(client, cwd=str(REPO_ROOT))
        message = client.request("session/prompt", {"sessionId": session_id, "prompt": []})
    assert message["error"]["code"] == stub.INTERNAL_ERROR
    assert message["error"]["message"] == "Internal error"


def test_the_response_is_a_well_formed_json_rpc_envelope() -> None:
    with spawn_stub() as client:
        identity = client.call("initialize", {"protocolVersion": PROTOCOL_VERSION})
        message = client.read_message()
    assert message["jsonrpc"] == "2.0"
    assert message["id"] == identity
    assert "error" not in message


def test_an_unknown_method_is_a_json_rpc_error_not_a_crash() -> None:
    with spawn_stub() as client:
        initialize(client)
        message = client.request("session/teleport", {})
        assert message["error"]["code"] == stub.METHOD_NOT_FOUND
        # The stub is still alive and still answering afterwards.
        assert initialize(client)["protocolVersion"] == PROTOCOL_VERSION


# ---- session/new -----------------------------------------------------------------------------


def test_session_new_is_deterministic_for_a_cwd() -> None:
    with spawn_stub() as first:
        one = new_session(first, "/work/alpha")
    with spawn_stub() as second:
        two = new_session(second, "/work/alpha")
    assert one == two


def test_session_new_separates_two_cwds() -> None:
    with spawn_stub() as client:
        initialize(client)
        alpha = client.request("session/new", {"cwd": "/work/alpha", "mcpServers": []})
        beta = client.request("session/new", {"cwd": "/work/beta", "mcpServers": []})
    assert alpha["result"]["sessionId"] != beta["result"]["sessionId"]


# ---- session/prompt --------------------------------------------------------------------------


def test_prompt_emits_a_tool_call_then_a_message_chunk_then_a_stop_reason() -> None:
    with spawn_stub() as client:
        session_id = new_session(client, "/work/alpha")
        identity = client.call(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "do the thing"}]},
        )
        seen = client.drain_until_response(identity)

    notifications = [message for message in seen if message.get("method") == "session/update"]
    assert len(notifications) == 2
    kinds = [message["params"]["update"]["sessionUpdate"] for message in notifications]
    assert kinds == ["tool_call", "agent_message_chunk"]
    assert all(message["params"]["sessionId"] == session_id for message in notifications)
    assert notifications[1]["params"]["update"]["content"]["type"] == "text"
    assert seen[-1]["result"]["stopReason"] == stub.STOP_REASON


def test_a_notification_carries_no_id() -> None:
    with spawn_stub() as client:
        session_id = new_session(client, "/work/alpha")
        identity = client.call("session/prompt", {"sessionId": session_id, "prompt": []})
        seen = client.drain_until_response(identity)
    assert all("id" not in message for message in seen if "method" in message)


def test_prompting_an_unknown_session_is_an_error() -> None:
    with spawn_stub() as client:
        initialize(client)
        message = client.request("session/prompt", {"sessionId": "nope", "prompt": []})
    assert message["error"]["code"] == stub.INVALID_PARAMS


# ---- session/request_permission --------------------------------------------------------------


def test_permission_is_requested_over_the_real_channel_when_asked() -> None:
    with spawn_stub(HARNESSED_STUB_ASK_PERMISSION="1") as client:
        session_id = new_session(client, "/work/alpha")
        identity = client.call("session/prompt", {"sessionId": session_id, "prompt": []})

        first = client.read_message()
        assert first["params"]["update"]["sessionUpdate"] == "tool_call"

        ask = client.read_message()
        assert ask["method"] == "session/request_permission"
        assert ask["params"]["sessionId"] == session_id
        assert ask["params"]["toolCall"]["toolCallId"] == first["params"]["update"]["toolCallId"]
        assert [option["optionId"] for option in ask["params"]["options"]] == list(
            stub.PERMISSION_OPTIONS
        )

        client.respond(ask["id"], {"outcome": {"outcome": "selected", "optionId": "allow_once"}})
        rest = client.drain_until_response(identity)

    assert rest[0]["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert rest[-1]["result"]["stopReason"] == stub.STOP_REASON


def test_the_granted_option_is_echoed_back_in_the_stop_result() -> None:
    """The stub really reads our answer off the wire rather than assuming one."""
    with spawn_stub(HARNESSED_STUB_ASK_PERMISSION="1") as client:
        session_id = new_session(client, "/work/alpha")
        identity = client.call("session/prompt", {"sessionId": session_id, "prompt": []})
        client.read_message()
        ask = client.read_message()
        client.respond(ask["id"], {"outcome": {"outcome": "selected", "optionId": "reject_once"}})
        seen = client.drain_until_response(identity)
    assert seen[-1]["result"]["_meta"]["permissionOptionId"] == "reject_once"


def test_permission_is_not_requested_by_default() -> None:
    with spawn_stub() as client:
        session_id = new_session(client, "/work/alpha")
        identity = client.call("session/prompt", {"sessionId": session_id, "prompt": []})
        seen = client.drain_until_response(identity)
    asked = [message for message in seen if message.get("method") == "session/request_permission"]
    assert asked == []


# ---- failure modes ---------------------------------------------------------------------------


def test_hang_mode_never_responds_and_the_tree_kill_ends_it() -> None:
    with spawn_stub(HARNESSED_STUB_MODE="hang") as client:
        client.call("initialize", {"protocolVersion": PROTOCOL_VERSION})
        with pytest.raises(StubTimeout):
            client.read_message(timeout=HANG_TIMEOUT)
        assert client.process.poll() is None, "the stub should still be alive, just silent"
        process = client.process
    assert process.poll() is not None, "teardown must leave no stub behind"


def test_crash_mode_exits_non_zero_mid_session() -> None:
    with spawn_stub(HARNESSED_STUB_MODE="crash") as client:
        session_id = new_session(client, "/work/alpha")
        client.call("session/prompt", {"sessionId": session_id, "prompt": []})
        first = client.read_message()
        assert first["params"]["update"]["sessionUpdate"] == "tool_call"
        returncode = client.process.wait(timeout=READ_TIMEOUT)
    assert returncode != 0


def test_the_handshake_still_works_in_crash_mode() -> None:
    """``crash`` is mid-session, so ``initialize`` and ``session/new`` must both succeed first."""
    with spawn_stub(HARNESSED_STUB_MODE="crash") as client:
        assert new_session(client, "/work/alpha")
        assert client.process.poll() is None


# ---- the mode vocabulary ---------------------------------------------------------------------


def test_the_default_mode_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESSED_STUB_MODE", raising=False)
    assert stub.resolve_mode() == "ok"
    assert "ok" in stub.MODES


def test_an_empty_mode_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "")
    assert stub.resolve_mode() == "ok"


def test_the_mode_vocabulary_is_exactly_the_seven_the_architecture_names() -> None:
    assert set(stub.MODES) == {
        "ok",
        "hang",
        "crash",
        "bad_version",
        "internal_error",
        "quota_error",
        "grok_quota",
    }


def test_an_unknown_mode_is_loud_not_silently_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "sulk")
    with pytest.raises(ValueError, match="sulk"):
        stub.resolve_mode()


def test_an_unknown_mode_kills_the_child_before_it_answers_anything() -> None:
    with spawn_stub(HARNESSED_STUB_MODE="sulk") as client:
        assert client.process.wait(timeout=READ_TIMEOUT) != 0
        with pytest.raises(StubTimeout):
            client.read_message(timeout=HANG_TIMEOUT)


def test_the_session_id_is_a_pure_function_of_the_cwd() -> None:
    assert stub.session_id_for("/work/alpha") == stub.session_id_for("/work/alpha")
    assert stub.session_id_for("/work/alpha") != stub.session_id_for("/work/beta")
    assert stub.session_id_for("/work/alpha").startswith("sess_")


# ---- the spawn spy (FR-23's counter) ----------------------------------------------------------


def test_the_spawn_spy_counts_real_process_creations() -> None:
    spy = SpawnSpy()
    with spy.installed(subprocess, "Popen"):
        with spawn_stub() as first:
            initialize(first)
        with spawn_stub() as second:
            initialize(second)
    # Teardown's process-tree kill is itself a spawn on Windows, so the claim is about the
    # command line, not the raw total: two stub processes, not one reused.
    assert len(spy.matching(str(STUB_PATH))) == 2
    assert spy.count >= 2


def test_the_spawn_spy_starts_at_zero_and_leaves_popen_restored() -> None:
    spy = SpawnSpy()
    original = subprocess.Popen
    assert spy.count == 0
    with spy.installed(subprocess, "Popen"):
        assert subprocess.Popen is spy
    assert subprocess.Popen is original


# ==============================================================================================
# Ticket 021 - FR-22: exactly one swappable ACP delegation tool
# ==============================================================================================
#
# Everything above this line proves the far side of the wire behaves as the architecture says.
# Everything below proves the harness side: one tool, one protocol, a permission matrix that
# fails closed, and a summary the parent model can afford to read.
#
# The delegation tests still spawn the same stub as a real child process - now through
# ``acp/delegate.py`` rather than through ``spawn_stub`` - because a mocked handshake cannot
# make the version-mismatch or process-tree-kill assertions mean anything either.

PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
ACP_ROOT = PACKAGE_ROOT / "acp"
EXAMPLE_AGENTS = REPO_ROOT / "agents.toml.example"

# A timeout short enough that `hang` costs about a second, long enough that a cold interpreter
# start on Windows is not mistaken for a hang on the happy path.
SHORT_TIMEOUT = 1.5


def write_agents(path: Path, body: str) -> Path:
    """Write an ``agents.toml`` and return its path."""
    target = path / "agents.toml"
    target.write_text(body, encoding="utf-8")
    return target


def stub_agents(path: Path, **fields: str) -> Path:
    """An ``agents.toml`` whose one agent is the stub, spawned through this interpreter.

    ``fields`` are TOML value literals and *replace* the defaults rather than adding to them: a
    duplicate key is a TOML error, so an override has to actually override.
    """
    declared = {
        "command": json.dumps(sys.executable),
        "args": f"[{json.dumps(str(STUB_PATH))}]",
        "install": '"install the probe agent first"',
        "timeout_s": str(READ_TIMEOUT),
    }
    declared.update(fields)
    lines = ["[agents.probe]"] + [f"{key} = {value}" for key, value in declared.items()]
    return write_agents(path, "\n".join(lines) + "\n")


def probe_spec(path: Path, **fields: str) -> acp_config.AgentSpec:
    """The stub agent, loaded through the real loader so no test builds a spec by hand."""
    return acp_config.agent_named(stub_agents(path, **fields), None)


# ---- 1. exactly one tool, and it is named delegate_to_agent -----------------------------------


def test_the_acp_tools_are_exactly_one_named_delegate_to_agent(tmp_path: Path) -> None:
    declared = delegate_tool.tools(tmp_path)
    assert [tool.name for tool in declared] == ["delegate_to_agent"]
    assert delegate_tool.TOOL_NAME == "delegate_to_agent"


def test_the_default_registry_carries_the_one_delegation_tool(tmp_path: Path) -> None:
    bench = default_registry(tmp_path)
    assert "delegate_to_agent" in bench.names()
    acp = {tool.name for tool in delegate_tool.tools(tmp_path)}
    acp_names = [name for name in bench.names() if name in acp]
    assert acp_names == ["delegate_to_agent"]


def test_the_tool_declares_a_unit_of_work_and_not_a_session_api(tmp_path: Path) -> None:
    """FR-22: the model hands off a unit of work; it never drives the session itself."""
    (tool,) = delegate_tool.tools(tmp_path)
    properties = tool.parameters["properties"]
    assert set(properties) == {"agent", "task", "acceptance"}
    assert tool.parameters["required"] == ["task", "acceptance"]
    assert tool.parameters["additionalProperties"] is False
    # Nothing in the declaration lets the model address a session, a prompt turn or a permission.
    assert not {"session_id", "prompt", "permission"} & set(properties)


# ---- 2. no agent CLI name is hardcoded --------------------------------------------------------

# Assembled from fragments so this file can name what it forbids without becoming an offender in
# some other scan, and kept here because this module *declares* the vocabulary. The scan below
# exempts exactly this file for the same reason ticket 010 exempted the module that declares
# `parse_error`: a declaration is not a use.
#
# One agent's name is deliberately absent: it is also the ordinary English word for a database
# handle and a text caret, so scanning for it would report a `sqlite3` cursor as a hardcoded CLI.
# The vocabulary keeps the names that can only mean an agent.
FORBIDDEN_CLI_NAMES = (
    "clau" + "de-code",
    "cod" + "ex",
    "opencl" + "aw",
    "gem" + "ini",
    "ai" + "der",
)


def cli_name_offenders(paths: Iterable[Path], root: Path) -> list[str]:
    """Every scanned file naming a specific coding-agent CLI, relative to ``root``."""
    found = []
    for path in paths:
        body = path.read_text(encoding="utf-8").lower()
        if any(name in body for name in FORBIDDEN_CLI_NAMES):
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


#: The one module the architecture sanctions as a place an agent may be named: ``acp/config.py``
#: carries the ``agents.toml`` template table FR-29 adds, and a template is data an operator
#: copies, not a branch the source takes. Everything else in the package stays silent, which is
#: still the rule this scan exists for. ``tests/test_agents_onboarding.py`` scans the same tree
#: from the other side, against the names the table actually holds.
TEMPLATE_MODULE = PACKAGE_ROOT / "acp" / "config.py"


def test_no_specific_agent_cli_is_named_anywhere_in_the_package() -> None:
    """The bug this package exists to prevent: a hardcoded agent (``acp.md``, "Never")."""
    scanned = (path for path in PACKAGE_ROOT.rglob("*.py") if path != TEMPLATE_MODULE)
    assert cli_name_offenders(scanned, REPO_ROOT) == []


def test_the_template_table_is_the_only_module_the_scan_forgives() -> None:
    """The carve-out is one file, by path. Widening it would empty the rule above."""
    assert cli_name_offenders([TEMPLATE_MODULE], REPO_ROOT) == ["src/harnessed/acp/config.py"]


def test_no_specific_agent_cli_is_named_in_the_tests_either() -> None:
    """Excluding this file, which declares the vocabulary. A declaration is not a use."""
    scanned = (
        path for path in (REPO_ROOT / "tests").rglob("*.py") if path != Path(__file__).resolve()
    )
    assert cli_name_offenders(scanned, REPO_ROOT) == []


def test_the_scan_catches_a_synthetic_offender(tmp_path: Path) -> None:
    """Without this the two assertions above are green against source that was already clean."""
    innocent = tmp_path / "swappable.py"
    innocent.write_text("COMMAND = spec.command\n", encoding="utf-8")
    offender = tmp_path / "hardcoded.py"
    offender.write_text(f'AGENT = "{FORBIDDEN_CLI_NAMES[0]}"\n', encoding="utf-8")
    assert cli_name_offenders([innocent, offender], tmp_path) == ["hardcoded.py"]


def test_the_example_agents_file_is_the_committed_half_of_the_pair() -> None:
    """``agents.toml`` is gitignored; its example is what ships, and it is what names commands."""
    assert EXAMPLE_AGENTS.is_file()
    assert "command" in EXAMPLE_AGENTS.read_text(encoding="utf-8")


# ---- agents.toml: the registry that makes the agent swappable ---------------------------------


def test_the_committed_example_loads_through_the_real_loader() -> None:
    """The example is documentation only if it parses. It does."""
    agents = acp_config.load_agents(EXAMPLE_AGENTS)
    assert agents
    for spec in agents.values():
        assert spec.command
        assert spec.mode in acp_config.MODES


def test_an_agent_carries_its_command_args_lists_and_ceilings(tmp_path: Path) -> None:
    path = write_agents(
        tmp_path,
        "\n".join(
            [
                'default = "worker"',
                "[agents.worker]",
                'command = "the-configured-binary"',
                'args = ["serve", "--acp"]',
                'cwd = "sub"',
                'mode = "allowlist"',
                'allow = ["read"]',
                'deny = ["execute"]',
                "timeout_s = 12.5",
                "ask_timeout_s = 3",
                'install = "the exact install command"',
            ]
        )
        + "\n",
    )
    spec = acp_config.agent_named(path, "worker")
    assert spec.name == "worker"
    assert spec.command == "the-configured-binary"
    assert spec.args == ("serve", "--acp")
    assert spec.cwd == "sub"
    assert spec.mode == "allowlist"
    assert spec.allow == ("read",)
    assert spec.deny == ("execute",)
    assert spec.timeout_s == 12.5
    assert spec.ask_timeout_s == 3.0
    assert spec.install == "the exact install command"


def test_the_declared_default_is_what_an_unnamed_delegation_gets(tmp_path: Path) -> None:
    path = write_agents(
        tmp_path,
        'default = "second"\n[agents.first]\ncommand = "a"\n[agents.second]\ncommand = "b"\n',
    )
    assert acp_config.agent_named(path, None).name == "second"


def test_a_single_agent_needs_no_default(tmp_path: Path) -> None:
    path = write_agents(tmp_path, '[agents.only]\ncommand = "a"\n')
    assert acp_config.agent_named(path, None).name == "only"


def test_two_agents_and_no_default_is_an_error_naming_the_choices(tmp_path: Path) -> None:
    path = write_agents(tmp_path, '[agents.first]\ncommand = "a"\n[agents.second]\ncommand = "b"\n')
    with pytest.raises(ConfigError) as caught:
        acp_config.agent_named(path, None)
    assert "first" in str(caught.value) and "second" in str(caught.value)


def test_an_unknown_agent_names_the_ones_that_are_declared(tmp_path: Path) -> None:
    path = write_agents(tmp_path, '[agents.only]\ncommand = "a"\n')
    with pytest.raises(AgentNotFoundError) as caught:
        acp_config.agent_named(path, "ghost")
    assert "ghost" in str(caught.value)
    assert "only" in str(caught.value)


def test_a_missing_agents_file_names_the_path_and_the_fix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        acp_config.load_agents(tmp_path / "agents.toml")
    message = str(caught.value)
    assert "agents.toml" in message
    assert EXAMPLE_AGENTS.name in message


def test_an_agent_without_a_command_is_rejected_at_load(tmp_path: Path) -> None:
    path = write_agents(tmp_path, '[agents.broken]\nargs = ["x"]\n')
    with pytest.raises(ConfigError, match="command"):
        acp_config.load_agents(path)


def test_an_unknown_permission_mode_is_rejected_at_load(tmp_path: Path) -> None:
    path = write_agents(tmp_path, '[agents.broken]\ncommand = "a"\nmode = "vibes"\n')
    with pytest.raises(ConfigError, match="vibes"):
        acp_config.load_agents(path)


def test_an_unknown_key_is_a_typo_and_fails_loudly(tmp_path: Path) -> None:
    path = write_agents(tmp_path, '[agents.broken]\ncommand = "a"\ntimeout = 5\n')
    with pytest.raises(ConfigError, match="timeout"):
        acp_config.load_agents(path)


def test_a_file_declaring_no_agents_is_an_error(tmp_path: Path) -> None:
    path = write_agents(tmp_path, 'default = "nobody"\n')
    with pytest.raises(ConfigError):
        acp_config.load_agents(path)


def test_a_spec_yields_the_policy_the_matrix_reads(tmp_path: Path) -> None:
    body = '[agents.a]\ncommand = "c"\nmode = "ask"\nallow = ["read"]\ndeny = ["edit"]\n'
    spec = acp_config.agent_named(write_agents(tmp_path, body), "a")
    policy = spec.policy(interactive=True)
    assert (policy.mode, policy.allow, policy.deny, policy.interactive) == (
        "ask",
        ("read",),
        ("edit",),
        True,
    )


# ==============================================================================================
# Ticket 157 - FR-98: a registry entry carries kind, use_for, not_for and billing
# ==============================================================================================


def test_a_v4_entry_with_none_of_the_new_fields_loads_unchanged_as_kind_acp(tmp_path: Path) -> None:
    """AC 1: an entry declaring none of the new keys still loads, defaulting to acp/not metered."""
    path = write_agents(tmp_path, '[agents.worker]\ncommand = "a"\n')
    spec = acp_config.agent_named(path, "worker")
    assert spec.kind == "acp"
    assert spec.use_for == ""
    assert spec.not_for == ""
    assert spec.billing is None
    assert spec.produces is None
    assert spec.outputs is None
    assert spec.floor is None
    assert spec.base_url_var is None
    assert spec.key_var is None
    assert spec.input == "argv"
    assert spec.allow_write is False


def test_an_unknown_kind_is_rejected_at_load_naming_the_entry(tmp_path: Path) -> None:
    path = write_agents(tmp_path, '[agents.broken]\ncommand = "a"\nkind = "x"\n')
    with pytest.raises(ConfigError) as caught:
        acp_config.load_agents(path)
    assert "broken" in str(caught.value)
    assert "x" in str(caught.value)


def test_an_unknown_billing_is_rejected_at_load_naming_the_entry(tmp_path: Path) -> None:
    path = write_agents(tmp_path, '[agents.broken]\ncommand = "a"\nbilling = "x"\n')
    with pytest.raises(ConfigError) as caught:
        acp_config.load_agents(path)
    assert "broken" in str(caught.value)
    assert "x" in str(caught.value)


@pytest.mark.parametrize("floor", [0.0, 0.49, 0.999, 1.0])
def test_a_floor_outside_the_valid_range_is_rejected_at_load_naming_the_entry(
    floor: float, tmp_path: Path
) -> None:
    path = write_agents(tmp_path, f'[agents.broken]\ncommand = "a"\nfloor = {floor}\n')
    with pytest.raises(ConfigError) as caught:
        acp_config.load_agents(path)
    assert "broken" in str(caught.value)


def test_a_floor_inside_the_valid_range_loads_and_is_stored(tmp_path: Path) -> None:
    path = write_agents(tmp_path, '[agents.worker]\ncommand = "a"\nfloor = 0.85\n')
    assert acp_config.agent_named(path, "worker").floor == 0.85


def test_a_cli_entry_carries_its_new_fields(tmp_path: Path) -> None:
    path = write_agents(
        tmp_path,
        "\n".join(
            [
                "[agents.worker]",
                'command = "a"',
                'kind = "cli"',
                'use_for = "quick local edits"',
                'not_for = "anything that touches a secret"',
                'produces = "code"',
                'outputs = "*.patch"',
                "floor = 0.9",
                'input = "stdin"',
                "allow_write = true",
            ]
        )
        + "\n",
    )
    spec = acp_config.agent_named(path, "worker")
    assert spec.kind == "cli"
    assert spec.use_for == "quick local edits"
    assert spec.not_for == "anything that touches a secret"
    assert spec.produces == "code"
    assert spec.outputs == "*.patch"
    assert spec.floor == 0.9
    assert spec.input == "stdin"
    assert spec.allow_write is True


#: A believable OpenRouter key shape, assembled from fragments so no literal here looks like a
#: real one (ticket 008's lesson): "sk-or-v1-" is the vendor's real prefix.
SEEDED_OPENROUTER_KEY = "sk-" + "or-v1-" + "f" * 48


def test_an_openrouter_entry_declares_variable_names_never_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3: base_url_var/key_var name the variables the harness reads; the value never appears,
    even when the operator's real key is sitting in the environment under that name."""
    monkeypatch.setenv("OPENROUTER_API_KEY", SEEDED_OPENROUTER_KEY)
    path = write_agents(
        tmp_path,
        "\n".join(
            [
                "[agents.pi]",
                'command = "npx"',
                'args = ["-y", "pi-acp"]',
                'billing = "openrouter"',
                'base_url_var = "OPENROUTER_BASE_URL"',
                'key_var = "OPENROUTER_API_KEY"',
            ]
        )
        + "\n",
    )
    rendered = path.read_text(encoding="utf-8")
    assert rendered.count(SEEDED_OPENROUTER_KEY) == 0

    spec = acp_config.agent_named(path, "pi")
    assert spec.billing == "openrouter"
    assert spec.base_url_var == "OPENROUTER_BASE_URL"
    assert spec.key_var == "OPENROUTER_API_KEY"


def test_two_entries_and_no_default_still_run_when_one_is_named(tmp_path: Path) -> None:
    """AC 4: known defect (d) is moot - code delegates by entry name, never an implicit default."""
    path = write_agents(tmp_path, '[agents.first]\ncommand = "a"\n[agents.second]\ncommand = "b"\n')
    assert acp_config.agent_named(path, "second").name == "second"
    assert acp_config.agent_named(path, "first").name == "first"


EVIDENCE_157 = REPO_ROOT / "evidence" / "157"


def test_evidence_the_four_acceptance_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Captures the four Gherkin criteria as one transcript, regenerated on every verify run."""
    report = []

    counter = iter(range(1, 100))

    def sub() -> Path:
        folder = tmp_path / str(next(counter))
        folder.mkdir()
        return folder

    v4 = write_agents(sub(), '[agents.worker]\ncommand = "a"\n')
    spec = acp_config.agent_named(v4, "worker")
    report.append(
        "AC 1 - a v4 entry with none of the new fields loads unchanged:\n"
        f"  kind={spec.kind!r} billing={spec.billing!r} (not metered)\n"
    )

    for label, body in (
        ('kind = "x"', '[agents.broken]\ncommand = "a"\nkind = "x"\n'),
        ('billing = "x"', '[agents.broken]\ncommand = "a"\nbilling = "x"\n'),
        ("floor = 1.0", '[agents.broken]\ncommand = "a"\nfloor = 1.0\n'),
    ):
        with pytest.raises(ConfigError) as caught:
            acp_config.load_agents(write_agents(sub(), body))
        report.append(f"AC 2 - {label} is refused naming the entry:\n  {caught.value}\n")

    monkeypatch.setenv("OPENROUTER_API_KEY", SEEDED_OPENROUTER_KEY)
    rendered_path = write_agents(
        sub(),
        "\n".join(
            [
                "[agents.pi]",
                'command = "npx"',
                'billing = "openrouter"',
                'base_url_var = "OPENROUTER_BASE_URL"',
                'key_var = "OPENROUTER_API_KEY"',
            ]
        )
        + "\n",
    )
    rendered = rendered_path.read_text(encoding="utf-8")
    report.append(
        "AC 3 - an openrouter entry, rendered:\n"
        f"{rendered}"
        f"  grep for the seeded OpenRouter key: {rendered.count(SEEDED_OPENROUTER_KEY)} matches\n"
    )

    two = write_agents(sub(), '[agents.first]\ncommand = "a"\n[agents.second]\ncommand = "b"\n')
    named = acp_config.agent_named(two, "second")
    report.append(
        "AC 4 - two entries, no default, named delegation runs the named entry:\n"
        f"  agent_named(path, 'second').name == {named.name!r}\n"
    )

    EVIDENCE_157.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_157 / "acceptance-criteria.txt").write_text("\n".join(report), encoding="utf-8")


# ---- the permission matrix: fail closed, never hang -------------------------------------------


def refusing_prompter(question: str, options: tuple[str, ...], timeout: float) -> str | None:
    """A prompter that must never be reached. Being called at all is the failure."""
    raise AssertionError("a non-ask rule blocked on input, which an unattended run cannot afford")


def answering(option_id: str | None) -> permissions.Prompter:
    """A prompter that always answers ``option_id``. ``None`` is what a timeout looks like."""

    def prompter(question: str, options: tuple[str, ...], timeout: float) -> str | None:
        return option_id

    return prompter


def policy_for(mode: str, **fields: Any) -> permissions.Policy:
    return permissions.Policy(
        mode=mode,
        allow=tuple(fields.get("allow", ())),
        deny=tuple(fields.get("deny", ())),
        interactive=bool(fields.get("interactive", False)),
    )


@pytest.mark.parametrize("mode", ["allow", "deny", "allowlist", "ask"])
def test_deny_tools_beats_every_other_rule(mode: str) -> None:
    decision = permissions.decide(
        policy_for(mode, allow=["edit"], deny=["edit"], interactive=True),
        "edit",
        prompter=answering("allow_once"),
    )
    assert decision.allowed is False
    assert decision.rule == "deny_tools"


def test_mode_allow_allows_a_tool_it_never_heard_of() -> None:
    decision = permissions.decide(policy_for("allow"), "execute", prompter=refusing_prompter)
    assert (decision.allowed, decision.rule) == (True, "mode_allow")


def test_mode_deny_denies_a_tool_that_is_on_the_allow_list() -> None:
    listed = policy_for("deny", allow=["read"])
    decision = permissions.decide(listed, "read", prompter=refusing_prompter)
    assert (decision.allowed, decision.rule) == (False, "mode_deny")


def test_allowlist_allows_a_hit_and_denies_a_miss() -> None:
    listed = policy_for("allowlist", allow=["read"])
    assert permissions.decide(listed, "read", prompter=refusing_prompter).allowed is True
    miss = permissions.decide(listed, "execute", prompter=refusing_prompter)
    assert (miss.allowed, miss.rule) == (False, "allowlist_miss")


def test_ask_unattended_degrades_to_allowlist_and_warns() -> None:
    """The single most important line in the package: an unattended run never stalls."""
    unattended = policy_for("ask", allow=["read"], interactive=False)
    with pytest.warns(UserWarning, match="allowlist"):
        decision = permissions.decide(unattended, "read", prompter=refusing_prompter)
    assert decision.allowed is True
    assert decision.rule == "ask_degraded_allowlist"
    assert decision.warning


def test_the_unattended_degrade_still_refuses_a_tool_off_the_list() -> None:
    """Degrading is not auto-approving. The list is still the list."""
    unattended = policy_for("ask", allow=["read"], interactive=False)
    with pytest.warns(UserWarning):
        decision = permissions.decide(unattended, "execute", prompter=refusing_prompter)
    assert decision.allowed is False
    assert decision.rule == "ask_degraded_allowlist"


def test_ask_in_an_interactive_session_denies_on_timeout() -> None:
    listed = policy_for("ask", allow=["read"], interactive=True)
    decision = permissions.decide(listed, "read", prompter=answering(None))
    assert (decision.allowed, decision.rule) == (False, "ask_timeout")


def test_ask_in_an_interactive_session_honours_an_allow_answer() -> None:
    listed = policy_for("ask", interactive=True)
    decision = permissions.decide(listed, "edit", prompter=answering("allow_once"))
    assert (decision.allowed, decision.rule) == (True, "ask_allowed")


def test_ask_in_an_interactive_session_honours_a_reject_answer() -> None:
    listed = policy_for("ask", interactive=True)
    decision = permissions.decide(listed, "edit", prompter=answering("reject_once"))
    assert (decision.allowed, decision.rule) == (False, "ask_denied")


def test_ask_with_no_prompter_at_all_denies_rather_than_waits() -> None:
    decision = permissions.decide(policy_for("ask", interactive=True), "edit", prompter=None)
    assert (decision.allowed, decision.rule) == (False, "ask_unanswerable")


def test_every_rule_a_decision_can_carry_is_in_the_declared_vocabulary() -> None:
    """A caller reads ``rule`` instead of re-deriving why, so the vocabulary is closed."""
    seen = {
        permissions.decide(policy_for("allow"), "x").rule,
        permissions.decide(policy_for("deny"), "x").rule,
        permissions.decide(policy_for("allowlist", allow=["x"]), "x").rule,
        permissions.decide(policy_for("allowlist"), "x").rule,
        permissions.decide(policy_for("ask", interactive=True), "x").rule,
        permissions.decide(policy_for("ask", deny=["x"]), "x").rule,
    }
    assert seen <= set(permissions.RULES)
    assert "ask_degraded_allowlist" in permissions.RULES


def test_the_chosen_option_matches_the_decision_against_the_agents_options() -> None:
    """Answering the agent means picking one of *its* options, not inventing a word."""
    options = ("allow_once", "reject_once")
    assert permissions.option_for(True, options) == "allow_once"
    assert permissions.option_for(False, options) == "reject_once"
    assert permissions.option_for(True, ("reject_once",)) is None


# ---- the runtime: one background loop, one submit with a timeout ------------------------------


def test_submit_runs_a_coroutine_off_the_calling_thread() -> None:
    async def where() -> int:
        return threading.get_ident()

    assert acp_runtime.submit(where, timeout=READ_TIMEOUT) != threading.get_ident()


def test_the_same_background_loop_serves_every_submit() -> None:
    """One loop on one daemon thread, per ``acp.md``. Not one loop per delegation."""

    async def where() -> int:
        return threading.get_ident()

    first = acp_runtime.submit(where, timeout=READ_TIMEOUT)
    second = acp_runtime.submit(where, timeout=READ_TIMEOUT)
    assert first == second


def test_the_loop_thread_is_a_daemon_so_it_never_holds_the_process_open() -> None:
    async def nothing() -> None:
        return None

    acp_runtime.submit(nothing, timeout=READ_TIMEOUT)
    assert acp_runtime.loop_thread() is not None
    thread = acp_runtime.loop_thread()
    assert thread is not None and thread.daemon


def test_a_coroutine_that_outlives_its_timeout_becomes_a_delegation_timeout() -> None:
    async def forever() -> None:
        await asyncio.sleep(60)

    with pytest.raises(DelegationTimeout):
        acp_runtime.submit(forever, timeout=0.2)


def test_a_timeout_without_on_timeout_raises_promptly_and_reports_unsettled() -> None:
    """Review-gate finding: the settle wait is a backstop for ``on_timeout``, not a blanket delay.

    With no callback nothing will stop the coroutine, so waiting out ``SETTLE_GRACE_S`` would be
    pure delay - a caller that used to raise immediately after ``future.cancel()`` would hang for
    the whole grace bound on every bare timeout. The caller is released at once, and the flag
    honestly reports that the state was not settled.
    """

    async def forever() -> None:
        await asyncio.sleep(60)

    started = time.monotonic()
    with pytest.raises(DelegationTimeout) as info:
        acp_runtime.submit(forever, timeout=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, "no callback means there is nothing to wait for, so the raise is prompt"
    assert info.value.settled is False


def test_an_error_inside_the_coroutine_reaches_the_caller_unchanged() -> None:
    async def unhappy() -> None:
        raise AcpVersionMismatch("wrong version")

    with pytest.raises(AcpVersionMismatch, match="wrong version"):
        acp_runtime.submit(unhappy, timeout=READ_TIMEOUT)


# ---- ticket 060: the ceiling settles the coroutine before the caller reads its state ------------


def test_the_coroutine_has_stopped_writing_before_submit_raises() -> None:
    """Review-gate finding 3. The race, not the wording.

    ``kill_tree`` returns when the signal is sent, not when the coroutine has noticed the pipe
    close, so ``_finish`` used to read ``client.files_changed`` and ``client.summary()`` on the
    calling thread while the loop thread was still appending. That partial is not merely printed:
    it reaches the raised error and ``ledger.settle(..., failure=...)``, so a value read mid-write
    gets persisted.

    ``collected`` stands in for the client's state, and ``on_timeout`` for killing the child.
    """
    released = threading.Event()
    collected: list[int] = []
    finished: list[bool] = []

    async def appending() -> None:
        try:
            for index in range(10_000):
                if released.is_set():
                    break
                collected.append(index)
                await asyncio.sleep(0.001)
        finally:
            finished.append(True)

    with pytest.raises(DelegationTimeout):
        acp_runtime.submit(appending, timeout=0.2, on_timeout=released.set)

    first = len(collected)
    second = len(collected)
    assert finished == [True], "the coroutine must have run to completion before the caller woke"
    assert first == second, "the caller read a list another thread was still appending to"


def test_on_timeout_runs_before_the_settle_not_after() -> None:
    """The callback is how the caller stops the child, so it must precede the wait.

    Asserted through effect rather than instrumentation: the coroutine only ends because the
    callback fired, so a callback invoked after the join would run to the grace bound instead of
    returning promptly.
    """
    released = threading.Event()

    async def waits() -> None:
        while not released.is_set():
            await asyncio.sleep(0.001)

    started = time.monotonic()
    with pytest.raises(DelegationTimeout):
        acp_runtime.submit(waits, timeout=0.2, on_timeout=released.set)
    elapsed = time.monotonic() - started

    assert elapsed < acp_runtime.SETTLE_GRACE_S, (
        "the coroutine ended as soon as on_timeout fired, so the wait should not have run to the "
        "grace bound"
    )


def test_a_coroutine_that_never_settles_still_releases_the_caller_and_says_so() -> None:
    """The uncloseable case shrinks; it does not vanish.

    A coroutine that ignores the callback must not hold the caller forever, and the error has to
    distinguish "timed out, state settled" from "timed out and the writer never stopped" - a
    partial presented as settled when it is not would be this ticket's own defect one level up.
    """

    async def unstoppable() -> None:
        await asyncio.sleep(60)

    started = time.monotonic()
    with pytest.raises(DelegationTimeout) as info:
        acp_runtime.submit(unstoppable, timeout=0.2, on_timeout=lambda: None)
    elapsed = time.monotonic() - started

    assert elapsed < 0.2 + acp_runtime.SETTLE_GRACE_S + 2.0, "the caller must be released"
    assert info.value.settled is False
    assert "did not settle" in str(info.value)


def test_a_settled_timeout_says_it_settled() -> None:
    """The other half of the flag, so ``settled`` cannot be a constant that reads as informative."""
    released = threading.Event()

    async def stops() -> None:
        while not released.is_set():
            await asyncio.sleep(0.001)

    with pytest.raises(DelegationTimeout) as info:
        acp_runtime.submit(stops, timeout=0.2, on_timeout=released.set)

    assert info.value.settled is True
    assert "did not settle" not in str(info.value)


# ---- the coroutine quarantine -----------------------------------------------------------------


def coroutine_definers(root: Path) -> list[str]:
    """Every module under ``root`` defining an ``async def``, relative to the package root."""
    found = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree)):
            found.append(path.relative_to(PACKAGE_ROOT).as_posix())
    return sorted(found)


#: The one module outside ``acp/`` the architecture lets define a coroutine: the SSE tail
#: generator (decision 20, ticket 069). Asserted in both directions, so the exemption cannot
#: widen unnoticed and cannot outlive the generator it names.
COROUTINE_EXEMPTIONS = frozenset({"gateway/sse.py"})


def test_no_module_outside_acp_defines_a_coroutine() -> None:
    """FR-22: async is one package's problem, and it is this one's, plus the one named exemption."""
    outside = [name for name in coroutine_definers(PACKAGE_ROOT) if not name.startswith("acp/")]
    assert outside == sorted(COROUTINE_EXEMPTIONS)


def test_acp_is_where_the_coroutines_actually_are() -> None:
    """Without this the scan above would also pass over a repo containing no async at all."""
    assert coroutine_definers(ACP_ROOT)


# ---- delegation against the real stub, over the real protocol ---------------------------------


@pytest.fixture
def harness_home(tmp_path: Path) -> Path:
    home = tmp_path / ".harnessed"
    home.mkdir()
    return home


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "harness.db")
    db.migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def transcript_files(home: Path) -> list[Path]:
    return sorted((home / "acp").glob("*.json"))


def test_a_completed_delegation_returns_the_five_field_summary(
    tmp_path: Path, harness_home: Path
) -> None:
    result = acp_delegate.delegate(
        probe_spec(tmp_path),
        task="do the thing",
        acceptance="the thing is done",
        home=harness_home,
    )
    assert result.status == "succeeded"
    assert result.session_id.startswith("sess_")
    assert isinstance(result.files_changed, tuple)
    assert result.summary == stub.STUB_MESSAGE
    assert result.acceptance == "the thing is done"
    assert set(result.as_dict()) == {
        "status",
        "session_id",
        "files_changed",
        "summary",
        "acceptance",
    }


def test_the_transcript_lands_on_disk_and_never_in_the_summary(
    tmp_path: Path, harness_home: Path
) -> None:
    result = acp_delegate.delegate(
        probe_spec(tmp_path), task="do it", acceptance="done", home=harness_home
    )
    (written,) = transcript_files(harness_home)
    transcript = written.read_text(encoding="utf-8")
    assert stub.TOOL_TITLE in transcript
    assert stub.STUB_MESSAGE in transcript

    handed_to_the_model = json.dumps(result.as_dict())
    assert stub.TOOL_TITLE not in handed_to_the_model
    assert len(handed_to_the_model) < len(transcript)


def test_the_transcript_also_lands_in_episodic_memory(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    bus = EventBus(conn=store)
    acp_delegate.delegate(
        probe_spec(tmp_path),
        task="do it",
        acceptance="done",
        home=harness_home,
        bus=bus,
        store=store,
    )
    episodes = episodic.for_run(store, bus.run_id)
    assert [episode.kind for episode in episodes] == ["delegation"]
    assert stub.TOOL_TITLE in episodes[0].text


def test_a_delegation_publishes_its_start_and_its_finish(
    tmp_path: Path, harness_home: Path
) -> None:
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    acp_delegate.delegate(
        probe_spec(tmp_path), task="do it", acceptance="done", home=harness_home, bus=bus
    )
    assert seen.names == ["acp.delegation.started", "acp.tool.called", "acp.delegation.finished"]
    assert seen.named("acp.delegation.finished")[0].payload["status"] == "succeeded"
    called = seen.named("acp.tool.called")[0].payload
    assert called["title"] == stub.TOOL_TITLE
    assert called["kind"] == "other"
    assert called["agent"] == "probe"


def test_a_step_is_published_while_the_agent_is_still_working(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console is live only if a step arrives before the delegation returns, not with it.

    The stub announces its tool call, works for two seconds, then reports the call finished. A
    step flushed only at the end would arrive within a moment of the return; a live one arrives
    about two seconds earlier.
    """
    import time

    monkeypatch.setenv("HARNESSED_STUB_WORK_S", "2")
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    arrived: dict[str, float] = {}
    bus.subscribe(lambda event: arrived.setdefault(event.name, time.monotonic()))
    acp_delegate.delegate(
        probe_spec(tmp_path), task="do it", acceptance="done", home=harness_home, bus=bus
    )
    returned = time.monotonic()

    assert returned - arrived["acp.tool.called"] >= 1.5
    # The stub's finishing update carries no title; the step still names the call it ends.
    (finished,) = seen.named("acp.tool.finished")
    assert finished.payload["title"] == stub.TOOL_TITLE
    assert finished.payload["status"] == "completed"
    assert arrived["acp.tool.called"] < arrived["acp.tool.finished"]
    assert arrived["acp.tool.finished"] <= arrived["acp.delegation.finished"]


def test_a_permission_request_publishes_a_request_and_a_decision(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_ASK_PERMISSION", "1")
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    acp_delegate.delegate(
        probe_spec(tmp_path, mode='"allow"'),
        task="do it",
        acceptance="done",
        home=harness_home,
        bus=bus,
    )
    assert seen.names == [
        "acp.delegation.started",
        "acp.tool.called",
        "acp.permission.request",
        "acp.permission.decision",
        "acp.delegation.finished",
    ]
    decision = seen.named("acp.permission.decision")[0].payload
    assert decision["allowed"] is True
    assert decision["rule"] == "mode_allow"


def test_the_decision_reaches_the_agent_over_the_real_channel(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub echoes the option it was granted, so this proves the answer was on the wire."""
    monkeypatch.setenv("HARNESSED_STUB_ASK_PERMISSION", "1")
    granted = acp_delegate.delegate(
        probe_spec(tmp_path, mode='"deny"'),
        task="do it",
        acceptance="done",
        home=harness_home,
    )
    (written,) = transcript_files(harness_home)
    assert '"reject_once"' in written.read_text(encoding="utf-8")
    assert granted.status == "succeeded"


def test_an_unattended_ask_degrades_rather_than_stalling_the_delegation(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the overnight run finishes instead of sitting on a prompt."""
    monkeypatch.setenv("HARNESSED_STUB_ASK_PERMISSION", "1")
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = acp_delegate.delegate(
            probe_spec(tmp_path, mode='"ask"', allow='["other"]'),
            task="do it",
            acceptance="done",
            home=harness_home,
            bus=bus,
        )
    assert result.status == "succeeded"
    assert any("allowlist" in str(warning.message) for warning in caught)
    decision = seen.named("acp.permission.decision")[0].payload
    assert decision["rule"] == "ask_degraded_allowlist"
    assert decision["allowed"] is False


def test_a_protocol_version_mismatch_fails_loudly_at_initialize(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "bad_version")
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    with pytest.raises(AcpVersionMismatch) as caught:
        acp_delegate.delegate(
            probe_spec(tmp_path), task="do it", acceptance="done", home=harness_home, bus=bus
        )
    message = str(caught.value)
    assert str(stub.WRONG_PROTOCOL_VERSION) in message
    assert str(PROTOCOL_VERSION) in message
    # Loudly at initialize means no session was ever opened and no work was ever prompted.
    assert seen.named("acp.delegation.finished")[0].payload["status"] == "version_mismatch"
    assert seen.named("acp.delegation.finished")[0].payload["session_id"] == ""


def test_an_unnamed_error_settles_failed_and_is_a_chained_delegation_error(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-93, decision 69: the fourth failure - none of the three named errors.

    Grok's own JSON-RPC "Internal error" reaches ``delegate`` as ``acp.RequestError``, a class the
    three named ``except`` clauses never named. Every caller that already handles
    ``DelegationError`` handles this too, because it is what leaves ``delegate``.
    """
    monkeypatch.setenv("HARNESSED_STUB_MODE", "internal_error")
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    with pytest.raises(DelegationError) as caught:
        acp_delegate.delegate(
            probe_spec(tmp_path), task="do it", acceptance="done", home=harness_home, bus=bus
        )
    original = caught.value.__cause__
    assert original is not None
    assert not isinstance(original, DelegationError)
    finished = seen.named("acp.delegation.finished")
    assert len(finished) == 1
    assert finished[0].payload["status"] == "error"
    assert str(caught.value) == "the agent 'probe' failed: -32603 Internal error"
    assert finished[0].payload["reason"] == "-32603 Internal error"


QUOTA_DETAIL = '-32000 Usage limit reached ({"retry_after":"2026-10-04T00:00:00Z"})'


def test_an_agents_own_error_code_message_and_data_reach_every_record(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 229, FR-93, decision 69: the operator reads why, on one line."""
    monkeypatch.setenv("HARNESSED_STUB_MODE", "quota_error")
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    with pytest.raises(DelegationError) as caught:
        acp_delegate.delegate(
            probe_spec(tmp_path), task="do it", acceptance="done", home=harness_home, bus=bus
        )
    assert str(caught.value) == f"the agent 'probe' failed: {QUOTA_DETAIL}"
    assert seen.named("acp.delegation.finished")[0].payload["reason"] == QUOTA_DETAIL
    written = [json.loads(p.read_text("utf-8")) for p in (harness_home / "acp").glob("dlg_*.json")]
    assert [w["failure"] for w in written] == [QUOTA_DETAIL]


def test_a_secret_in_an_agents_error_message_is_scrubbed_everywhere(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-or-v1-" + "a1b2c3d4" * 6
    monkeypatch.setenv("HARNESSED_STUB_MODE", "quota_error")
    monkeypatch.setenv("HARNESSED_STUB_ERROR_NOTE", f" for {secret}")
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    with pytest.raises(DelegationError) as caught:
        acp_delegate.delegate(
            probe_spec(tmp_path), task="do it", acceptance="done", home=harness_home, bus=bus
        )
    texts = [str(caught.value), repr(seen.named("acp.delegation.finished")[0].payload)]
    texts += [p.read_text("utf-8") for p in (harness_home / "acp").glob("dlg_*.json")]
    assert all(secret not in text for text in texts)
    assert "[redacted" in str(caught.value)


def test_failure_detail_for_other_exceptions_is_class_and_first_line_capped() -> None:
    detail = acp_delegate._failure_detail
    assert detail(ValueError("boom\nsecond line")) == "ValueError: boom"
    assert detail(ValueError("")) == "ValueError"
    capped = detail(ValueError("x" * 500))
    assert len(capped) == 300 and capped.endswith("...")


def test_a_missing_cli_names_the_exact_install_command(tmp_path: Path, harness_home: Path) -> None:
    body = (
        "[agents.absent]\n"
        'command = "a-binary-that-is-not-installed"\n'
        'install = "run this exact command to install it"\n'
    )
    spec = acp_config.agent_named(write_agents(tmp_path, body), "absent")
    with pytest.raises(DelegationError) as caught:
        acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home)
    assert "run this exact command to install it" in str(caught.value)


def test_a_hard_timeout_kills_the_recorded_process_tree(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "hang")
    spawned: list[int] = []
    spec = probe_spec(tmp_path, timeout_s=str(SHORT_TIMEOUT))
    with pytest.raises(DelegationTimeout) as caught:
        acp_delegate.delegate(
            spec,
            task="do it",
            acceptance="done",
            home=harness_home,
            on_spawn=spawned.append,
        )
    assert len(spawned) == 1
    assert not acp_delegate.alive(spawned[0]), "the recorded pid outlived its ceiling"
    assert "done" in str(caught.value), "the partial summary must carry the acceptance"


def test_a_timeout_still_writes_the_partial_transcript(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "hang")
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    with pytest.raises(DelegationTimeout):
        acp_delegate.delegate(
            probe_spec(tmp_path, timeout_s=str(SHORT_TIMEOUT)),
            task="do it",
            acceptance="done",
            home=harness_home,
            bus=bus,
        )
    assert transcript_files(harness_home)
    assert seen.named("acp.delegation.finished")[0].payload["status"] == "timed_out"


# ---- the tool the model actually sees ---------------------------------------------------------


def registry_for(tmp_path: Path, home: Path, **fields: str):
    """The default registry, with the delegation tool bound to a stub ``agents.toml``."""
    return default_registry(home, agents_path=stub_agents(tmp_path, **fields))


def test_the_tool_returns_the_compact_summary_as_its_content(
    tmp_path: Path, harness_home: Path
) -> None:
    bench = registry_for(tmp_path, harness_home)
    result = bench.dispatch(
        ToolCall(
            id="d1",
            name="delegate_to_agent",
            arguments={"task": "do the thing", "acceptance": "the thing is done"},
        )
    )
    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["status"] == "succeeded"
    assert payload["summary"] == stub.STUB_MESSAGE
    assert stub.TOOL_TITLE not in result.content


def test_a_missing_cli_comes_back_as_an_error_tool_result_naming_the_install(
    tmp_path: Path, harness_home: Path
) -> None:
    """The loop sees a failed tool, not a crashed harness (``acp.md``, failure policy)."""
    body = (
        "[agents.absent]\n"
        'command = "a-binary-that-is-not-installed"\n'
        'install = "run this exact command to install it"\n'
    )
    bench = default_registry(harness_home, agents_path=write_agents(tmp_path, body))
    result = bench.dispatch(
        ToolCall(id="d1", name="delegate_to_agent", arguments={"task": "t", "acceptance": "a"})
    )
    assert result.is_error is True
    assert "run this exact command to install it" in result.content


def test_a_hard_timeout_comes_back_as_an_error_tool_result_not_a_crash(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "hang")
    bench = registry_for(tmp_path, harness_home, timeout_s=str(SHORT_TIMEOUT))
    result = bench.dispatch(
        ToolCall(
            id="d1", name="delegate_to_agent", arguments={"task": "t", "acceptance": "acc-marker"}
        )
    )
    assert result.is_error is True
    assert "acc-marker" in result.content


def test_an_unknown_agent_name_comes_back_as_an_error_tool_result(
    tmp_path: Path, harness_home: Path
) -> None:
    bench = registry_for(tmp_path, harness_home)
    result = bench.dispatch(
        ToolCall(
            id="d1",
            name="delegate_to_agent",
            arguments={"task": "t", "acceptance": "a", "agent": "ghost"},
        )
    )
    assert result.is_error is True
    assert "ghost" in result.content


def test_the_registry_still_builds_when_there_is_no_agents_file_at_all(
    harness_home: Path, tmp_path: Path
) -> None:
    """Configuration is read when the tool runs, so a missing file is a value, not a crash."""
    bench = default_registry(harness_home, agents_path=tmp_path / "absent.toml")
    assert "delegate_to_agent" in bench.names()
    result = bench.dispatch(
        ToolCall(id="d1", name="delegate_to_agent", arguments={"task": "t", "acceptance": "a"})
    )
    assert result.is_error is True
    assert "absent.toml" in result.content


# ==============================================================================================
# Ticket 088 - coding agents launch on Windows, and a failure says why
# ==============================================================================================
#
# Four defects stacked on one operator's machine: a ``.cmd`` shim launched through whatever
# ``ComSpec`` named, a child's stderr sent to DEVNULL so the only symptom was "Connection closed",
# two stale templates, and a doctor that said ``live`` after a PATH check. The last is asserted
# in ``tests/test_doctor.py``; the other three are here, because this file declares the agent
# vocabulary and so may spell a template's name.

#: A word in any template argument that would let an agent run without the registry deciding.
AUTO_APPROVE = re.compile(r"approv|yolo|danger|bypass|skip-perm|full-auto|trust", re.IGNORECASE)


def test_the_templates_run_the_current_adapters() -> None:
    table = acp_config.TEMPLATES
    claude, codex = FORBIDDEN_CLI_NAMES[0], FORBIDDEN_CLI_NAMES[1]
    assert (table[claude].command, table[claude].args) == (
        "npx",
        ("-y", "@agentclientprotocol/claude-agent-acp"),
    )
    assert (table[codex].command, table[codex].args) == (
        "npx",
        ("-y", f"@agentclientprotocol/{codex}-acp"),
    )
    assert (table["pi"].command, table["pi"].args) == ("npx", ("-y", "pi-acp"))
    assert (table["grok"].command, table["grok"].args) == ("grok", ("agent", "stdio"))


def test_no_template_carries_an_auto_approve_flag() -> None:
    """Every permission request is decided by the registry's policy, never by a flag."""
    for template in acp_config.TEMPLATES.values():
        assert not [part for part in template.args if AUTO_APPROVE.search(part)], template.name


def test_the_auto_approve_scan_catches_a_synthetic_flag() -> None:
    assert AUTO_APPROVE.search("--always-approve")
    assert not AUTO_APPROVE.search("stdio")


def talking_spec(path: Path, script: str) -> acp_config.AgentSpec:
    """The probe agent, but its program is ``script`` run by this interpreter."""
    return probe_spec(path, args=json.dumps(["-c", script]))


def test_an_agent_that_dies_before_answering_quotes_its_stderr_tail(
    tmp_path: Path, harness_home: Path
) -> None:
    """The reason reaches the caller: scrubbed, bounded, and from the end of what was said."""
    secret = "sk-" + "ant-api03-" + "Q" * 48
    script = (
        "import sys\n"
        "sys.stderr.write('x' * (1 << 20) + '\\n')\n"
        "for i in range(200): print('noise', i, file=sys.stderr)\n"
        f"print('the last words', {secret!r}, file=sys.stderr)\n"
        "sys.exit(3)\n"
    )
    with pytest.raises(DelegationError) as caught:
        acp_delegate.delegate(
            talking_spec(tmp_path, script), task="do it", acceptance="done", home=harness_home
        )
    message = str(caught.value)
    assert "the last words" in message
    assert "noise 199" in message
    assert "noise 0\n" not in message, "the tail is the end of stderr, not all of it"
    assert secret not in message
    assert len(message) < 4000, "a talkative child must not become a huge error"
    evidence = REPO_ROOT / "evidence" / "088"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "agents.txt").write_text(
        f"A dying test double's error:\n\n{message}\n\n"
        f"agents.toml.example, regenerated from the table:\n\n{acp_config.render_example()}",
        encoding="utf-8",
    )


def test_a_failed_delegation_is_settled_and_finished_as_failed(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    bus = EventBus(conn=store)
    seen = RecordingObserver()
    bus.subscribe(seen)
    spec = talking_spec(tmp_path, "import sys\nprint('gone', file=sys.stderr)\nsys.exit(1)\n")
    with pytest.raises(DelegationError):
        acp_delegate.delegate(
            spec, task="do it", acceptance="done", home=harness_home, bus=bus, store=store
        )
    assert seen.named("acp.delegation.finished")[0].payload["status"] == "failed"
    key = acp_delegate.ledger.task_hash(
        agent=spec.name, cwd=str(Path.cwd()), task="do it", acceptance="done"
    )
    recorded = acp_delegate.ledger.get(store, key)
    assert recorded is not None
    assert recorded.status == acp_delegate.ledger.FAILED
    assert "gone" in recorded.failure


def test_a_talkative_agent_cannot_fill_the_stderr_pipe_and_stall(
    tmp_path: Path, harness_home: Path
) -> None:
    """Megabytes on stderr before the handshake, and the delegation still completes."""
    script = (
        "import runpy, sys\n"
        "sys.stderr.write('chatter ' * (1 << 18)); sys.stderr.flush()\n"
        f"sys.argv = [{str(STUB_PATH)!r}]\n"
        f"runpy.run_path({str(STUB_PATH)!r}, run_name='__main__')\n"
    )
    result = acp_delegate.delegate(
        talking_spec(tmp_path, script), task="do it", acceptance="done", home=harness_home
    )
    assert result.status == "succeeded"
    assert result.summary == stub.STUB_MESSAGE


@pytest.mark.skipif(
    sys.platform != "win32", reason="a .cmd agent is a Windows launch; ci: windows job"
)
def test_a_cmd_agent_launches_through_cmd_exe_whatever_comspec_names(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ComSpec pointed at PowerShell split an unquoted path at its space. It must not matter."""
    folder = tmp_path / "with space"
    folder.mkdir()
    shim = folder / "agent.cmd"
    shim.write_text(f'@"{sys.executable}" "{STUB_PATH}" %*\r\n', encoding="utf-8")
    system = Path(os.environ["SYSTEMROOT"]) / "System32"
    powershell = system / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    monkeypatch.setenv("ComSpec", str(powershell if powershell.is_file() else sys.executable))
    spec = probe_spec(tmp_path, command=json.dumps(str(shim)), args="[]")
    result = acp_delegate.delegate(spec, task="do it", acceptance="done", home=harness_home)
    assert result.status == "succeeded"
    assert result.summary == stub.STUB_MESSAGE


# ---- ticket 090: no run or delegation leaves an agent process behind --------------------------

EVIDENCE_090 = REPO_ROOT / "evidence" / "090"
#: Long enough for the stub and its grandchild to start cold on Windows before the ceiling fires.
GRANDCHILD_TIMEOUT = 5.0
#: How long a killed process may take to leave the process table.
DEATH_GRACE = 5.0


def survivors(pids: Iterable[int], grace: float = DEATH_GRACE) -> list[int]:
    """The pids still alive once ``grace`` has passed, or at once when all are already gone."""
    watched = list(pids)
    deadline = time.monotonic() + grace
    while True:
        living = [pid for pid in watched if acp_delegate.alive(pid)]
        if not living or time.monotonic() >= deadline:
            return living
        time.sleep(0.1)


@pytest.mark.parametrize(
    ("mode", "raised"),
    [("ok", None), ("bad_version", AcpVersionMismatch), ("hang", DelegationTimeout)],
    ids=["succeeds", "fails", "times_out"],
)
def test_no_process_of_the_agents_tree_survives_the_delegation(
    mode: str,
    raised: type[Exception] | None,
    tmp_path: Path,
    harness_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success path kills the tree like the others do, not only the direct child."""
    grandchild = tmp_path / "grandchild.pid"
    monkeypatch.setenv(stub.GRANDCHILD_VAR, str(grandchild))
    monkeypatch.setenv("HARNESSED_STUB_MODE", mode)
    spawned: list[int] = []
    spec = probe_spec(tmp_path, timeout_s=str(GRANDCHILD_TIMEOUT))
    expectation = pytest.raises(raised) if raised else contextlib.nullcontext()
    with expectation:
        acp_delegate.delegate(
            spec, task="do it", acceptance="done", home=harness_home, on_spawn=spawned.append
        )
    assert grandchild.is_file(), "the stub never started its grandchild"
    pids = [*spawned, int(grandchild.read_text(encoding="utf-8"))]
    try:
        assert survivors(pids) == [], f"the {mode} path left part of the agent's tree alive"
    finally:
        for pid in pids:
            acp_delegate.kill_tree(pid)


INNER_INTERRUPTED = """
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from harnessed.acp import config, delegate


def test_interrupted_midway_through_a_delegation(tmp_path):
    spec = config.agent_named(Path(os.environ["INNER_AGENTS"]), None)
    spawned = Path(os.environ["INNER_SPAWNED"])
    worker = threading.Thread(
        target=delegate.delegate,
        args=(spec, "do it", "done"),
        kwargs={"home": tmp_path, "on_spawn": lambda pid: spawned.write_text(str(pid))},
        daemon=True,
    )
    worker.start()
    grandchild = Path(os.environ["HARNESSED_STUB_GRANDCHILD"])
    deadline = time.monotonic() + 30
    while not grandchild.is_file():
        assert time.monotonic() < deadline, "the stub never started its grandchild"
        time.sleep(0.05)
    raise KeyboardInterrupt
"""

INNER_ABANDONED = """
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def stub_reaper():
    # Overrides the per-test finaliser, so the session-end check is the only net left.
    yield


def test_starts_a_stub_and_walks_away():
    registry = Path(os.environ["HARNESSED_TEST_STUB_REGISTRY"])
    subprocess.Popen(
        [sys.executable, os.environ["INNER_STUB"]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Real spawns isolate the child's process group (``_process_group``); without doing the
        # same here, the reaper's ``killpg`` on this abandoned stub also takes down the pytest
        # process that is walking away from it.
        start_new_session=True,
    )
    deadline = time.monotonic() + 30
    while not any(registry.iterdir()):
        assert time.monotonic() < deadline, "the stub never registered"
        time.sleep(0.05)
    (registered,) = registry.iterdir()
    Path(os.environ["INNER_SPAWNED"]).write_text(registered.name)
"""


def run_inner_pytest(
    folder: Path, body: str, *, reaper: bool, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run ``body`` as its own pytest session, with or without ``tests.support.reaper`` loaded."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "test_inner.py").write_text(body, encoding="utf-8")
    child_env = {key: value for key, value in os.environ.items() if key != stub.REGISTRY_VAR}
    child_env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])
    )
    # Autoloading every entry-point plugin (deepeval among them) costs ~7 s per inner session;
    # the reaper is named with -p, so the with/without comparison is unchanged.
    child_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    child_env.update(env)
    plugins = ["-p", "tests.support.reaper"] if reaper else []
    return subprocess.run(
        [
            sys.executable,
            *("-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(folder)),
            *plugins,
            str(folder / "test_inner.py"),
        ],
        cwd=folder,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def test_an_interrupted_test_leaves_no_stub_tree_behind(tmp_path: Path) -> None:
    """Reproduce first: the same interrupted run, without the finaliser and then with it."""
    report = ["Survivors after a run interrupted mid-delegation (hang stub with a grandchild):"]
    left: dict[bool, list[int]] = {}
    for reaper in (False, True):
        folder = tmp_path / ("with" if reaper else "without")
        folder.mkdir()
        spawned, grandchild = folder / "spawned.pid", folder / "grandchild.pid"
        outcome = run_inner_pytest(
            folder,
            INNER_INTERRUPTED,
            reaper=reaper,
            HARNESSED_STUB_MODE="hang",
            HARNESSED_STUB_GRANDCHILD=str(grandchild),
            INNER_AGENTS=str(stub_agents(folder, timeout_s="60")),
            INNER_SPAWNED=str(spawned),
        )
        assert outcome.returncode == pytest.ExitCode.INTERRUPTED, outcome.stdout + outcome.stderr
        pids = [int(spawned.read_text()), int(grandchild.read_text())]
        try:
            left[reaper] = survivors(pids, grace=DEATH_GRACE if reaper else 0.0)
        finally:
            for pid in pids:
                acp_delegate.kill_tree(pid)
        label = "after (support.reaper loaded)" if reaper else "before (no finaliser)"
        report.append(f"{label}: {len(left[reaper])} of {len(pids)} alive {left[reaper]}")
    EVIDENCE_090.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_090 / "leftovers.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    assert left[False], "the control run left nothing, so it did not reproduce the leak"
    assert left[True] == [], "the finaliser left part of the stub's tree alive"


def test_a_stub_alive_at_session_end_is_reported_killed_and_fails_the_run(tmp_path: Path) -> None:
    spawned = tmp_path / "spawned.pid"
    outcome = run_inner_pytest(
        tmp_path / "inner",
        INNER_ABANDONED,
        reaper=True,
        HARNESSED_STUB_MODE="hang",
        INNER_STUB=str(STUB_PATH),
        INNER_SPAWNED=str(spawned),
    )
    pid = int(spawned.read_text())
    try:
        assert "outlived the test that started it" in outcome.stdout, outcome.stdout
        assert str(pid) in outcome.stdout
        assert outcome.returncode == pytest.ExitCode.TESTS_FAILED
        assert survivors([pid]) == []
    finally:
        acp_delegate.kill_tree(pid)
    EVIDENCE_090.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_090 / "session-check.txt").write_text(outcome.stdout, encoding="utf-8")


def test_alive_answers_live_exited_and_none_without_starting_a_process() -> None:
    """The reaper asks at every teardown, so a liveness check must cost no process of its own."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        with watch_spawns() as spy:
            assert acp_delegate.alive(child.pid)
            assert acp_delegate.alive(os.getpid())
            assert not acp_delegate.alive(None)
        assert spy.commands == [], f"alive() started {spy.commands}"
    finally:
        child.kill()
        child.wait(timeout=READ_TIMEOUT)
    with watch_spawns() as spy:
        assert not acp_delegate.alive(child.pid)
    assert spy.commands == [], f"alive() started {spy.commands}"


@pytest.mark.skipif(
    sys.platform != "win32", reason="the kernel query is the Windows path; ci: windows job"
)
def test_alive_on_windows_reads_access_denied_as_alive_and_other_failures_as_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for error, expected in ((5, True), (87, False)):
        monkeypatch.setattr(proc, "_open_process", lambda pid: None)
        monkeypatch.setattr(proc.ctypes, "get_last_error", lambda error=error: error)
        assert acp_delegate.alive(1234) is expected


def test_inner_pytest_runs_without_autoloaded_plugins_but_with_the_reaper(tmp_path: Path) -> None:
    seen = tmp_path / "seen.txt"
    body = (
        "import os, sys\nfrom pathlib import Path\n\n\ndef test_env() -> None:\n"
        "    Path(os.environ['INNER_SEEN']).write_text(\n"
        "        os.environ.get('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '') + '|'\n"
        "        + str('tests.support.reaper' in sys.modules)\n    )\n"
    )
    outcome = run_inner_pytest(tmp_path / "inner", body, reaper=True, INNER_SEEN=str(seen))
    assert outcome.returncode == pytest.ExitCode.OK, outcome.stdout + outcome.stderr
    assert seen.read_text() == "1|True"


# ---- ticket 117: coverage never follows a stub agent into its process -------------------------


def test_the_stub_and_its_grandchild_start_without_coverages_variables(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under ``--cov`` a stub started with these variables cold-starts past the 5 s ceiling."""
    grandchild = tmp_path / "grandchild.pid"
    monkeypatch.setenv(stub.GRANDCHILD_VAR, str(grandchild))
    spec = probe_spec(tmp_path, timeout_s=str(GRANDCHILD_TIMEOUT))
    spawned: list[int] = []
    try:
        result = acp_delegate.delegate(
            spec, task="do it", acceptance="done", home=harness_home, on_spawn=spawned.append
        )
    finally:
        for pid in spawned:
            acp_delegate.kill_tree(pid)
        if grandchild.is_file():
            acp_delegate.kill_tree(int(grandchild.read_text(encoding="utf-8")))
    assert result.status == "succeeded"
    for who in ("stub", "grandchild"):
        seen = tmp_path / f"grandchild.pid.{who}{stub.COVERAGE_VARS_SUFFIX}"
        assert json.loads(seen.read_text(encoding="utf-8")) == [], f"the {who} ran under coverage"


def test_spawn_stub_builds_an_environment_without_coverages_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in stub.COVERAGE_VARS:
        monkeypatch.setenv(name, "set by the parent")
    child_env = stub_env(HARNESSED_STUB_MODE="ok")
    assert [name for name in stub.COVERAGE_VARS if name in child_env] == []
    assert child_env["HARNESSED_STUB_MODE"] == "ok"


def test_the_grandchild_ceiling_is_still_five_seconds() -> None:
    """Raising it would slow every ``times_out`` run and hide the cause (ticket 117)."""
    assert GRANDCHILD_TIMEOUT == 5.0


# ---- ticket 167 (FR-106): cli/routed.py::run_agent dispatches by kind, unchanged underneath -----

CLI_STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_cli_agent.py"


def test_run_agent_calls_delegate_for_an_acp_entry(tmp_path: Path) -> None:
    from harnessed.cli import routed as cli_routed

    spec = probe_spec(tmp_path)
    result = cli_routed.run_agent(spec, "do the thing", "it is done", home=tmp_path)
    assert result.status == "succeeded"
    assert result.summary == stub.STUB_MESSAGE


def test_run_agent_calls_cli_runner_for_a_cli_entry(tmp_path: Path) -> None:
    from harnessed.cli import routed as cli_routed

    spec = acp_config.AgentSpec(
        name="probe-cli",
        command=sys.executable,
        args=(str(CLI_STUB_PATH),),
        kind="cli",
        input="stdin",
        cwd=str(tmp_path),
    )
    result = cli_routed.run_agent(spec, "do the thing", "it is done", home=tmp_path)
    assert result.status == "succeeded"
    assert result.summary == "do the thing"


def test_build_task_is_the_bare_task_with_no_session_history() -> None:
    from harnessed.cli import routed as cli_routed

    assert cli_routed.build_task("do it") == f"do it\n\n{cli_routed.NO_CO_AUTHOR}"


def test_build_task_appends_the_session_digest_when_there_is_one() -> None:
    from harnessed.cli import routed as cli_routed
    from harnessed.providers.types import Message

    with_history = cli_routed.build_task(
        "do it",
        (Message(role="user", content="earlier"), Message(role="assistant", content="ok")),
    )
    assert "do it" in with_history
    assert "user: earlier" in with_history
    assert "assistant: ok" in with_history


# ---- ticket 276 (FR-145): a delegated commit carries no agent co-author line --------------------


def test_the_no_co_author_line_is_one_static_sentence_and_no_procedure() -> None:
    from harnessed.cli import routed as cli_routed

    line = cli_routed.NO_CO_AUTHOR
    assert "\n" not in line
    assert "Co-Authored-By" in line
    assert "never" in line.lower()
    assert cli_routed.build_acceptance() == cli_routed.ACCEPTANCE


def test_build_task_ends_with_the_no_co_author_line_in_every_shape() -> None:
    from harnessed.cli import routed as cli_routed
    from harnessed.providers.types import Message

    history = (Message(role="user", content="earlier"), Message(role="assistant", content="ok"))
    for built in (
        cli_routed.build_task("do it"),
        cli_routed.build_task("do it", history),
        cli_routed.build_task("do it", previous="the prior result"),
        cli_routed.build_task("do it", history, "the prior result"),
    ):
        assert built.endswith(cli_routed.NO_CO_AUTHOR)
        assert built.count(cli_routed.NO_CO_AUTHOR) == 1


def test_the_stub_agent_receives_the_no_co_author_line(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    """The delegation journals the prompt it sent as the turn's ``request`` (``journal.task``)."""
    from harnessed.cli import routed as cli_routed

    bus = EventBus(conn=store)
    task = cli_routed.build_task("do the thing")
    cli_routed.run_agent(
        probe_spec(tmp_path, billing='"oauth"'),
        task,
        cli_routed.build_acceptance(),
        home=harness_home,
        bus=bus,
        store=store,
    )
    (request,) = store.execute(
        "SELECT request FROM agent_turns WHERE run_id = ?", (bus.run_id,)
    ).fetchone()
    assert request.endswith(cli_routed.NO_CO_AUTHOR)


def test_build_acceptance_is_the_one_static_sentence() -> None:
    from harnessed.cli import routed as cli_routed

    assert cli_routed.build_acceptance() == cli_routed.ACCEPTANCE
    assert cli_routed.ACCEPTANCE == (
        "Do the work as described in the task; report what you did and what you found."
    )


# ---- ticket 171 (FR-112): a delegation's transcript turns are agent_turns rows ------------------


def test_a_delegation_with_no_routed_message_journals_a_null_message_seq(
    tmp_path: Path, harness_home: Path, store: sqlite3.Connection
) -> None:
    """The ``delegate_to_agent`` tool's door has no routed message, so ``message_seq`` is null."""
    bus = EventBus(conn=store)
    acp_delegate.delegate(
        probe_spec(tmp_path, billing='"oauth"'),
        task="do it",
        acceptance="done",
        home=harness_home,
        bus=bus,
        store=store,
    )
    found = store.execute(
        "SELECT message_seq, hop, source, entry FROM agent_turns WHERE run_id = ?", (bus.run_id,)
    ).fetchall()
    assert found == [(None, 0, "acp", "probe")]
    (turns_rows,) = store.execute(
        "SELECT COUNT(*) FROM turns WHERE run_id = ?", (bus.run_id,)
    ).fetchone()
    assert turns_rows == 0


# ---- Ticket 213 - FR-22, FR-98, FR-106: an agent runs under Harnessed's settings ---------------

SECRET_SEED = "sk" + "-ticket-213-never-in-a-child"
PRE_APPROVING = '{"permissions": {"allow": ["Bash(npx:*)", "Bash", "Edit", "Write"]}}'
CLAUDE_META = (
    "{claudeCode = {options = {settingSources = [], allowDangerouslySkipPermissions = false}}}"
)
TOOL_NAMES = '{execute = ["Bash"], edit = ["Edit", "Write"], delete = ["Rm"], read = ["Read"]}'


def echoed(result: acp_delegate.Delegation) -> dict[str, Any]:
    echo: dict[str, Any] = json.loads(result.summary)
    return echo


def test_an_oauth_acp_child_holds_no_secret_name_or_value(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET_SEED)
    monkeypatch.setenv(stub.DUMP_ENV_NAMES_VAR, "1")
    spec = probe_spec(tmp_path, billing='"oauth"')
    result = acp_delegate.delegate(spec, task="t", acceptance="a", home=harness_home)
    echo = echoed(result)
    assert not set(echo["env_names"]) & config.SECRET_VARS
    assert SECRET_SEED not in result.summary
    assert echo["env_stub"][stub.DUMP_ENV_NAMES_VAR] == "1"


def test_session_meta_is_sent_with_the_derived_deny_and_the_folder_cannot_change_it(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(stub.DUMP_ENV_NAMES_VAR, "0")
    folder = tmp_path / "folder"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / "settings.json").write_text(PRE_APPROVING, encoding="utf-8")
    spec = probe_spec(
        tmp_path,
        cwd=json.dumps(str(folder)),
        deny='["execute", "delete"]',
        allow='["read", "edit"]',
        session_meta=CLAUDE_META,
        tool_names=TOOL_NAMES,
    )
    meta = echoed(acp_delegate.delegate(spec, task="t", acceptance="a", home=harness_home))["meta"]
    options = meta["claudeCode"]["options"]
    assert options["settingSources"] == []
    assert options["allowDangerouslySkipPermissions"] is False
    assert sorted(options["disallowedTools"]) == ["Bash", "Rm"]


def test_disallowed_tools_in_allowlist_mode_adds_every_mapped_kind_not_allowed(
    tmp_path: Path,
) -> None:
    spec = probe_spec(
        tmp_path,
        mode='"allowlist"',
        allow='["read"]',
        deny='["delete"]',
        session_meta=CLAUDE_META,
        tool_names=TOOL_NAMES,
    )
    assert acp_delegate.disallowed_tools(spec) == ("Rm", "Bash", "Edit", "Write")
    allowing = probe_spec(
        tmp_path,
        mode='"allow"',
        deny='["execute"]',
        session_meta=CLAUDE_META,
        tool_names=TOOL_NAMES,
    )
    assert acp_delegate.disallowed_tools(allowing) == ("Bash",)


def test_an_allowed_kind_keeps_a_tool_name_an_unallowed_kind_shares(tmp_path: Path) -> None:
    spec = probe_spec(
        tmp_path,
        mode='"allowlist"',
        allow='["execute"]',
        session_meta=CLAUDE_META,
        tool_names='{execute = ["Bash"], delete = ["Bash"], edit = ["Edit"]}',
    )
    assert acp_delegate.disallowed_tools(spec) == ("Edit",)
    assert "Bash" not in acp_delegate.disallowed_tools(spec)  # delete shares it with execute


def test_an_operator_written_disallowed_tools_is_unioned_not_replaced(
    tmp_path: Path, harness_home: Path
) -> None:
    meta = '{claudeCode = {options = {disallowedTools = ["WebFetch"]}}}'
    spec = probe_spec(
        tmp_path, deny='["execute"]', session_meta=meta, tool_names=TOOL_NAMES, mode='"allow"'
    )
    sent = echoed(acp_delegate.delegate(spec, task="t", acceptance="a", home=harness_home))["meta"]
    assert sorted(sent["claudeCode"]["options"]["disallowedTools"]) == ["Bash", "WebFetch"]


def test_no_session_meta_sends_no_meta(tmp_path: Path, harness_home: Path) -> None:
    result = acp_delegate.delegate(
        probe_spec(tmp_path), task="t", acceptance="a", home=harness_home
    )
    assert result.summary == stub.STUB_MESSAGE


def test_session_meta_and_tool_names_must_be_tables_of_the_right_shape(tmp_path: Path) -> None:
    for bad in ('session_meta = "x"', 'tool_names = {execute = "Bash"}', "tool_names = []"):
        path = write_agents(tmp_path, f'[agents.a]\ncommand = "a"\n{bad}\n')
        with pytest.raises(ConfigError):
            acp_config.load_agents(path)


def test_the_claude_code_template_carries_the_launch_settings_and_round_trips(
    tmp_path: Path,
) -> None:
    spec = acp_config.TEMPLATES["claude-code"].spec()
    options = spec.session_meta["claudeCode"]["options"]
    assert options["settingSources"] == []
    assert options["allowDangerouslySkipPermissions"] is False
    assert {"execute", "edit", "delete", "read", "search", "fetch"} <= set(spec.tool_names)
    path = write_agents(tmp_path, acp_config.render_entry(acp_config.TEMPLATES["claude-code"]))
    loaded = acp_config.load_agents(path)["claude-code"]
    assert loaded.session_meta == spec.session_meta
    assert loaded.tool_names == spec.tool_names
    for name in ("codex", "pi", "grok"):
        assert not acp_config.TEMPLATES[name].spec().session_meta


def _strings_of(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in [str(key), *_strings_of(item)]]
    if isinstance(value, list | tuple):
        return [s for item in value for s in _strings_of(item)]
    return []


def test_the_launch_settings_carry_no_auto_approve_value_and_the_bypass_flag_is_false() -> None:
    """Ticket 213: the AUTO_APPROVE scan now covers ``session_meta`` and ``tool_names`` values."""
    for template in acp_config.TEMPLATES.values():
        values = _strings_of(template.tool_names) + [
            s for s in _strings_of(template.session_meta) if s not in _bypass_keys(template)
        ]
        assert not [v for v in values if AUTO_APPROVE.search(v)], template.name
    options = acp_config.TEMPLATES["claude-code"].session_meta["claudeCode"]["options"]
    assert options["allowDangerouslySkipPermissions"] is False


def _bypass_keys(template: acp_config.AgentTemplate) -> set[str]:
    """Option *keys* may spell the word (the key that removes the bypass); values may not."""
    return {s for s in _strings_of(template.session_meta) if AUTO_APPROVE.search(s)} & {
        "allowDangerouslySkipPermissions"
    }


def test_a_cli_delegation_started_event_carries_the_jail_kind(tmp_path: Path) -> None:
    """Ticket 214: one new key on an existing event, and no new event name."""
    from harnessed.acp import jail

    bus = EventBus()
    observer = RecordingObserver()
    bus.subscribe(observer)
    spec = acp_config.AgentSpec(
        name="probe-cli",
        command=sys.executable,
        args=(str(CLI_STUB_PATH),),
        kind="cli",
        input="stdin",
        cwd=str(tmp_path),
    )
    cli_runner.run(spec, "do it", "done", bus=bus)
    names = [event.name for event in observer.events]
    assert names == ["acp.delegation.started", "acp.delegation.finished"]
    assert observer.events[0].payload["jail"] == jail.detect().kind


# ---- ticket 194: a recorded quota refusal is its own failure reason ------------------------------

REFUSED_DETAIL = (
    '-32603 Internal error ({"message":"API error (status 402 Payment Required): '
    'Grok Build usage balance exhausted","http_status":402})'
)


def quota_template_name() -> str:
    """The one template carrying ``quota_markers``, found by that data and never by its name."""
    named = [t.name for t in acp_config.TEMPLATES.values() if t.quota_markers]
    assert len(named) == 1
    return named[0]


def quota_spec(path: Path, name: str) -> acp_config.AgentSpec:
    target = stub_agents(path)
    target.write_text(
        target.read_text("utf-8").replace("[agents.probe]", f"[agents.{name}]"), encoding="utf-8"
    )
    return acp_config.agent_named(target, None)


def test_only_the_recorded_template_carries_quota_markers() -> None:
    marked = {t.name: t.quota_markers for t in acp_config.TEMPLATES.values() if t.quota_markers}
    assert marked == {quota_template_name(): {"http_status": (402,)}}
    assert acp_config.quota_markers_for(quota_template_name()) == {"http_status": (402,)}
    assert acp_config.quota_markers_for("probe") == {}


def test_a_marked_http_status_settles_quota_refused_and_raises_quota_refused(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "grok_quota")
    name = quota_template_name()
    bus = EventBus()
    seen = RecordingObserver()
    bus.subscribe(seen)
    with pytest.raises(QuotaRefused) as caught:
        acp_delegate.delegate(
            quota_spec(tmp_path, name), task="do it", acceptance="done", home=harness_home, bus=bus
        )
    assert isinstance(caught.value, DelegationError)
    assert str(caught.value) == f"the agent {name!r} refused for quota: {REFUSED_DETAIL}"
    finished = seen.named("acp.delegation.finished")
    assert finished[0].payload["status"] == "quota_refused"
    assert finished[0].payload["reason"] == REFUSED_DETAIL
    written = [json.loads(p.read_text("utf-8")) for p in (harness_home / "acp").glob("dlg_*.json")]
    assert [w["failure"] for w in written] == [f"quota_refused: {REFUSED_DETAIL}"]


def test_the_same_error_from_an_unmatched_entry_is_the_unnamed_failure(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "grok_quota")
    with pytest.raises(DelegationError) as caught:
        acp_delegate.delegate(
            probe_spec(tmp_path), task="do it", acceptance="done", home=harness_home
        )
    assert not isinstance(caught.value, QuotaRefused)
    assert str(caught.value) == f"the agent 'probe' failed: {REFUSED_DETAIL}"


def test_a_marked_entry_with_an_unmarked_internal_error_stays_unnamed(
    tmp_path: Path, harness_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_MODE", "internal_error")
    name = quota_template_name()
    with pytest.raises(DelegationError) as caught:
        acp_delegate.delegate(
            quota_spec(tmp_path, name), task="do it", acceptance="done", home=harness_home
        )
    assert not isinstance(caught.value, QuotaRefused)
    assert str(caught.value) == f"the agent {name!r} failed: -32603 Internal error"


# ---- ticket 234: the batch publish is gone; the ceiling pauses while a request is open -----


def test_delegate_no_longer_publishes_permission_events_in_a_batch() -> None:
    """Known defect (e): ``_finish`` published every request and decision after the run was over."""
    source = Path(acp_delegate.__file__).read_text(encoding="utf-8")
    finish = source[source.index("def _finish(") : source.index("_FAILURE_CAP")]
    assert "_REQUEST" not in finish and "_DECISION" not in finish
    assert "permissions" not in finish.replace("Permission requests", "")


def test_a_paused_ceiling_does_not_count_down_and_an_unpaused_one_does() -> None:
    async def forever() -> None:
        await asyncio.sleep(30)

    ticks: list[int] = []
    paused = {"now": True}

    def on_wait() -> None:
        ticks.append(len(ticks))
        if len(ticks) >= 6:
            paused["now"] = False  # after six slices the request is answered

    started = time.monotonic()
    with pytest.raises(DelegationTimeout):
        acp_runtime.submit(
            forever,
            1.0,
            on_wait=on_wait,
            pausing=lambda: paused["now"],
            on_timeout=lambda: None,
        )
    elapsed = time.monotonic() - started
    assert elapsed >= 1.0 + 5 * acp_runtime.POLL_S * 0.8, "the paused slices were not counted"


def test_the_dirty_tree_refusal_prints_the_path_as_stored(tmp_path: Path) -> None:
    """Ticket 269: a Windows-shaped path is not Python-quoted (single backslashes)."""
    from harnessed.acp import repo
    from harnessed.errors import DirtyTreeRefused

    stored = "C:\\Users\\...\\hqa6-repo"
    begun = repo.Start(
        cwd=stored, head="a" * 40, head_short="aaaaaaa", branch="main", dirty=True,
        dirty_paths=("README.md",),
    )  # fmt: skip
    spec = acp_config.AgentSpec(name="claude-code", command=sys.executable)
    with pytest.raises(DirtyTreeRefused) as caught:
        acp_delegate.refuse_dirty(spec, begun, None, "k", 1, None, allow_dirty=False)
    assert f"working tree at {stored} has" in str(caught.value)
    assert "\\\\" not in str(caught.value)
