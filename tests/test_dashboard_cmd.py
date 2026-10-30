"""FR-66: ``harnessed dashboard`` is one command, refuses a non-loopback bind, ``doctor`` says so.

Ticket 125, the close of phase A. ``dashboard`` is rung 4 and is shaped exactly as decision 21
shapes ``serve``: resolve settings, build the one app, render the one startup line, hand it to
uvicorn. The default port is the gateway's, 8756, so every ``curl`` line in v3's evidence still
resolves; ``serve`` stays, unchanged, as the gateway-only command.

The one difference between the two commands is the thing this file exists to make a fact rather
than a claim (MUST rule 12): on a host that ``serve_cmd._is_loopback`` rejects, ``serve`` warns
and binds, because a read-only ``GET`` surface has nothing to authorize, and ``dashboard``
**refuses**, because ``/control/v1`` will be mounted beside ``/v1`` and there is no auth. The two
are tested side by side here, the refusal is proven by counting the sockets the process
constructed (0) and the calls ``uvicorn.run`` received (0), and the predicate consulted is
``serve_cmd``'s own, proven by replacing it and watching the refusal follow (MUST rule 18: a
second loopback predicate is how a guard silently loosens).

``doctor``'s rung 4 stops being the constant ``v3`` and becomes a check: the export present in
the installed package, else ``broken`` naming the build command, and the mounted apps' routes
enumerated off the app the way rung 3 enumerates the gateway's (FR-42), never by string.
"""

from __future__ import annotations

import ast
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harnessed import dashboard_app, gateway
from harnessed.cli import dashboard_cmd, doctor_cmd, serve_cmd
from harnessed.cli.app import main
from harnessed.config import load
from harnessed.errors import ExportMissing, GatewayExtraMissing
from harnessed.memory import db

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
DASHBOARD_CMD = PACKAGE_ROOT / "cli" / "dashboard_cmd.py"
DASHBOARD_ROOT = REPO_ROOT / "dashboard"
PLAYWRIGHT_CONFIG = DASHBOARD_ROOT / "playwright.config.ts"
E2E_HARNESS = DASHBOARD_ROOT / "tests" / "e2e" / "harness.ts"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "125"

#: The gateway's port, kept so v3's ``curl`` evidence still resolves (decision 46).
DEFAULT_PORT = 8756

#: What ``dashboard`` names when the export was never staged (decision 44).
BUILD_COMMAND = "npm --prefix dashboard run build"

#: Hosts ``serve_cmd._is_loopback`` rejects: addresses off loopback, the unspecified addresses
#: of both families, and hostnames, which are not addresses at all.
REFUSED_HOSTS = ("0.0.0.0", "192.168.1.5", "::", "localhost", "example.invalid")

#: Literal loopback addresses of both families; ``127.0.0.2`` is the whole ``/8``.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "127.0.0.2")

#: The e2e harness's one command and the ports decision 30 retires with it (FR-66).
E2E_PORT = 18756
RETIRED_PORTS = (18757, 18758, 18759)

#: How long a real process is given to answer its health route.
START_TIMEOUT_S = 30.0


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- benches -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shell:
    """What an operator saw: the exit code, stdout and stderr, kept apart."""

    code: int
    out: str
    err: str


def shell(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Shell:
    """Run one command through ``main`` - argv and all - so the error contract is exercised."""
    monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_info:
        code = 0 if exit_info.code is None else int(exit_info.code)
    captured = capsys.readouterr()
    return Shell(code=code, out=captured.out, err=captured.err)


@dataclass
class Bound:
    """What ``uvicorn.run`` was handed, recorded instead of binding a port."""

    calls: list[dict[str, Any]]


@pytest.fixture
def uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> Bound:
    import uvicorn

    bound = Bound(calls=[])

    def record(app: Any, **kwargs: Any) -> None:
        bound.calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", record)
    return bound


@dataclass
class Sockets:
    """Every socket the process constructed while the command ran, whatever it was for."""

    opened: list[tuple[Any, ...]]


@pytest.fixture
def sockets(monkeypatch: pytest.MonkeyPatch) -> Sockets:
    """Count socket construction itself, so "0 sockets" is a measurement rather than a claim.

    ``uvicorn.run`` is recorded by another fixture, but a refusal that came after a bind would
    still show 0 calls to it: this counts lower, at ``socket.socket``, which every bind, every
    listen and every connect goes through.
    """
    seen = Sockets(opened=[])
    original = socket.socket

    class Counted(original):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.opened.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(socket, "socket", Counted)
    return seen


@pytest.fixture
def export_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the package's export lookup at a directory nothing staged."""
    absent = tmp_path / "never-staged"
    monkeypatch.setattr(dashboard_app, "export_directory", lambda: absent)
    return absent


@pytest.fixture
def extra_absent(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stage the extra being absent: the gateway package and uvicorn are unimportable."""
    for name in list(sys.modules):
        if name in {"harnessed.gateway", "harnessed.dashboard_app"} or name.startswith(
            "harnessed.gateway."
        ):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "harnessed.gateway", None)
    monkeypatch.setitem(sys.modules, "harnessed.dashboard_app", None)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    yield


def rung_four(settings: Any = None) -> doctor_cmd.Rung:
    rung = doctor_cmd.run_rungs(load() if settings is None else settings)[3]
    assert rung.number == 4
    assert rung.name == "Dashboard"
    return rung


# ---- AC 1: one line, then 8756 by default -------------------------------------------------------


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_dashboard_prints_one_line_naming_the_url_and_the_database_then_serves_on_8756(
    uvicorn_run: Bound, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = shell(["dashboard"], monkeypatch, capsys)
    assert seen.code == 0, seen.err
    assert len(uvicorn_run.calls) == 1
    call = uvicorn_run.calls[0]
    assert call["host"] == serve_cmd.DEFAULT_HOST
    assert call["port"] == DEFAULT_PORT
    assert call["log_config"] is None
    assert call["access_log"] is False
    lines = [line for line in seen.out.splitlines() if line.strip()]
    assert len(lines) == 1, seen.out
    assert f"http://127.0.0.1:{DEFAULT_PORT}" in lines[0]
    assert str(db.database_path().resolve()) in lines[0]
    assert seen.err == ""
    # The app handed to uvicorn is the one process: the export at ``/`` and the gateway at ``/v1``.
    with TestClient(call["app"]) as client:
        assert client.get("/v1/health").status_code == 200
        home = client.get("/")
        assert home.status_code == 200
        assert "<html" in home.text
    capture("startup-line.txt", f"$ harnessed dashboard\n{seen.out}")


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_dashboard_takes_a_port_and_the_config_parity_flags(
    uvicorn_run: Bound, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = shell(
        ["dashboard", "--port", "9010", "--provider", "fake", "--token-budget", "1234"],
        monkeypatch,
        capsys,
    )
    assert seen.code == 0, seen.err
    assert uvicorn_run.calls[0]["port"] == 9010
    assert "http://127.0.0.1:9010" in seen.out


# ---- AC 2: a non-loopback bind is refused before any socket, and serve warns ---------------------


@pytest.mark.parametrize("host", REFUSED_HOSTS)
@pytest.mark.usefixtures("tmp_harnessed_home")
def test_dashboard_refuses_a_non_loopback_host_before_any_socket_is_opened(
    host: str,
    uvicorn_run: Bound,
    sockets: Sockets,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert not serve_cmd._is_loopback(host)
    seen = shell(["dashboard", "--host", host], monkeypatch, capsys)
    assert seen.code != 0
    assert uvicorn_run.calls == []
    assert sockets.opened == []
    assert "control" in seen.err.lower(), seen.err
    assert host in seen.err
    assert "Traceback" not in seen.err
    assert seen.out == ""
    if host == REFUSED_HOSTS[0]:
        capture(
            "refused-bind.txt",
            f"$ harnessed dashboard --host {host}\nexit {seen.code}\n{seen.err}\n"
            f"uvicorn.run calls: {len(uvicorn_run.calls)}\n"
            f"sockets constructed: {len(sockets.opened)}\n",
        )


@pytest.mark.parametrize("host", LOOPBACK_HOSTS)
@pytest.mark.usefixtures("tmp_harnessed_home")
def test_dashboard_binds_any_literal_loopback_address(
    host: str,
    uvicorn_run: Bound,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert ipaddress.ip_address(host).is_loopback
    seen = shell(["dashboard", "--host", host], monkeypatch, capsys)
    assert seen.code == 0, seen.err
    assert uvicorn_run.calls[0]["host"] == host


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_serve_warns_on_the_same_host_and_binds_while_dashboard_refuses(
    uvicorn_run: Bound,
    sockets: Sockets,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The two commands, one host, one file: the difference is a fact, not a claim."""
    host = REFUSED_HOSTS[0]
    served = shell(["serve", "--host", host, "--port", "9001"], monkeypatch, capsys)
    assert served.code == 0, served.err
    assert len(uvicorn_run.calls) == 1
    assert uvicorn_run.calls[0]["host"] == host
    assert "warning" in served.err.lower()
    assert "auth" in served.err.lower()

    refused = shell(["dashboard", "--host", host, "--port", "9001"], monkeypatch, capsys)
    assert refused.code != 0
    assert len(uvicorn_run.calls) == 1, "dashboard reached uvicorn"
    assert "warning" not in refused.err.lower()
    assert "control" in refused.err.lower()
    capture(
        "serve-vs-dashboard.txt",
        f"$ harnessed serve --host {host} --port 9001\nexit {served.code}\n{served.err}"
        f"uvicorn.run calls after serve: 1\n\n"
        f"$ harnessed dashboard --host {host} --port 9001\nexit {refused.code}\n{refused.err}"
        f"uvicorn.run calls after dashboard: {len(uvicorn_run.calls)} (unchanged)\n",
    )


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_dashboard_consults_serve_cmd_own_loopback_predicate(
    uvicorn_run: Bound, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Replace ``serve_cmd._is_loopback`` and the refusal follows it: one predicate, reused."""
    asked: list[str] = []

    def never(host: str) -> bool:
        asked.append(host)
        return False

    monkeypatch.setattr(serve_cmd, "_is_loopback", never)
    seen = shell(["dashboard"], monkeypatch, capsys)
    assert asked == [serve_cmd.DEFAULT_HOST]
    assert seen.code != 0
    assert uvicorn_run.calls == []


def test_dashboard_cmd_defines_no_loopback_predicate_of_its_own() -> None:
    """MUST rule 18 in the source: no ``ipaddress``, no second ``_is_loopback``."""
    tree = ast.parse(DASHBOARD_CMD.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "ipaddress" not in imported
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not any("loopback" in name for name in defined), defined


# ---- AC 3: no export ----------------------------------------------------------------------------


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_dashboard_without_the_export_exits_non_zero_naming_the_build_command(
    export_absent: Path,
    uvicorn_run: Bound,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = shell(["dashboard"], monkeypatch, capsys)
    assert seen.code != 0
    assert BUILD_COMMAND in seen.err
    assert str(export_absent) in seen.err
    assert "Traceback" not in seen.err
    assert uvicorn_run.calls == []
    assert ExportMissing.remedy == BUILD_COMMAND
    capture("export-missing.txt", f"$ harnessed dashboard\nexit {seen.code}\n{seen.err}")


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_doctor_reports_rung_four_broken_without_the_export(
    export_absent: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rung = rung_four()
    assert rung.state == "broken"
    assert rung.broken
    assert BUILD_COMMAND in rung.detail
    seen = shell(["doctor", "--json"], monkeypatch, capsys)
    assert seen.code != 0
    payload = json.loads(seen.out)
    assert payload["ok"] is False
    assert payload["rungs"][3]["state"] == "broken"
    assert BUILD_COMMAND in payload["rungs"][3]["detail"]


@pytest.mark.usefixtures("tmp_harnessed_home", "extra_absent")
def test_dashboard_without_the_extra_names_the_extra_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = shell(["dashboard"], monkeypatch, capsys)
    assert seen.code != 0
    assert GatewayExtraMissing.remedy in seen.err
    assert "Traceback" not in seen.err
    assert "ImportError" not in seen.err


# ---- AC 4: doctor enumerates the mounted routes -------------------------------------------------


@pytest.mark.usefixtures("clean_env", "tmp_harnessed_home")
def test_doctor_rung_four_is_live_and_enumerates_the_mounted_apps_routes(
    tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``clean_env``: ``seen.code == 0`` below needs a configured provider (rung 1), which this
    test is not about - every other CLI-invoking test takes the fixture for the same reason."""
    rung = rung_four()
    assert rung.state == "live", rung.detail
    assert dashboard_cmd.COMMAND in rung.detail
    assert "v2" not in rung.detail
    # The count in the detail is the gateway's own route count, read off the app.
    mounted = gateway.create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    assert dashboard_app.GATEWAY_MOUNT in rung.detail
    assert re.search(rf"\b{len(mounted.routes)} routes\b", rung.detail), rung.detail
    assert str(dashboard_app.export_directory()) in rung.detail

    seen = shell(["doctor", "--json"], monkeypatch, capsys)
    assert seen.code == 0, seen.err
    payload = json.loads(seen.out)
    fourth = payload["rungs"][3]
    assert fourth == {"number": 4, "name": "Dashboard", "state": "live", "detail": rung.detail}
    capture("doctor-json.txt", json.dumps(payload, indent=2) + "\n")

    human = shell(["doctor"], monkeypatch, capsys)
    assert human.code == 0, human.err
    assert "rung 4" in human.out
    assert "v3" not in human.out
    capture("doctor.txt", human.out)


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_doctor_rung_four_is_broken_when_the_mounted_gateway_registers_zero_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enumerated, never compared: an app that mounts but registers nothing is not ``live``."""

    def empty(database_path: Path) -> FastAPI:
        return FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    monkeypatch.setattr(gateway, "create_app", empty)
    rung = rung_four()
    assert rung.state == "broken"
    assert "0 routes" in rung.detail


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_doctor_rung_four_counts_the_routes_the_app_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []
    real = gateway.create_app

    def counted(database_path: Path) -> FastAPI:
        app = real(database_path)
        seen.append(len(app.routes))
        return app

    monkeypatch.setattr(gateway, "create_app", counted)
    rung = rung_four()
    assert rung.state == "live"
    assert seen and f"{seen[0]} routes" in rung.detail


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_doctor_rung_four_is_not_yet_when_the_extra_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "harnessed.gateway", None)
    rung = rung_four()
    assert rung.state == "not yet"
    assert not rung.broken
    assert GatewayExtraMissing.remedy in rung.detail


def test_no_rung_state_is_the_v3_constant_any_more() -> None:
    """Rung 4 is a check now; the constant state and its rendering are gone."""
    from harnessed.cli import render

    assert "v3" not in doctor_cmd.RungState.__args__
    assert "v3" not in render._RUNG_MARK


# ---- AC 5: no database on disk -----------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.slow
def test_the_process_starts_with_no_database_on_disk_and_serves_the_ui_and_health(
    tmp_path: Path, clean_env: None
) -> None:
    """A real ``harnessed dashboard`` over an empty home: ``/`` and ``/v1/health`` answer 200,
    and the first data read is decision 24's ``StorageError`` body, which is what FR-54's Path C
    renders. The startup line is read off the real process's stdout."""
    home = tmp_path / "home"
    home.mkdir()
    assert not (home / db.DATABASE_FILENAME).exists()
    port = free_port()
    env = {
        **os.environ,
        "HARNESSED_HOME": str(home),
        "HARNESSED_PROVIDER": "fake",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "harnessed", "dashboard", "--port", str(port)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    origin = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + START_TIMEOUT_S
        health = None
        with httpx.Client(base_url=origin, timeout=2.0) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    health = client.get("/v1/health")
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
            assert process.poll() is None, process.stderr.read() if process.stderr else ""
            assert health is not None and health.status_code == 200, health
            index = client.get("/")
            assert index.status_code == 200
            assert "<html" in index.text
            first_read = client.get("/v1/eval/runs")
            assert first_read.status_code != 200
            assert first_read.json()["error"] == "StorageError"
        assert not (home / db.DATABASE_FILENAME).exists(), "the process wrote a database"
        assert process.stdout is not None
        startup = process.stdout.readline()
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(10)
    assert f"http://127.0.0.1:{port}" in startup
    assert str((home / db.DATABASE_FILENAME).resolve()) in startup
    capture(
        "process-no-database.txt",
        f"$ harnessed dashboard --port {port}   (HARNESSED_HOME={home}, no harness.db)\n"
        f"{startup}"
        f"GET / -> {index.status_code}\n"
        f"GET /v1/health -> {health.status_code} {health.text}\n"
        f"GET /v1/eval/runs -> {first_read.status_code} {first_read.text}\n",
    )


# ---- AC 6: the e2e harness starts exactly one command -----------------------------------------


def harness_constants() -> dict[str, str]:
    text = E2E_HARNESS.read_text(encoding="utf-8")
    return dict(re.findall(r"export const (\w+) = ([^;]+);", text))


def test_the_e2e_web_server_starts_exactly_one_command_the_dashboard_on_18756() -> None:
    config = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
    commands = re.findall(r"command:\s*(`[^`]*`|\"[^\"]*\")", config)
    assert len(commands) == 1, commands
    command = commands[0]
    matched = re.fullmatch(r"`uv run harnessed dashboard --port \$\{(\w+)\}`", command)
    assert matched, command
    constants = harness_constants()
    assert constants[matched.group(1)] == str(E2E_PORT)
    assert "next start" not in config
    for text in (config, E2E_HARNESS.read_text(encoding="utf-8")):
        for port in RETIRED_PORTS:
            assert str(port) not in text, port
    assert "DOWN_" not in config
