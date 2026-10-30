"""FR-34: one database, resolved once, safe to read from a threadpool.

``HARNESSED_HOME`` defaults to the relative ``.harnessed``, so the serving process's working
directory would otherwise decide which file it opens. ``harnessed serve`` resolves the home
through ``config.load()`` like every other command, reports the absolute database path it will
open, and refuses a home that does not exist rather than creating an empty database there.

Uvicorn runs a sync handler in a threadpool and ``db.connect()`` is one connection per process
without ``check_same_thread=False``. The gateway opens one read-only connection per request on
the worker that serves it, so there is no shared connection for a second thread to trip over,
and WAL lets the CLI commit while the gateway is mid-read without either side seeing
``database is locked``.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from harnessed.cli import serve_cmd
from harnessed.cli.app import main
from harnessed.errors import ConfigError, HarnessError, HomeNotFoundError
from harnessed.gateway import create_app, deps
from harnessed.memory import db, episodic

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "065"

PROBE_PATH = "/v1/probe"
EPISODES_PATH = "/v1/probe/episodes"
SESSION_ID = "run_gateway_db_0001"

#: The concurrency the requirement names, and the number of requests spread across it.
WORKERS = 12
REQUESTS = 60


def capture(name: str, text: str) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


@dataclass(frozen=True)
class Shell:
    code: int
    out: str
    err: str


def shell(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Shell:
    """Run one command through ``main`` so the error contract is the one an operator sees."""
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
    calls: list[dict[str, Any]]


@pytest.fixture
def uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> Bound:
    import uvicorn

    bound = Bound(calls=[])

    def record(app: Any, **kwargs: Any) -> None:
        bound.calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", record)
    return bound


@pytest.fixture
def migrated_db(tmp_harnessed_home: Path) -> Path:
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    conn = db.connect(path)
    conn.close()
    return path


def add_probes(app: Any, threads: list[int]) -> None:
    """Two routes that read through the per-request connection, recording the serving thread."""

    @app.get(PROBE_PATH)
    def probe(conn: Annotated[sqlite3.Connection, Depends(deps.connection)]) -> dict[str, int]:
        threads.append(threading.get_ident())
        return {"user_version": db.schema_version(conn)}

    @app.get(EPISODES_PATH)
    def episodes(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, int]:
        threads.append(threading.get_ident())
        return {"episodes": len(episodic.for_run(conn, SESSION_ID))}


@pytest.fixture
def served(migrated_db: Path) -> Iterator[tuple[Any, TestClient, list[int]]]:
    threads: list[int] = []
    app = create_app(migrated_db)
    add_probes(app, threads)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client, threads


# ---------------------------------------------------------------------------
# 1. The home resolves through config.load(), from any working directory
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("migrated_db")
def test_serve_from_another_directory_opens_the_same_file_and_reports_it_absolute(
    tmp_harnessed_home: Path,
    tmp_path: Path,
    uvicorn_run: Bound,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    seen = shell(["serve"], monkeypatch, capsys)
    assert seen.code == 0, seen.err
    expected = (tmp_harnessed_home / db.DATABASE_FILENAME).resolve()
    assert expected.is_absolute()
    with TestClient(uvicorn_run.calls[0]["app"]) as client:
        assert client.get("/v1/health").json()["database_path"] == str(expected)
    assert str(expected) in seen.out
    assert not (elsewhere / ".harnessed").exists()
    capture("serve-from-elsewhere.txt", f"cwd {elsewhere}\n$ harnessed serve\n{seen.out}")


def test_a_relative_home_resolves_against_the_working_directory(
    tmp_path: Path,
    uvicorn_run: Bound,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / ".harnessed"
    home.mkdir()
    conn = db.connect(home / db.DATABASE_FILENAME)
    conn.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_HOME", ".harnessed")
    seen = shell(["serve"], monkeypatch, capsys)
    assert seen.code == 0, seen.err
    assert str((home / db.DATABASE_FILENAME).resolve()) in seen.out


# ---------------------------------------------------------------------------
# 2. A home that does not exist is a named error, and nothing is created
# ---------------------------------------------------------------------------


def test_a_relative_home_naming_no_directory_is_home_not_found_and_creates_nothing(
    tmp_path: Path,
    uvicorn_run: Bound,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_HOME", "nowhere")
    with pytest.raises(HomeNotFoundError) as caught:
        serve_cmd.serve()
    exc = caught.value
    assert isinstance(exc, ConfigError)
    assert isinstance(exc, HarnessError)
    assert "nowhere" in str(exc)
    assert str((tmp_path / "nowhere").resolve()) in str(exc)
    assert uvicorn_run.calls == []
    assert not (tmp_path / "nowhere").exists()
    assert not (tmp_path / "nowhere" / db.DATABASE_FILENAME).exists()
    seen = shell(["serve"], monkeypatch, capsys)
    assert seen.code == 1
    assert "Traceback" not in seen.err
    assert "nowhere" in seen.err
    assert seen.out == ""
    assert not (tmp_path / "nowhere").exists()
    capture(
        "home-not-found.txt",
        f"cwd {tmp_path}\nHARNESSED_HOME=nowhere\n$ harnessed serve\n{seen.err}",
    )


def test_an_absolute_home_naming_no_directory_is_refused_the_same_way(
    tmp_path: Path, uvicorn_run: Bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "absent" / "home"
    monkeypatch.setenv("HARNESSED_HOME", str(missing))
    with pytest.raises(HomeNotFoundError):
        serve_cmd.serve()
    assert not missing.exists()
    assert uvicorn_run.calls == []


# ---------------------------------------------------------------------------
# 3. Concurrent requests across the read surfaces: all 200, no thread error
# ---------------------------------------------------------------------------


#: What a parametrized route's ``{parameter}`` is filled with: an id no ledger holds, so the
#: request reaches a real handler and answers decision 24's 404 in the one body - a read like
#: any other for the purpose of "no thread error, correct body".
UNHELD_ID = "unheld_0000000000000000000000000"


#: A route whose question is incomplete without a query string, and the query that completes
#: it with ids no ledger holds - so the sweep meets a real handler and gets the same 404 the
#: path-parameter rows give (ticket 098).
REQUIRED_QUERY: dict[str, str] = {
    "/v1/eval/compare": f"?before={UNHELD_ID}&after={UNHELD_ID}",
    "/v1/permissions": "?state=pending",
}


def concrete(template: str) -> str:
    filled = re.sub(r"{[^}]+}", UNHELD_ID, template)
    return filled + REQUIRED_QUERY.get(filled, "")


def test_twelve_concurrent_requests_all_answer_200_with_correct_bodies(
    served: tuple[Any, TestClient, list[int]], migrated_db: Path
) -> None:
    app, client, threads = served
    paths = [concrete(route.path) for route in app.routes]
    assert PROBE_PATH in paths and EPISODES_PATH in paths
    expected_bodies = {
        PROBE_PATH: {"user_version": len(db.load_migrations())},
        EPISODES_PATH: {"episodes": 0},
    }
    failures: list[str] = []

    def fetch(index: int) -> tuple[str, int, Any]:
        path = paths[index % len(paths)]
        try:
            response = client.get(path)
        except Exception as exc:
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
            raise
        return path, response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(fetch, range(REQUESTS)))

    assert failures == []
    assert len(results) == REQUESTS
    for path, status, body in results:
        if UNHELD_ID in path:
            assert status == 404, (path, status, body)
            assert body == {"error": "SurfaceNotFound", "message": body["message"]}
            assert UNHELD_ID in body["message"]
        else:
            assert status == 200, (path, status, body)
        assert "ProgrammingError" not in str(body)
        if path in expected_bodies:
            assert body == expected_bodies[path], (path, body)
        elif path == "/v1/health":
            assert body["database_path"] == str(migrated_db.resolve())
    served_by = sorted(set(threads))
    capture(
        "concurrency.txt",
        f"{REQUESTS} requests over {WORKERS} workers across {sorted(set(paths))}\n"
        f"statuses: {sorted({status for _, status, _ in results})}\n"
        f"threads that opened a connection: {len(served_by)}\n",
    )


# ---------------------------------------------------------------------------
# 4. A CLI write lands while the gateway serves reads, and nobody is locked
# ---------------------------------------------------------------------------


def test_a_cli_write_completes_while_the_gateway_is_reading(
    served: tuple[Any, TestClient, list[int]], migrated_db: Path
) -> None:
    _, client, _ = served
    stop = threading.Event()
    read_errors: list[str] = []
    read_count = 0

    def keep_reading() -> None:
        nonlocal read_count
        while not stop.is_set():
            try:
                response = client.get(EPISODES_PATH)
            except Exception as exc:
                read_errors.append(f"{type(exc).__name__}: {exc}")
                return
            if response.status_code != 200:
                read_errors.append(response.text)
                return
            read_count += 1

    readers = [threading.Thread(target=keep_reading) for _ in range(4)]
    for reader in readers:
        reader.start()
    try:
        writer = db.connect(migrated_db)
        try:
            for index in range(5):
                episodic.put(writer, SESSION_ID, "turn", f"the CLI wrote line {index}")
        finally:
            writer.close()
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=10)
    assert read_errors == []
    assert not any("database is locked" in error for error in read_errors)
    assert read_count > 0
    assert client.get(EPISODES_PATH).json() == {"episodes": 5}
