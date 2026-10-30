"""FR-77: the control app serves ``POST`` and no ``GET``, beside a gateway that cannot write.

``control.create_app(home)`` is the write surface of decision 49: three ``POST`` routes over one
one-worker executor, its own copy of decision 24's error body, and no import from
``harnessed.gateway``. ``dashboard_app.create_app`` mounts it at ``/control/v1`` beside the read
gateway at ``/v1``; **mounted, not merged**, so the two proofs below - 0 non-``GET`` under ``/v1``
and 0 non-``POST`` under ``/control/v1`` - live in this one file and cannot rot apart.

This ticket stands the app up; the route behaviour is tickets 137 (open a run), 140 (continue) and
146 (configure a provider). The runs route here is thin but real: it opens the CLI's own session on
the worker thread and calls ``run_cmd.solve_with_memory`` directly, which is what lets the
redaction round-trip below write through ``db.connect()`` and ``redact.scrub`` like the CLI does.

The import half of the boundary proof (``IMPORT_TABLE``'s ``control`` row, and a seeded
``harnessed.gateway`` import failing it) is ``tests/test_boundaries.py``'s, beside every other row.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import Mount
from fastapi.testclient import TestClient

from harnessed import config, control, dashboard_app
from harnessed.control import errors as control_errors
from harnessed.control import worker
from harnessed.errors import (
    CredentialRefused,
    CursorError,
    HarnessError,
    LimitError,
    MissingCredentialError,
    ParameterError,
    PromptError,
    ProviderNotConfiguredError,
    RunInFlight,
    SuiteMismatch,
    SurfaceNotFound,
)
from harnessed.gateway import errors as gateway_errors
from harnessed.memory import db, redact
from harnessed.providers import registry
from harnessed.providers.types import ChatResponse

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = REPO_ROOT / "src" / "harnessed" / "control"
DASHBOARD_ROOT = REPO_ROOT / "dashboard"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "136"

GATEWAY_MOUNT = "/v1"
CONTROL_MOUNT = "/control/v1"
RUNS = "/control/v1/runs"

#: A key shape the redaction table knows, built in pieces so this file carries no credential.
SEEDED_KEY = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDDEEEEFFFF"

#: How long a test waits on the worker's future before calling the run stuck.
RUN_TIMEOUT_S = 10.0


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- benches -------------------------------------------------------------------------------------


class Replies:
    """An offline provider that answers every request with one line, optionally after a pause.

    FakeProvider selects fixtures by scope and has none for an ad-hoc prompt, so the round-trip
    registers this under ``fake`` instead: the session, the gate, the loop, the journal and the
    episode are all the CLI's own, and only the endpoint is stubbed.
    """

    pause_s = 0.0

    def __init__(self, settings: config.Settings) -> None:
        self.settings = settings

    def complete(self, request: object) -> ChatResponse:
        time.sleep(self.pause_s)
        return ChatResponse(response_id="resp_136", model="stub-model", content="noted")


class SlowReplies(Replies):
    pause_s = 0.3


@pytest.fixture
def home(clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A migrated home, the offline provider named, and the endpoint stubbed."""
    monkeypatch.setitem(registry.OFFLINE_FACTORIES, "fake", Replies)
    db.connect().close()
    return tmp_harnessed_home


@pytest.fixture
def app(home: Path) -> Any:
    return control.create_app(home)


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def route_table(routes: list[Any]) -> list[tuple[str, tuple[str, ...]]]:
    """Every route with methods, as ``(path, methods)`` in registration order."""
    return [
        (route.path, tuple(sorted(route.methods)))
        for route in routes
        if getattr(route, "methods", None)
    ]


def mount_at(app: Any, path: str) -> Mount:
    found = [route for route in app.routes if isinstance(route, Mount) and route.path == path]
    assert len(found) == 1, [getattr(route, "path", None) for route in app.routes]
    return found[0]


def finished(app: Any) -> Any:
    """Wait for the run in flight and return what the worker's future returned."""
    in_flight = app.state.worker.run
    assert in_flight is not None, "nothing was submitted"
    return in_flight.result(timeout=RUN_TIMEOUT_S)


def assert_error_body(response: Any, status: int, error: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error", "message"}, body
    assert body["error"] == error
    assert isinstance(body["message"], str) and body["message"]
    return dict(body)


# ---- AC 1: two route walks, one file -------------------------------------------------------------


def test_the_control_app_registers_post_and_nothing_else(app: Any) -> None:
    table = route_table(list(app.routes))
    assert table, "the control app carries no routes"
    assert [(path, methods) for path, methods in table if methods != ("POST",)] == []
    assert all(path.startswith(f"{CONTROL_MOUNT}/") for path, _ in table), table
    assert (RUNS, ("POST",)) in table


def test_the_mounted_gateway_registers_get_and_nothing_else_beside_it(home: Path) -> None:
    """The other half, in the same file: the read gateway inside the process that writes."""
    mounted = dashboard_app.create_app(home)
    paths = [route.path for route in mounted.routes if isinstance(route, Mount)]
    assert paths == [GATEWAY_MOUNT, CONTROL_MOUNT, ""], "both apps mount before the static /"

    reads = route_table(list(mount_at(mounted, GATEWAY_MOUNT).routes))
    writes = route_table(list(mount_at(mounted, CONTROL_MOUNT).routes))
    assert reads and writes
    assert [row for row in reads if row[1] != ("GET",)] == []
    assert [row for row in writes if row[1] != ("POST",)] == []
    assert writes == route_table(list(control.create_app(home).routes)), "mounted, not merged"

    lines = [f"{GATEWAY_MOUNT}  ->  gateway"]
    lines += [f"    {' '.join(methods):5} {path}" for path, methods in reads]
    lines += [f"{CONTROL_MOUNT}  ->  control"]
    lines += [f"    {' '.join(methods):5} {path}" for path, methods in writes]
    capture("route-walks.txt", "\n".join(lines) + "\n")


def test_a_post_answers_through_the_mount_at_the_path_the_table_spells(home: Path) -> None:
    """The control table spells its prefix, so the mount must not double it (as in ticket 124)."""
    with TestClient(dashboard_app.create_app(home), raise_server_exceptions=False) as client:
        assert_error_body(client.post(RUNS, json={"prompt": ""}), 400, "PromptError")
        assert client.post(f"{CONTROL_MOUNT}{RUNS}", json={"prompt": "x"}).status_code == 404


# ---- AC 3: no subprocess, no os.system, no typer -------------------------------------------------

#: What the control app may never name (MUST rule 16, decision 49).
FORBIDDEN_MODULES = frozenset({"subprocess", "typer"})


def shell_outs(root: Path) -> list[str]:
    """Every import of, or reference to, ``subprocess``, ``typer`` or ``os.system``."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                if node.module == "os":
                    names += [f"os.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                names = [f"{node.value.id}.{node.attr}"]
            found.extend(
                f"{where}:{node.lineno} {name}"
                for name in names
                if name.split(".")[0] in FORBIDDEN_MODULES or name == "os.system"
            )
    return found


def test_the_control_app_never_shells_out() -> None:
    sources = sorted(path.name for path in CONTROL_ROOT.rglob("*.py"))
    assert {"__init__.py", "app.py", "routes.py", "worker.py", "errors.py"} <= set(sources)
    found = shell_outs(CONTROL_ROOT)
    capture("shell-out-scan.txt", f"scanned {sources}\nfound {found}\n")
    assert found == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import subprocess\n", "subprocess"),
        ("from subprocess import run\n", "subprocess"),
        ("import os\nos.system('harnessed run x')\n", "os.system"),
        ("from os import system\n", "os.system"),
        ("import typer\n", "typer"),
        ("from harnessed.cli.run_cmd import run\nrun(typer.Argument)\n", "typer"),
    ],
)
def test_a_seeded_shell_out_is_reported(tmp_path: Path, source: str, expected: str) -> None:
    (tmp_path / "routes.py").write_text(source, encoding="utf-8")
    found = shell_outs(tmp_path)
    assert found and all(line.startswith("routes.py:") for line in found), found
    assert any(line.endswith(f" {expected}") for line in found), found


# ---- AC 4: one error body, never a 200 with an error ---------------------------------------------


def test_get_on_the_runs_route_is_a_405_in_the_one_body(client: TestClient) -> None:
    response = client.get(RUNS)
    body = assert_error_body(response, 405, "ControlError")
    capture("get-runs-405.txt", f"{response.status_code} {response.text}\n")
    assert "GET" in body["message"]


def test_a_path_no_route_claims_is_a_404_in_the_one_body(client: TestClient) -> None:
    assert_error_body(client.post(f"{CONTROL_MOUNT}/nothing"), 404, "SurfaceNotFound")


@pytest.mark.parametrize(
    "request_body",
    [
        pytest.param({"json": {"prompt": ""}}, id="empty"),
        pytest.param({"json": {"prompt": "   "}}, id="blank"),
        pytest.param({"json": {"prompt": 7}}, id="not-a-string"),
        pytest.param({"json": {}}, id="no-prompt"),
        pytest.param({"json": ["prompt"]}, id="not-an-object"),
        pytest.param({}, id="no-body"),
        pytest.param(
            {"content": b"{not json", "headers": {"content-type": "application/json"}},
            id="not-json",
        ),
    ],
)
def test_a_prompt_the_route_cannot_take_is_a_400_prompt_error(
    app: Any, client: TestClient, request_body: dict[str, Any]
) -> None:
    assert_error_body(client.post(RUNS, **request_body), 400, "PromptError")
    assert app.state.worker.run is None, "a refused prompt submitted nothing"


def test_a_second_run_while_one_is_in_flight_is_a_409_naming_it(
    app: Any, client: TestClient
) -> None:
    pending: Future[Any] = Future()
    app.state.worker.run = pending
    app.state.worker.session_id = "run_in_flight_136"
    body = assert_error_body(client.post(RUNS, json={"prompt": "again"}), 409, "RunInFlight")
    assert "run_in_flight_136" in body["message"]
    pending.set_result(None)


def test_an_unconfigured_provider_is_a_400_never_a_traceback(
    app: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HARNESSED_PROVIDER", raising=False)
    monkeypatch.chdir(tmp_path)
    assert_error_body(
        client.post(RUNS, json={"prompt": "hello"}), 400, "ProviderNotConfiguredError"
    )
    assert app.state.worker.run is not None and app.state.worker.run.done(), "the worker is free"


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (RunInFlight("run_x"), 409),
        (PromptError("empty"), 400),
        (CredentialRefused("refused: 401"), 400),
        (ProviderNotConfiguredError(), 400),
        (MissingCredentialError("groq", "GROQ_API_KEY"), 400),
        (SurfaceNotFound("no session"), 404),
        (HarnessError("anything else"), 500),
        (ValueError("a bug"), 500),
    ],
)
def test_the_control_status_map(exc: BaseException, status: int) -> None:
    assert control_errors.status_of(exc) == status


def test_the_control_and_gateway_maps_agree_on_every_class_both_can_raise() -> None:
    """A second file rather than an import, so a test is what keeps the two from drifting."""
    shared = (SurfaceNotFound, CursorError, LimitError, ParameterError, SuiteMismatch)
    for cls in shared:
        exc = cls("x")
        assert control_errors.status_of(exc) == gateway_errors.status_of(exc), cls.__name__
    assert control_errors.INTERNAL_ERROR == gateway_errors.INTERNAL_ERROR


def test_the_control_app_never_answers_2xx_with_an_error_field(
    app: Any, client: TestClient
) -> None:
    responses = [
        client.post(RUNS, json={"prompt": ""}),
        client.get(RUNS),
        client.post(f"{CONTROL_MOUNT}/nothing"),
        client.post(RUNS, json={"prompt": "hello"}),
    ]
    finished(app)
    for response in responses:
        body = response.json()
        if 200 <= response.status_code < 300:
            assert "error" not in body, body
        else:
            assert set(body) == {"error", "message"}, body


# ---- AC 5: one dashboard module writes, and only to /control/v1/ --------------------------------

_METHOD = re.compile(r"""\bmethod\s*:\s*(["'`])([^"'`]*)\1""")
_PATH_LITERAL = re.compile(r"""(["'`])(/[^"'`]*)\1""")


def test_one_dashboard_module_sends_a_non_get_and_only_to_the_control_app() -> None:
    writers: dict[str, list[str]] = {}
    for area in ("app", "components", "lib"):
        for path in sorted((DASHBOARD_ROOT / area).rglob("*")):
            if path.suffix not in {".ts", ".tsx"}:
                continue
            text = path.read_text(encoding="utf-8")
            methods = [m.group(2) for m in _METHOD.finditer(text) if m.group(2) != "GET"]
            if methods:
                writers[path.relative_to(DASHBOARD_ROOT).as_posix()] = [
                    m.group(2) for m in _PATH_LITERAL.finditer(text)
                ]
    assert list(writers) == ["lib/control-client.ts"], writers
    paths = writers["lib/control-client.ts"]
    assert paths and all(path.startswith(f"{CONTROL_MOUNT}/") for path in paths), paths


# ---- AC 6: the CLI's own connect and scrub -------------------------------------------------------


def test_a_seeded_key_in_a_prompt_is_stored_redacted(
    app: Any, client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real_connect = db.connect

    def counting(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append("connect")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(db, "connect", counting)
    response = client.post(RUNS, json={"prompt": f"remember {SEEDED_KEY} for me"})
    assert response.status_code == 202, response.text
    session_id = response.json()["session_id"]
    finished(app)
    assert opened[:1] == ["connect"], "the run opened the CLI's own migrating connection, once"
    # Ticket 247: once the run's future completes the worker submits consolidation's trigger as its
    # own job, and that job opens a connection of its own (a connection belongs to its thread).
    deadline = time.monotonic() + 15
    while app.state.worker.consolidating is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert app.state.worker.consolidating is not None
    app.state.worker.consolidating.result(timeout=15)
    assert opened == ["connect", "connect"], "the run's connection, then the trigger job's"

    with sqlite3.connect(home / db.DATABASE_FILENAME) as conn:
        stored = {
            "turns": [row[0] for row in conn.execute("SELECT request FROM turns")],
            "mem_episodic": [row[0] for row in conn.execute("SELECT text FROM mem_episodic")],
            "events": [row[0] for row in conn.execute("SELECT payload FROM events")],
        }
        run_ids = {row[0] for row in conn.execute("SELECT DISTINCT run_id FROM turns")}
    everything = "\n".join(text for rows in stored.values() for text in rows)
    capture("redaction-round-trip.txt", f"session {session_id}\n{everything}\n")
    assert stored["turns"] and stored["mem_episodic"], "the run wrote nothing to judge"
    assert run_ids == {session_id}
    assert SEEDED_KEY not in everything
    assert redact.token("anthropic_key") in stored["turns"][0]
    assert redact.token("anthropic_key") in stored["mem_episodic"][0]


def test_the_202_names_the_session_and_where_to_read_it(app: Any, client: TestClient) -> None:
    response = client.post(RUNS, json={"prompt": "hello"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"session_id", "stream", "run"}
    session_id = body["session_id"]
    assert body["stream"] == f"/v1/sessions/{session_id}/stream"
    assert body["run"] == f"/v1/runs/{session_id}"
    assert app.state.worker.session_id == session_id
    finished(app)


# ---- AC 7: one worker, shut down by the lifespan, nothing in flight after a restart --------------


def test_the_worker_is_one_single_thread_executor_on_app_state(app: Any) -> None:
    held = app.state.worker
    assert isinstance(held, worker.Worker)
    assert isinstance(held.executor, ThreadPoolExecutor)
    assert (
        held.executor._max_workers == 1 + worker.PARKED_MAX
    )  # the active run and its parked ones (ticket 234)
    assert held.run is None and held.session_id is None


def test_the_lifespan_waits_for_the_run_in_flight_and_shuts_the_worker(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry.OFFLINE_FACTORIES, "fake", SlowReplies)
    app = control.create_app(home)
    with TestClient(app) as client:
        assert client.post(RUNS, json={"prompt": "take your time"}).status_code == 202
        in_flight = app.state.worker.run
        assert in_flight is not None and not in_flight.done(), "the stub is slow on purpose"
    assert in_flight.done(), "shutdown(wait=True) let the run finish its turn"
    in_flight.result()
    with pytest.raises(RuntimeError):
        app.state.worker.executor.submit(print)

    restarted = control.create_app(home)
    assert restarted.state.worker.run is None, "a future does not survive a process"
    with TestClient(restarted) as client:
        assert client.post(RUNS, json={"prompt": "after the restart"}).status_code == 202
        finished(restarted)
    capture(
        "restart.txt",
        f"before: done={in_flight.done()}\nafter restart: run={None} then 202 accepted\n",
    )


def test_the_dashboard_process_runs_the_control_lifespan(home: Path) -> None:
    """A mounted app's lifespan is not run by Starlette, so the one process hands it over."""
    mounted = dashboard_app.create_app(home)
    control_app = mount_at(mounted, CONTROL_MOUNT).app
    with TestClient(mounted):
        pass
    with pytest.raises(RuntimeError):
        control_app.state.worker.executor.submit(print)
