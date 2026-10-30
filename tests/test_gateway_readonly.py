"""FR-33: the gateway cannot mutate anything.

Read-only is a property SQLite enforces, not a promise a reviewer checks. Every assertion here
is over the live route table, the live row counts or the live AST, so the routes tickets 068 to
070 add are covered without anyone editing this file: a route with a writing method, a handler
that spells SQL, or a module that opens the file with ``db.connect()`` turns one of these red.

The trap the architecture names is that ``db.connect()`` migrates on open. A gateway that used
it would write on the first request while every test stayed green, so ``connect_readonly`` is
proved to open ``mode=ro``, to run no migration and no writing pragma, and ``create_app`` is
proved to refuse a database behind the shipped migration count without calling ``db.migrate``.
"""

from __future__ import annotations

import ast
import importlib
import re
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from harnessed.errors import HarnessError, SchemaOutOfDateError, StorageError
from harnessed.gateway import create_app, deps
from harnessed.memory import db

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_ROOT = REPO_ROOT / "src" / "harnessed" / "gateway"
MIGRATIONS_DIR = REPO_ROOT / "src" / "harnessed" / "memory" / "migrations"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "065"

#: The unpatched opener, for the test's own reads: fixtures below monkeypatch ``sqlite3.connect``
#: to watch what ``db`` does, and ``db.sqlite3`` is this same module object.
_REAL_CONNECT = sqlite3.connect

#: Exactly what ``connect_readonly`` passes beside the URI. ``check_same_thread=False`` is there
#: because FastAPI runs a sync dependency and its endpoint on separate threadpool workers.
READONLY_KWARGS = {"uri": True, "isolation_level": None, "check_same_thread": False}

#: A well-formed INSERT, so the statement reaches SQLite's write check rather than failing at
#: prepare time on an unknown column (which would prove nothing about read-only).
PROBE_INSERT = (
    "INSERT INTO mem_episodic (episode_id, run_id, kind, text, created_at) "
    "VALUES ('x', 'run_x', 'turn', 'gateway wrote', '2026-01-01T00:00:00Z')"
)

#: The writing methods. Zero routes may serve any of them (FR-33).
WRITING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Every table in ``architecture.md``'s data table, spelled from the document rather than read
#: off ``sqlite_master`` so the schema and the document are checked against each other.
LEDGER_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "events",
    "mem_semantic",
    "mem_semantic_fts",
    "mem_episodic",
    "procedures",
    "active_procedures",
    "turns",
    "tool_results",
    "eval_runs",
    "eval_results",
    "improvement_runs",
    "improvement_steps",
    "candidates",
    "promotions",
    "delegations",
)

#: The SQL verbs the scan refuses inside any string constant under ``gateway/`` (MUST rule 14).
SQL_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")

#: The path a test-only probe route is registered on, so a request really opens a connection.
PROBE_PATH = "/v1/probe"


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# fixtures: a migrated database, an app over it, a probe route that opens a connection
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db(tmp_harnessed_home: Path) -> Path:
    """The one database, created and migrated the way the CLI does it, then closed."""
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    conn = db.connect(path)
    conn.close()
    return path


def add_probe(app: Any) -> None:
    """A route that opens the per-request connection and reads the version through it."""

    @app.get(PROBE_PATH)
    def probe(conn: Annotated[sqlite3.Connection, Depends(deps.connection)]) -> dict[str, int]:
        return {"user_version": db.schema_version(conn)}


@pytest.fixture
def app(migrated_db: Path) -> Any:
    application = create_app(migrated_db)
    add_probe(application)
    return application


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def registered_paths(app: Any) -> list[str]:
    paths = [route.path for route in app.routes]
    assert paths, "the route table must not be empty, or every assertion over it is vacuous"
    return paths


#: What the scrape substitutes for every path parameter. An id no ledger holds, so a
#: parametrized route answers with decision 24's 404 - a read like any other, and one the
#: row-count and ``data_version`` assertions below still cover.
SCRAPE_ID = "scrape_00000000000000000000000000"


#: A route whose question is incomplete without a query string, and the query that completes
#: it with ids no ledger holds. Without it the scrape would meet a 400 before any handler
#: ran, which proves nothing about what a handler does to the file (ticket 098).
REQUIRED_QUERY: dict[str, str] = {
    "/v1/eval/compare": f"?before={SCRAPE_ID}&after={SCRAPE_ID}",
    "/v1/permissions": "?state=pending",
}


def scrape_path(template: str) -> str:
    """A registered path with each ``{parameter}`` filled and any required query attached,
    so the scrape hits a real handler."""
    filled = re.sub(r"{[^}]+}", SCRAPE_ID, template)
    return filled + REQUIRED_QUERY.get(filled, "")


#: The statuses a scrape may see: 200 on a collection, 404 for an id the ledgers do not hold.
#: Anything else - a 500, a 405 - is a defect the scrape exists to surface.
SCRAPE_STATUSES = frozenset({200, 404})


def row_counts(path: Path) -> dict[str, int]:
    """Row count of every ledger table, read through a plain read-only connection."""
    conn = _REAL_CONNECT(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        present = {
            name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = [table for table in LEDGER_TABLES if table not in present]
        assert missing == [], f"architecture.md names tables the schema lacks: {missing}"
        return {
            table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in LEDGER_TABLES
        }
    finally:
        conn.close()


def user_version(path: Path) -> int:
    conn = _REAL_CONNECT(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


class Monitor:
    """A watching connection: ``PRAGMA data_version`` changes iff another connection commits."""

    def __init__(self, path: Path) -> None:
        self.conn = _REAL_CONNECT(f"file:{path.resolve()}?mode=ro", uri=True)

    def data_version(self) -> int:
        return int(self.conn.execute("PRAGMA data_version").fetchone()[0])

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# 1. Zero writing routes, asserted over the live route table
# ---------------------------------------------------------------------------


def test_zero_routes_serve_a_writing_method(app: Any) -> None:
    offenders = [
        (route.path, sorted(methods))
        for route in app.routes
        if (methods := set(getattr(route, "methods", ()) or ()) & WRITING_METHODS)
    ]
    assert registered_paths(app)
    assert offenders == []


def test_every_registered_route_is_get_only(app: Any) -> None:
    """Stronger than the absence of writing methods: nothing but GET (and HEAD) answers."""
    for route in app.routes:
        methods = set(getattr(route, "methods", ()) or ())
        assert methods <= {"GET", "HEAD"}, (route.path, methods)


# ---------------------------------------------------------------------------
# 2. A POST to every path is a 405 that writes nothing
# ---------------------------------------------------------------------------


def test_a_post_to_every_registered_path_is_405_and_writes_nothing(
    app: Any, client: TestClient, migrated_db: Path
) -> None:
    before = row_counts(migrated_db)
    version = user_version(migrated_db)
    for path in registered_paths(app):
        response = client.post(path, json={"x": 1})
        assert response.status_code == 405, (path, response.status_code)
        assert response.json() == {
            "error": "GatewayError",
            "message": f"POST is not served on {path}",
        }
    assert row_counts(migrated_db) == before
    assert user_version(migrated_db) == version


@pytest.mark.parametrize("method", sorted(WRITING_METHODS))
def test_each_writing_method_is_refused_on_every_path(
    app: Any, client: TestClient, migrated_db: Path, method: str
) -> None:
    before = row_counts(migrated_db)
    for path in registered_paths(app):
        assert client.request(method, path).status_code == 405, (method, path)
    assert row_counts(migrated_db) == before


# ---------------------------------------------------------------------------
# 3. A full serve-and-scrape cycle leaves the file untouched
# ---------------------------------------------------------------------------


def test_scraping_every_get_leaves_user_version_and_every_row_count_unchanged(
    app: Any, client: TestClient, migrated_db: Path
) -> None:
    monitor = Monitor(migrated_db)
    try:
        before_counts = row_counts(migrated_db)
        before_version = user_version(migrated_db)
        before_data = monitor.data_version()
        served: list[tuple[str, int]] = []
        for _ in range(3):
            for template in registered_paths(app):
                path = scrape_path(template)
                response = client.get(path)
                served.append((path, response.status_code))
                assert response.status_code in SCRAPE_STATUSES, (path, response.text)
        after_counts = row_counts(migrated_db)
        after_version = user_version(migrated_db)
        after_data = monitor.data_version()
    finally:
        monitor.close()
    assert after_version == before_version
    assert after_counts == before_counts
    assert after_data == before_data, "another connection committed during the scrape"
    lines = [f"user_version before={before_version} after={after_version}"]
    lines += [f"{t:<20} before={before_counts[t]} after={after_counts[t]}" for t in LEDGER_TABLES]
    lines += ["", "served:"] + [f"  GET {p} -> {s}" for p, s in served]
    capture("row-counts-before-after.txt", "\n".join(lines) + "\n")


def test_the_probe_route_really_opens_a_connection_per_request(
    client: TestClient, migrated_db: Path
) -> None:
    """Guards the fixture: the scrape above is over a route that reads through ``deps``."""
    response = client.get(PROBE_PATH)
    assert response.status_code == 200
    assert response.json() == {"user_version": user_version(migrated_db)}


# ---------------------------------------------------------------------------
# 4. The schema guard: refuse, name both versions and the fix, migrate nothing
# ---------------------------------------------------------------------------


@pytest.fixture
def behind_db(tmp_harnessed_home: Path, tmp_path: Path) -> Path:
    """A database migrated to one version short of what the package ships."""
    partial = tmp_path / "partial_migrations"
    partial.mkdir()
    shipped = db.load_migrations()
    for migration in shipped[:-1]:
        shutil.copy(MIGRATIONS_DIR / migration.filename, partial / migration.filename)
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        db.migrate(conn, partial)
        assert db.schema_version(conn) == shipped[-1].version - 1
    finally:
        conn.close()
    return path


class Counter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("the gateway reached a writing entry point")


def test_create_app_refuses_a_database_behind_the_shipped_version_without_migrating(
    behind_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate = Counter()
    connect = Counter()
    monkeypatch.setattr(db, "migrate", migrate)
    monkeypatch.setattr(db, "connect", connect)
    expected = len(db.load_migrations())
    with pytest.raises(SchemaOutOfDateError) as caught:
        create_app(behind_db)
    exc = caught.value
    assert isinstance(exc, StorageError)
    assert isinstance(exc, HarnessError)
    assert exc.expected == expected
    assert exc.actual == expected - 1
    assert str(exc.expected) in str(exc)
    assert str(exc.actual) in str(exc)
    assert exc.remedy in str(exc)
    assert migrate.calls == 0
    assert connect.calls == 0
    assert user_version(behind_db) == expected - 1, "the guard changed the file"
    capture(
        "schema-guard.txt",
        f"$ harnessed serve\nerror: {exc}\n\n"
        f"db.migrate calls: {migrate.calls}\ndb.connect calls: {connect.calls}\n"
        f"PRAGMA user_version afterwards: {user_version(behind_db)}\n",
    )


def test_the_remedy_the_guard_names_actually_fixes_it(
    behind_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """MUST rule 12: the message says a command fixes it, so run that command and re-check."""
    from harnessed.cli.app import main

    with pytest.raises(SchemaOutOfDateError) as caught:
        create_app(behind_db)
    remedy = caught.value.remedy.split()
    assert remedy[0] == "harnessed"
    monkeypatch.setenv("HARNESSED_PROVIDER", "fake")
    monkeypatch.setattr(sys, "argv", ["harnessed", *remedy[1:], "--offline"])
    try:
        main()
    except SystemExit as exit_info:
        assert exit_info.code in (None, 0), capsys.readouterr().err
    assert user_version(behind_db) == len(db.load_migrations())
    app = create_app(behind_db)
    add_probe(app)
    with TestClient(app) as client:
        assert client.get(PROBE_PATH).json() == {"user_version": len(db.load_migrations())}


def test_create_app_over_a_current_database_serves_it(migrated_db: Path) -> None:
    app = create_app(migrated_db)
    add_probe(app)
    with TestClient(app) as client:
        assert client.get(PROBE_PATH).status_code == 200


def test_create_app_over_a_path_with_no_file_yet_creates_nothing(tmp_harnessed_home: Path) -> None:
    """There is no version to compare on an absent file, and creating one would be a write."""
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    app = create_app(path)
    add_probe(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/v1/health").status_code == 200
        response = client.get(PROBE_PATH)
    assert response.status_code == 500
    assert response.json()["error"] == "StorageError"
    assert str(path) in response.json()["message"]
    assert not path.exists()


# ---------------------------------------------------------------------------
# 5. The source scan over gateway/: no SQL, no pydantic, no logging, no db.connect
# ---------------------------------------------------------------------------


def gateway_sources() -> list[Path]:
    sources = sorted(GATEWAY_ROOT.rglob("*.py"))
    assert len(sources) >= 4
    return sources


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def string_constants(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def imported_roots(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module.split(".")[0]


def test_gateway_spells_zero_sql_string_literals() -> None:
    offenders = [
        f"{path.name}:{lineno} {text!r}"
        for path in gateway_sources()
        for lineno, text in string_constants(parse(path))
        if any(verb in text for verb in SQL_VERBS)
    ]
    assert offenders == []


def test_gateway_imports_neither_pydantic_nor_logging() -> None:
    offenders = [
        f"{path.name}:{lineno} imports {name}"
        for path in gateway_sources()
        for lineno, name in imported_roots(parse(path))
        if name in {"pydantic", "logging"}
    ]
    assert offenders == []


def _calls_db_connect(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "connect":
        return isinstance(func.value, ast.Name) and func.value.id == "db"
    return isinstance(func, ast.Name) and func.id == "connect"


def test_gateway_calls_db_connect_zero_times(app: Any) -> None:
    """MUST rule 13, both spellings: ``db.connect(...)`` and a bare imported ``connect``."""
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in gateway_sources()
        for node in ast.walk(parse(path))
        if _calls_db_connect(node)
    ]
    imports = [
        f"{path.name}:{node.lineno}"
        for path in gateway_sources()
        for node in ast.walk(parse(path))
        if isinstance(node, ast.ImportFrom)
        and node.module == "harnessed.memory.db"
        and any(alias.name == "connect" for alias in node.names)
    ]
    assert offenders == []
    assert imports == []
    # And the scan is not vacuous: the one sanctioned opener is spelled in the package.
    text = "\n".join(path.read_text(encoding="utf-8") for path in gateway_sources())
    assert "connect_readonly" in text


# ---------------------------------------------------------------------------
# 6. connect_readonly: the exact open, the two pragmas, nothing that writes
# ---------------------------------------------------------------------------


class Opened:
    """Every ``sqlite3.connect`` call ``db`` makes, and every statement each connection ran."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.statements: list[str] = []


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> Opened:
    seen = Opened()
    real = sqlite3.connect

    def recording(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        seen.calls.append((args, kwargs))
        conn = real(*args, **kwargs)
        conn.set_trace_callback(seen.statements.append)
        return conn

    monkeypatch.setattr(db.sqlite3, "connect", recording)
    return seen


def test_connect_readonly_opens_mode_ro_by_uri_with_autocommit(
    migrated_db: Path, opened: Opened
) -> None:
    conn = db.connect_readonly(migrated_db)
    try:
        assert opened.calls == [((f"file:{migrated_db.resolve()}?mode=ro",), READONLY_KWARGS)]
        assert conn.isolation_level is None
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_connect_readonly_runs_only_the_two_pragmas_and_reads(
    migrated_db: Path, opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate = Counter()
    monkeypatch.setattr(db, "migrate", migrate)
    before = user_version(migrated_db)
    conn = db.connect_readonly(migrated_db)
    conn.close()
    assert migrate.calls == 0
    assert "PRAGMA busy_timeout=5000" in opened.statements
    assert "PRAGMA query_only = ON" in opened.statements
    for statement in opened.statements:
        head = statement.strip().split()[0].upper()
        assert head == "PRAGMA", statement
        lowered = statement.lower()
        assert "journal_mode" not in lowered, statement
        assert "synchronous" not in lowered, statement
        assert "foreign_keys" not in lowered, statement
        assert "user_version" not in lowered or "=" not in lowered, statement
    assert user_version(migrated_db) == before


def test_sqlite_itself_refuses_a_write_through_a_readonly_connection(migrated_db: Path) -> None:
    conn = db.connect_readonly(migrated_db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(PROBE_INSERT)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("PRAGMA user_version = 99")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE gateway_wrote (x)")
    finally:
        conn.close()
    assert user_version(migrated_db) == len(db.load_migrations())


def test_connect_readonly_never_creates_a_missing_file(tmp_harnessed_home: Path) -> None:
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    with pytest.raises(StorageError) as caught:
        db.connect_readonly(path)
    assert str(path) in str(caught.value)
    assert not path.exists()


# ---------------------------------------------------------------------------
# 7. The WAL-recovery caveat: retry once with mode=rw plus query_only, and only for that error
# ---------------------------------------------------------------------------


def recovery_error() -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError("attempt to write a readonly database")
    exc.sqlite_errorcode = sqlite3.SQLITE_READONLY_RECOVERY  # type: ignore[attr-defined]
    exc.sqlite_errorname = "SQLITE_READONLY_RECOVERY"  # type: ignore[attr-defined]
    return exc


@pytest.fixture
def wal_needs_recovery(monkeypatch: pytest.MonkeyPatch) -> Opened:
    """Stage the caveat: every ``mode=ro`` open fails with exactly the recovery error."""
    seen = Opened()
    real = sqlite3.connect

    def failing_ro(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        seen.calls.append((args, kwargs))
        if "mode=ro" in str(args[0]):
            raise recovery_error()
        return real(*args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", failing_ro)
    return seen


def test_the_recovery_error_is_retried_once_with_mode_rw_and_query_only(
    migrated_db: Path, wal_needs_recovery: Opened
) -> None:
    conn = db.connect_readonly(migrated_db)
    try:
        uris = [str(args[0]) for args, _ in wal_needs_recovery.calls]
        assert uris == [
            f"file:{migrated_db.resolve()}?mode=ro",
            f"file:{migrated_db.resolve()}?mode=rw",
        ]
        assert all(kw == READONLY_KWARGS for _, kw in wal_needs_recovery.calls)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(PROBE_INSERT)
    finally:
        conn.close()


def test_on_the_recovery_path_the_route_post_and_row_count_assertions_still_hold(
    migrated_db: Path, wal_needs_recovery: Opened
) -> None:
    app = create_app(migrated_db)
    add_probe(app)
    before = row_counts(migrated_db)
    version = user_version(migrated_db)
    with TestClient(app, raise_server_exceptions=False) as client:
        for route in app.routes:
            assert not set(getattr(route, "methods", ()) or ()) & WRITING_METHODS
        for template in registered_paths(app):
            path = scrape_path(template)
            assert client.post(path).status_code == 405
            assert client.get(path).status_code in SCRAPE_STATUSES, path
    assert any("mode=rw" in str(args[0]) for args, _ in wal_needs_recovery.calls)
    assert row_counts(migrated_db) == before
    assert user_version(migrated_db) == version


def test_any_other_open_error_is_not_retried(
    migrated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly that error: a plain failure is a ``StorageError`` after one attempt, never ``rw``."""
    calls: list[str] = []

    def failing(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        calls.append(str(args[0]))
        exc = sqlite3.OperationalError("disk I/O error")
        exc.sqlite_errorcode = 10  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(db.sqlite3, "connect", failing)
    with pytest.raises(StorageError, match="disk I/O error"):
        db.connect_readonly(migrated_db)
    assert calls == [f"file:{migrated_db.resolve()}?mode=ro"]


def test_a_failure_on_the_retry_is_a_storage_error(
    migrated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        raise recovery_error()

    monkeypatch.setattr(db.sqlite3, "connect", failing)
    with pytest.raises(StorageError):
        db.connect_readonly(migrated_db)


# ---------------------------------------------------------------------------
# 8. No module-level connection, cache, subscriber registry or cursor (MUST rule 17)
# ---------------------------------------------------------------------------


STATEFUL_TYPES = (sqlite3.Connection, sqlite3.Cursor, dict, list, set)


def test_gateway_modules_hold_no_state_at_module_level() -> None:
    offenders: list[str] = []
    for path in gateway_sources():
        module_name = "harnessed.gateway" + (
            ""
            if path.name == "__init__.py"
            else "." + path.relative_to(GATEWAY_ROOT).with_suffix("").as_posix().replace("/", ".")
        )
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            if isinstance(value, STATEFUL_TYPES) or hasattr(value, "cache_info"):
                offenders.append(f"{path.name}:{name} is a {type(value).__name__}")
        for node in parse(path).body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
                value_node: ast.expr | None = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value_node = node.value
            else:
                continue
            if isinstance(value_node, ast.Call):
                names = [ast.unparse(t) for t in targets]
                offenders.append(f"{path.name}:{node.lineno} {names} is built by a call")
    assert offenders == []
