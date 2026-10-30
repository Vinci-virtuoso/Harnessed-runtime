"""FR-65: one process mounts the UI and the read gateway (decision 43; MUST rules 22, 23).

``dashboard_app.create_app(home)`` builds one ASGI app: the shipped export at ``/`` and
``gateway.create_app(...)`` **mounted** at ``/v1``. Mounted, not merged, is the whole point: the
gateway is called with no new argument, keeps its own route table and opens its own read-only
connection per request, so FR-33's "0 non-``GET`` routes, connection opened read-only" stays
literally true of ``gateway/`` inside a process that will also carry a write surface (ticket 136
adds the ``/control/v1`` mount and the second half of that proof, in ``tests/test_control_app.py``).

The static half is the export ``tests/test_dashboard_export.py`` already keeps honest: a path no
exported file matches is the export's ``404.html``, never decision 24's error body, because it is
not the gateway answering. Next writes each page as ``<page>.html`` beside a ``<page>/`` metadata
directory, so a direct load of ``/runs`` has to find ``runs.html`` - that lookup is the one piece
of logic the static mount carries.

The last test is the evidence FR-44 asked for and FR-65 keeps: a frame published through the
event bus reaches a client of the real uvicorn server, with nothing between them, in under one
second. It is the only test here that opens a socket, and the socket guard admits it because the
server is loopback.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import Mount
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from harnessed import dashboard_app, gateway
from harnessed.events.bus import EventBus
from harnessed.loop.guardrails import NO_MORE_TOOL_CALLS
from harnessed.memory import db

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
EXPORT = PACKAGE_ROOT / "dashboard_static"
DASHBOARD_ROOT = REPO_ROOT / "dashboard"
NEXT_CONFIG = DASHBOARD_ROOT / "next.config.ts"
DRAWER = DASHBOARD_ROOT / "components" / "ProvenanceDrawer.tsx"
FETCH_PANEL = DASHBOARD_ROOT / "lib" / "fetch-panel.ts"
CONTROL_CLIENT = DASHBOARD_ROOT / "lib" / "control-client.ts"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "124"

GATEWAY_MOUNT = "/v1"
CONTROL_PREFIX = "/control/v1/"
STREAM_ROUTE = "/v1/sessions/{session_id}/stream"

#: FR-44's number, kept by FR-65: a published frame reaches the browser in under this.
FRAME_CEILING_S = 1.0

#: What the export names the not-found page and the pages this test loads directly.
NOT_FOUND_PAGE = "404.html"
DIRECT_LOADS = ("/traces", "/evals", "/memory")

_METHOD = re.compile(r"""\bmethod\s*:\s*(["'`])([^"'`]*)\1""")
_PATH_LITERAL = re.compile(r"""(["'`])(/[^"'`]*)\1""")


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- benches -------------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_harnessed_home: Path) -> Path:
    """A migrated database under a throwaway home, so every gateway read has a table to open."""
    db.connect().close()
    return tmp_harnessed_home


@pytest.fixture
def app(home: Path) -> Any:
    return dashboard_app.create_app(home)


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def mounts(app: Any) -> list[Mount]:
    return [route for route in app.routes if isinstance(route, Mount)]


def gateway_mount(app: Any) -> Mount:
    found = [mount for mount in mounts(app) if mount.path == GATEWAY_MOUNT]
    assert len(found) == 1, [mount.path for mount in mounts(app)]
    return found[0]


def route_table(routes: list[Any]) -> list[tuple[str, tuple[str, ...]]]:
    """Every route with methods, as ``(path, methods)`` in registration order."""
    return [
        (route.path, tuple(sorted(route.methods)))
        for route in routes
        if getattr(route, "methods", None)
    ]


def walk(app: Any) -> str:
    lines = []
    for mount in mounts(app):
        target = type(mount.app).__name__
        lines.append(f"{mount.path or '/'}  ->  {target}")
        # The gateway's own table spells ``/v1``; the mount adds no second prefix (item 1 of the
        # ticket's Built section), so the path printed is the path that answers.
        for path, methods in route_table(list(getattr(mount, "routes", []) or [])):
            lines.append(f"    {' '.join(methods):5} {path}")
    return "\n".join(lines) + "\n"


# ---- AC 1: mounted, not merged ------------------------------------------------------------------


def test_the_gateway_mount_is_registered_before_the_static_mount(app: Any) -> None:
    """A static catch-all can never shadow a route (decision 43): ``/v1`` and ``/control/v1``
    first, ``/`` last. The control mount's own proofs are ``tests/test_control_app.py``'s."""
    found = mounts(app)
    assert [mount.path for mount in found] == [GATEWAY_MOUNT, "/control/v1", ""]
    assert isinstance(found[-1].app, StaticFiles)
    assert Path(found[-1].app.directory).resolve() == EXPORT.resolve()
    assert found[-1].app.html is True
    # Nothing but the mounts: the process adds no route of its own.
    assert len(app.routes) == len(found), [type(route).__name__ for route in app.routes]
    capture("route-walk.txt", walk(app))


def test_zero_non_get_methods_are_registered_under_v1(app: Any) -> None:
    table = route_table(list(gateway_mount(app).routes))
    assert table, "the mounted gateway carries no routes"
    non_get = [(path, methods) for path, methods in table if methods != ("GET",)]
    assert non_get == []


def test_the_gateway_is_called_with_the_database_path_and_nothing_else(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``create_app`` gains no argument: one call, one positional path, no keyword (FR-65)."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real = gateway.create_app

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(gateway, "create_app", spy)
    mounted = dashboard_app.create_app(home)
    assert calls == [((home / db.DATABASE_FILENAME,), {})]
    sub = gateway_mount(mounted).app
    assert sub.state.database_path == (home / db.DATABASE_FILENAME).resolve()


def test_the_mounted_gateway_keeps_its_own_route_table(app: Any, home: Path) -> None:
    """The sub-app's routes are the standalone gateway's, path for path and method for method."""
    standalone = gateway.create_app(home / db.DATABASE_FILENAME)
    assert route_table(list(gateway_mount(app).routes)) == route_table(list(standalone.routes))
    # The gateway's paths already spell ``/v1``: the mount does not add a second one.
    assert all(path.startswith(GATEWAY_MOUNT) for path, _ in route_table(list(standalone.routes)))


def test_health_answers_through_the_mount_with_the_gateway_own_bytes(
    client: TestClient, home: Path
) -> None:
    mounted = client.get("/v1/health")
    assert mounted.status_code == 200, mounted.text
    with TestClient(gateway.create_app(home / db.DATABASE_FILENAME)) as standalone:
        assert mounted.content == standalone.get("/v1/health").content
    assert client.get("/v1/v1/health").status_code == 404, "the prefix is not doubled"


def test_a_read_opens_the_gateway_own_read_only_connection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``connect_readonly`` per request and never the migrating ``connect`` (decision 18)."""
    opened: list[sqlite3.Connection] = []
    real = db.connect_readonly

    def counting(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real(*args, **kwargs)
        opened.append(connection)
        return connection

    def refused(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        raise AssertionError("the mounted gateway reached for the migrating connect()")

    monkeypatch.setattr(db, "connect_readonly", counting)
    monkeypatch.setattr(db, "connect", refused)
    response = client.get("/v1/runs")
    assert response.status_code == 200, response.text
    assert len(opened) == 1


def test_a_gateway_miss_is_still_the_gateway_own_error_body(client: TestClient) -> None:
    """Under ``/v1`` a miss is decision 24's body: the static mount never answers there."""
    response = client.get("/v1/no-such-surface")
    assert response.status_code == 404
    body = json.loads(response.content)
    assert set(body) == {"error", "message"}
    assert body["error"] == "SurfaceNotFound"


# ---- AC 2: the export answers under ``/`` -----------------------------------------------------


def test_an_unmatched_path_serves_the_export_404_page_and_not_the_error_body(
    client: TestClient,
) -> None:
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert response.content == (EXPORT / NOT_FOUND_PAGE).read_bytes()
    with pytest.raises(ValueError):
        json.loads(response.content)


def test_the_root_serves_the_export_index(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.content == (EXPORT / "index.html").read_bytes()


@pytest.mark.parametrize("page", DIRECT_LOADS)
def test_a_page_loaded_directly_finds_its_exported_html(client: TestClient, page: str) -> None:
    """Next writes ``/traces`` as ``traces.html`` beside a ``traces/`` metadata directory."""
    response = client.get(page)
    assert response.status_code == 200, response.text[:200]
    assert response.headers["content-type"].startswith("text/html")
    assert response.content == (EXPORT / f"{page.lstrip('/')}.html").read_bytes()


def test_a_built_asset_is_served_from_the_export(client: TestClient) -> None:
    asset = next(path for path in sorted((EXPORT / "_next").rglob("*.js")) if path.is_file())
    response = client.get(f"/{asset.relative_to(EXPORT).as_posix()}")
    assert response.status_code == 200
    assert response.content == asset.read_bytes()


def test_a_write_to_the_static_mount_is_refused(client: TestClient) -> None:
    assert client.post("/", content=b"{}").status_code == 405


# ---- AC 3 to 5: what the dashboard tree spells ------------------------------------------------


def test_next_config_drops_compress_false() -> None:
    """No proxy hop remains to buffer SSE; uvicorn serves the stream directly."""
    assert "compress" not in NEXT_CONFIG.read_text(encoding="utf-8")


def test_the_drawer_keeps_the_request_own_url_and_offers_the_cli_twin_not_curl() -> None:
    """The drawer reads the response's own URL and never a transport one (decision 29 superseded).

    Its copyable line was a `curl -s` at `/v1` until ticket 191 (MUST rule 31): no endpoint string
    is drawn anywhere, so the one copyable line is the `harnessed` CLI twin from `lib/cli-twins.ts`.
    """
    drawer = DRAWER.read_text(encoding="utf-8")
    assert "response.url" in drawer
    assert "curl -s " not in drawer
    assert "cliTwin" in drawer
    assert "/gw" not in drawer
    sources = [
        path
        for area in ("app", "components", "lib")
        for path in sorted((DASHBOARD_ROOT / area).rglob("*"))
        if path.suffix in {".ts", ".tsx"}
    ]
    naming = [
        path.relative_to(DASHBOARD_ROOT).as_posix()
        for path in sources
        if "gatewayUrl" in path.read_text(encoding="utf-8")
    ]
    assert naming == [], "the transport URL is gone: a response has only its own URL"
    fetch_panel = FETCH_PANEL.read_text(encoding="utf-8")
    assert "url: string" in fetch_panel


def test_the_control_client_is_a_post_only_module_against_control_v1() -> None:
    """The one module allowed a non-``GET`` method, and only at ``/control/v1/**`` (rule 22)."""
    text = CONTROL_CLIENT.read_text(encoding="utf-8")
    methods = [match.group(2) for match in _METHOD.finditer(text)]
    assert methods, "the control client spells no method at all"
    assert set(methods) == {"POST"}
    paths = [match.group(2) for match in _PATH_LITERAL.finditer(text)]
    assert paths, "the control client names no route"
    assert all(path.startswith(CONTROL_PREFIX) for path in paths), paths


# ---- Evidence: a frame reaches a real client of the real server in under a second --------------


class Served:
    """A uvicorn server over ``app`` on an ephemeral loopback port, run on its own thread."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        self.config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="off"
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


def frames(lines: Iterator[str]) -> Iterator[dict[str, str]]:
    """Frames off a line iterator: ``id:``, ``event:``, ``data:``, then a blank."""
    fields: dict[str, str] = {}
    for line in lines:
        if line == "":
            if fields:
                yield fields
            fields = {}
            continue
        key, value = line.split(": ", 1)
        fields[key] = value


def test_a_published_frame_reaches_a_client_of_the_real_server_in_under_a_second(
    home: Path,
) -> None:
    conn = db.connect()
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake", "prompt": "frame timing"})
    app = dashboard_app.create_app(home)
    latencies: list[float] = []
    try:
        with (
            Served(app) as origin,
            httpx.Client(base_url=origin, timeout=httpx.Timeout(5.0)) as client,
            client.stream("GET", STREAM_ROUTE.format(session_id=bus.run_id)) as response,
        ):
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            seen = frames(response.iter_lines())
            first = next(seen)
            assert first["event"] == "run.started"
            for iteration in (1, 2, 3):
                started = time.perf_counter()
                bus.publish("loop.iteration.started", {"iteration": iteration})
                frame = next(seen)
                latencies.append(time.perf_counter() - started)
                assert frame["event"] == "loop.iteration.started"
            bus.publish("run.finished", {"iterations": 3, "guardrail": NO_MORE_TOOL_CALLS})
            assert next(seen)["event"] == "run.finished"
    finally:
        conn.close()
    capture(
        "sse-frame-latency.txt",
        "publish -> frame at a real uvicorn client, no proxy between them\n"
        + "".join(
            f"iteration {i + 1}: {latency * 1000:.1f} ms\n" for i, latency in enumerate(latencies)
        )
        + f"ceiling: {FRAME_CEILING_S * 1000:.0f} ms\n",
    )
    assert all(latency < FRAME_CEILING_S for latency in latencies), latencies
