"""FR-31: a gateway package the six-stage harness accepts.

The gateway is the first package added since the boundary scan and ``mypy --strict`` became the
gate, and this file proves the skeleton every later route ticket stands on: the ``gateway`` extra
is pinned, ``create_app()`` answers ``/v1/health``, every byte the app writes is
``serialize.canonical()`` of the payload the handler returned, and every error leaves through the
one body shape of decision 24 with a non-2xx status. ``harnessed serve`` reaches the package only
inside its function body, so an absent extra is a named ``HarnessError`` an operator can read
rather than an ``ImportError`` they have to decode (decision 21).

The extra is present here (``uv sync --all-extras``), so the missing-extra path is staged by
poisoning ``sys.modules`` for one test, which is how ``import fastapi`` raises without anything
being uninstalled.
"""

from __future__ import annotations

import ast
import ipaddress
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harnessed.cli.app import main
from harnessed.errors import (
    ConfigError,
    CursorError,
    EvalError,
    GatewayError,
    GatewayExtraMissing,
    HarnessError,
    LimitError,
    ParameterError,
    SuiteMismatch,
    SurfaceNotFound,
)
from harnessed.gateway import create_app
from harnessed.memory import db
from harnessed.serialize import canonical

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
GATEWAY_ROOT = PACKAGE_ROOT / "gateway"
SERVE_CMD = PACKAGE_ROOT / "cli" / "serve_cmd.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "064"

#: What the health route must carry (decision 24): the schema version, the absolute database
#: path and the route count.
HEALTH_KEYS = frozenset({"schema_version", "database_path", "route_count"})


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


@dataclass(frozen=True)
class Shell:
    """What an operator saw: the exit code, stdout and stderr, kept apart."""

    code: int
    out: str
    err: str


def shell(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Shell:
    """Run one command through ``main`` - argv and all - so the error contract is exercised.

    ``CliRunner`` invokes the Typer app directly and records an escaping ``HarnessError`` on the
    result object without rendering it; ``main`` is what an operator runs.
    """
    monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_info:
        code = 0 if exit_info.code is None else int(exit_info.code)
    captured = capsys.readouterr()
    return Shell(code=code, out=captured.out, err=captured.err)


def _imports_in(path: Path, *, module_level_only: bool = False) -> list[tuple[str, int]]:
    """Every imported dotted name in one file, with its line, from the AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = tree.body if module_level_only else list(ast.walk(tree))
    found: list[tuple[str, int]] = []
    for node in nodes:
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found.extend((f"{base}.{alias.name}", node.lineno) for alias in node.names)
    return found


def _names_top(name: str, *tops: str) -> bool:
    head = name.split(".")[0]
    return head in tops


@pytest.fixture
def app_and_home(tmp_harnessed_home: Path) -> Iterator[tuple[Any, Path]]:
    """The app built over the throwaway home's database path, which is never opened here."""
    yield create_app(tmp_harnessed_home / db.DATABASE_FILENAME), tmp_harnessed_home


@pytest.fixture
def client(app_and_home: tuple[Any, Path]) -> Iterator[TestClient]:
    app, _ = app_and_home
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# ---------------------------------------------------------------------------
# 1. Stage 1 provisions the surface: the extras and their ranges
# ---------------------------------------------------------------------------


def _extras() -> dict[str, list[str]]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    extras: dict[str, list[str]] = project["optional-dependencies"]
    return extras


def test_the_gateway_extra_pins_fastapi_and_uvicorn_at_the_architecture_ranges() -> None:
    gateway = _extras()["gateway"]
    assert "fastapi>=0.141,<0.142" in gateway
    assert "uvicorn>=0.52,<0.53" in gateway
    assert len(gateway) == 2, "the gateway extra adds exactly two runtime dependencies"


def test_httpx_arrives_with_the_dev_extra_and_nothing_else_new() -> None:
    dev = _extras()["dev"]
    assert "httpx>=0.28,<0.29" in dev
    runtime = {spec.split(">=")[0] for spec in _extras()["gateway"]}
    assert runtime == {"fastapi", "uvicorn"}


def test_nothing_under_src_imports_httpx_starlette_or_pydantic() -> None:
    """The three transitive arrivals are imported by nothing in the package (tech stack)."""
    offenders = [
        (path.relative_to(PACKAGE_ROOT).as_posix(), name, line)
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        for name, line in _imports_in(path)
        if _names_top(name, "httpx", "starlette", "pydantic")
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# 2. The arrow points one way, into the gateway
# ---------------------------------------------------------------------------


#: The one module outside ``cli/`` that may import the gateway: the process that mounts it at
#: ``/v1`` (decision 43, FR-65). It is itself imported only by ``cli``, which
#: ``tests/test_boundaries.py`` asserts, so the arrow still points one way.
GATEWAY_MOUNTER = "dashboard_app.py"


def gateway_importers(package: Path) -> set[str]:
    """Every module outside ``gateway/`` under ``package`` that imports the gateway."""
    gateway = package / "gateway"
    return {
        path.relative_to(package).as_posix()
        for path in sorted(package.rglob("*.py"))
        if not path.is_relative_to(gateway)
        for name, _ in _imports_in(path)
        if name == "harnessed.gateway" or name.startswith("harnessed.gateway.")
    }


def outside_the_permitted(importers: set[str]) -> list[str]:
    return sorted(
        module
        for module in importers
        if not (module.startswith("cli/") or module == GATEWAY_MOUNTER)
    )


def test_only_cli_and_the_mounting_process_import_the_gateway() -> None:
    """No existing area gains permission to import ``gateway``; ``cli`` and the mounter have it."""
    importers = gateway_importers(PACKAGE_ROOT)
    assert importers, "the scan must see the one importer it permits, or it is vacuous"
    assert GATEWAY_MOUNTER in importers, "the scan must see the mounter, or it is vacuous"
    assert outside_the_permitted(importers) == [], sorted(importers)


def test_a_second_importer_outside_cli_fails_the_pin(tmp_path: Path) -> None:
    package = tmp_path / "harnessed"
    (package / "cli").mkdir(parents=True)
    (package / "loop").mkdir()
    (package / "gateway").mkdir()
    (package / "gateway" / "app.py").write_text("", encoding="utf-8")
    (package / GATEWAY_MOUNTER).write_text("from harnessed.gateway import app\n", encoding="utf-8")
    (package / "cli" / "serve_cmd.py").write_text(
        "from harnessed.gateway import app\n", encoding="utf-8"
    )
    assert outside_the_permitted(gateway_importers(package)) == []
    (package / "loop" / "engine.py").write_text(
        "from harnessed.gateway.app import create_app\n", encoding="utf-8"
    )
    assert outside_the_permitted(gateway_importers(package)) == ["loop/engine.py"]


def test_the_gateway_imports_neither_pydantic_nor_logging() -> None:
    """Decisions 19 and 22, asserted from the AST over every module under ``gateway/``."""
    offenders = [
        (path.relative_to(GATEWAY_ROOT).as_posix(), name, line)
        for path in sorted(GATEWAY_ROOT.rglob("*.py"))
        for name, line in _imports_in(path)
        if _names_top(name, "pydantic", "logging")
    ]
    assert offenders == []


def test_the_gateway_reaches_only_the_areas_its_row_permits() -> None:
    """The row: evals, improve, memory, loop, events, config, plus the leaves, plus the named
    modules ``providers/pricing.py`` (``GET /v1/prices``, FR-73, ticket 132), ``acp/probes.py``
    (``GET /v1/agents``, FR-90, ticket 148), ``router/decisions.py`` (``GET
    /v1/sessions/{id}/decisions``, FR-120, ticket 180) and ``acp/outputs.py`` (``GET
    /v1/outputs/{output_id}``, FR-127, ticket 181). The module, not its package:
    ``providers.registry`` from here is still an offender."""
    permitted = ("evals", "improve", "memory", "loop", "events", "config")
    leaves = {"clock", "config", "errors", "ids", "serialize"}
    modules = {
        "harnessed.providers.pricing",
        "harnessed.providers.registry",
        "harnessed.acp.probes",
        "harnessed.router.decisions",
        "harnessed.acp.outputs",
        "harnessed.acp.pending",
        "harnessed.tasks.store",
        "harnessed.tasks.schedule",
    }
    offenders = []
    for path in sorted(GATEWAY_ROOT.rglob("*.py")):
        for name, line in _imports_in(path):
            if not name.startswith("harnessed."):
                continue
            head = name.split(".")[1]
            if head in permitted or head in leaves or head == "gateway" or name in modules:
                continue
            offenders.append((path.name, name, line))
    assert offenders == []


# ---------------------------------------------------------------------------
# 3. ``serve`` imports the gateway lazily, and names the extra when it is absent
# ---------------------------------------------------------------------------


def test_serve_cmd_imports_the_gateway_and_uvicorn_zero_times_at_module_level() -> None:
    """Decision 21: the import is inside the function body, never in the header."""
    top = [name for name, _ in _imports_in(SERVE_CMD, module_level_only=True)]
    assert not any(_names_top(name, "uvicorn") for name in top), top
    assert not any(name.startswith("harnessed.gateway") for name in top), top
    everywhere = [name for name, _ in _imports_in(SERVE_CMD)]
    assert any(name.startswith("harnessed.gateway") for name in everywhere)
    assert any(_names_top(name, "uvicorn") for name in everywhere)


def test_gateway_extra_missing_is_a_named_harness_error_that_names_the_fix() -> None:
    exc = GatewayExtraMissing()
    assert isinstance(exc, GatewayError)
    assert isinstance(exc, HarnessError)
    assert "uv sync --extra gateway" in str(exc)
    assert issubclass(CursorError, GatewayError)
    assert issubclass(LimitError, GatewayError)
    assert issubclass(ParameterError, GatewayError)
    # The one 400 that is not the gateway's own: the store raises it, and the status map
    # names the class the store raises rather than re-wrapping it (decision 37).
    assert issubclass(SuiteMismatch, EvalError)
    assert not issubclass(SuiteMismatch, GatewayError)


@pytest.fixture
def extra_absent(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stage the extra being absent: ``import fastapi`` and ``import uvicorn`` raise.

    Every already-imported ``harnessed.gateway`` module is dropped as well, so the lazy import
    re-executes and meets the poisoned entries instead of a cached module.
    """
    for name in list(sys.modules):
        if name == "harnessed.gateway" or name.startswith("harnessed.gateway."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "fastapi", None)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    yield


@pytest.mark.usefixtures("tmp_harnessed_home", "extra_absent")
def test_serve_without_the_extra_exits_non_zero_naming_the_extra_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = shell(["serve"], monkeypatch, capsys)
    assert seen.code != 0
    assert "uv sync --extra gateway" in seen.err
    assert "Traceback" not in seen.err
    assert "ImportError" not in seen.err
    assert "ModuleNotFoundError" not in seen.err
    assert seen.out == ""
    capture("missing-extra.txt", f"$ harnessed serve\nexit {seen.code}\n{seen.err}")


# ---------------------------------------------------------------------------
# 4. ``serve`` with the extra present: four calls, uvicorn silenced, loopback by default
# ---------------------------------------------------------------------------


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


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_serve_binds_loopback_by_default_with_uvicorn_silenced(
    uvicorn_run: Bound, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = shell(["serve"], monkeypatch, capsys)
    assert seen.code == 0, seen.err
    assert len(uvicorn_run.calls) == 1
    call = uvicorn_run.calls[0]
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8756
    assert call["log_config"] is None
    assert call["access_log"] is False
    # The app handed to uvicorn is the one create_app builds: it answers the health route.
    with TestClient(call["app"]) as client:
        assert client.get("/v1/health").status_code == 200
    # The one startup line names the bind address and the absolute database path (decision 22).
    assert "127.0.0.1:8756" in seen.out
    assert str(db.database_path().resolve()) in seen.out
    assert seen.err == ""


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_serve_on_a_non_loopback_host_warns_about_the_missing_auth_surface_before_binding(
    uvicorn_run: Bound, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = shell(["serve", "--host", "0.0.0.0", "--port", "9001"], monkeypatch, capsys)
    assert seen.code == 0, seen.err
    assert uvicorn_run.calls[0]["host"] == "0.0.0.0"
    assert uvicorn_run.calls[0]["port"] == 9001
    assert "warning" in seen.err.lower()
    assert "auth" in seen.err.lower()
    assert not ipaddress.ip_address("0.0.0.0").is_loopback


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_serve_accepts_the_provider_and_token_budget_flags_for_config_parity(
    uvicorn_run: Bound, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = shell(["serve", "--provider", "fake", "--token-budget", "1234"], monkeypatch, capsys)
    assert seen.code == 0, seen.err
    assert len(uvicorn_run.calls) == 1


# ---------------------------------------------------------------------------
# 5. The health route
# ---------------------------------------------------------------------------


def test_health_answers_200_with_schema_version_database_path_and_route_count(
    client: TestClient, app_and_home: tuple[Any, Path]
) -> None:
    _, home = app_and_home
    response = client.get("/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == HEALTH_KEYS
    assert payload["schema_version"] == db.load_migrations()[-1].version
    assert Path(payload["database_path"]).is_absolute()
    assert Path(payload["database_path"]) == (home / db.DATABASE_FILENAME).resolve()
    assert payload["route_count"] >= 1


def test_the_route_count_is_the_number_of_get_routes_the_app_registers(
    client: TestClient, app_and_home: tuple[Any, Path]
) -> None:
    """The count is read off the app, and the app carries no docs or openapi routes."""
    app, _ = app_and_home
    paths = [route.path for route in app.routes]
    assert "/v1/health" in paths
    assert not any("docs" in path or "openapi" in path for path in paths), paths
    assert client.get("/v1/health").json()["route_count"] == len(paths)


def test_health_does_not_create_the_database_file(
    client: TestClient, app_and_home: tuple[Any, Path]
) -> None:
    """Health reports the path it was handed; it does not open the file (ticket 065 owns that)."""
    _, home = app_and_home
    client.get("/v1/health")
    assert not (home / db.DATABASE_FILENAME).exists()


# ---------------------------------------------------------------------------
# 6. Every byte is serialize.canonical() of the payload the handler returned
# ---------------------------------------------------------------------------

#: Unsorted keys, a non-ASCII string, a float, nesting: everything a re-serializer would touch.
PROBE_PAYLOAD: dict[str, object] = {
    "zeta": "café ñ 日本",
    "alpha": 0.1,
    "score": 0.123457,
    "nested": {"y": [1, 2.5, "x"], "x": None, "w": True},
    "empty": {},
}


def test_the_default_response_class_writes_canonical_bytes_and_nothing_else(
    app_and_home: tuple[Any, Path],
) -> None:
    app, _ = app_and_home

    @app.get("/probe")
    def probe() -> dict[str, object]:
        return PROBE_PAYLOAD

    with TestClient(app) as client:
        response = client.get("/probe")
    assert response.status_code == 200
    assert response.content == canonical(PROBE_PAYLOAD).encode("utf-8")
    assert response.headers["content-type"].startswith("application/json")


def test_health_bytes_are_the_canonical_spelling(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert response.content == canonical(response.json()).encode("utf-8")
    assert b": " not in response.content
    assert b"\n" not in response.content


# ---------------------------------------------------------------------------
# 7. The one error body, always with a non-2xx status
# ---------------------------------------------------------------------------


@pytest.fixture
def raising_client(app_and_home: tuple[Any, Path]) -> Iterator[TestClient]:
    """The app plus four routes that raise, one per row of the status map."""
    app, _ = app_and_home

    @app.get("/raise/surface")
    def surface() -> dict[str, object]:
        raise SurfaceNotFound("no session id run_2f9c")

    @app.get("/raise/cursor")
    def cursor() -> dict[str, object]:
        raise CursorError("Last-Event-ID 'abc' is not a non-negative integer")

    @app.get("/raise/limit")
    def limit() -> dict[str, object]:
        raise LimitError("limit 'abc' is not a positive integer.")

    @app.get("/raise/parameter")
    def parameter() -> dict[str, object]:
        raise ParameterError("the 'before' query parameter is required")

    @app.get("/raise/suite")
    def suite() -> dict[str, object]:
        raise SuiteMismatch(
            "eval run 'eval_a' is suite 'smoke' and eval run 'eval_b' is " "suite 'regression'"
        )

    @app.get("/raise/gateway")
    def gateway() -> dict[str, object]:
        raise GatewayError("the gateway could not answer")

    @app.get("/raise/other")
    def other() -> dict[str, object]:
        raise ConfigError("HARNESSED_HOME names no directory")

    @app.get("/raise/bug")
    def bug() -> dict[str, object]:
        raise RuntimeError("a bug, not a HarnessError")

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize(
    ("path", "status", "error", "message"),
    [
        ("/raise/surface", 404, "SurfaceNotFound", "no session id run_2f9c"),
        ("/raise/cursor", 400, "CursorError", "Last-Event-ID 'abc' is not a non-negative integer"),
        ("/raise/limit", 400, "LimitError", "limit 'abc' is not a positive integer."),
        (
            "/raise/parameter",
            400,
            "ParameterError",
            "the 'before' query parameter is required",
        ),
        (
            "/raise/suite",
            400,
            "SuiteMismatch",
            "eval run 'eval_a' is suite 'smoke' and eval run 'eval_b' is suite 'regression'",
        ),
        # The base is not the client's mistake: only its named children are (ticket 080).
        ("/raise/gateway", 500, "GatewayError", "the gateway could not answer"),
        ("/raise/other", 500, "ConfigError", "HARNESSED_HOME names no directory"),
        ("/raise/bug", 500, "InternalError", "a bug, not a HarnessError"),
    ],
)
def test_each_error_leaves_in_the_one_body_with_its_mapped_status(
    raising_client: TestClient, path: str, status: int, error: str, message: str
) -> None:
    response = raising_client.get(path)
    assert response.status_code == status
    assert response.json() == {"error": error, "message": message}
    assert response.content == canonical({"error": error, "message": message}).encode("utf-8")


def test_an_unknown_path_is_a_404_in_the_one_body_not_fastapi_detail(
    client: TestClient,
) -> None:
    response = client.get("/v1/nope")
    assert response.status_code == 404
    payload = response.json()
    assert set(payload) == {"error", "message"}
    assert payload["error"] == "SurfaceNotFound"
    assert "detail" not in payload
    assert response.content == canonical(payload).encode("utf-8")


def test_no_2xx_response_carries_an_error_field(raising_client: TestClient) -> None:
    """Decision 24's anti-pattern: a 200 with an error string is v1's defect wearing a header."""
    for path in (
        "/raise/surface",
        "/raise/cursor",
        "/raise/parameter",
        "/raise/suite",
        "/raise/other",
        "/raise/bug",
        "/v1/health",
    ):
        response = raising_client.get(path)
        if 200 <= response.status_code < 300:
            assert "error" not in response.json(), path
        else:
            assert response.status_code >= 400, path


def test_the_error_body_is_produced_by_gateway_errors_and_nowhere_else() -> None:
    """Static: the string ``"error"`` as a JSON key is spelled in one module under gateway/."""
    spellers = [
        path.name
        for path in sorted(GATEWAY_ROOT.rglob("*.py"))
        if '"error"' in path.read_text(encoding="utf-8")
    ]
    assert spellers == ["errors.py"]


def test_the_app_holds_no_state_between_requests(app_and_home: tuple[Any, Path]) -> None:
    """MUST rule 17: two apps over two paths answer independently; nothing is module-level."""
    app, home = app_and_home
    other = create_app(home / "elsewhere" / db.DATABASE_FILENAME)
    with TestClient(app) as first, TestClient(other) as second:
        a = first.get("/v1/health").json()["database_path"]
        b = second.get("/v1/health").json()["database_path"]
    assert a != b
    assert a.endswith(db.DATABASE_FILENAME) and b.endswith(db.DATABASE_FILENAME)
