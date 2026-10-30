"""FR-127 (ticket 181, decisions 65, G-28, G-29): output kinds are served, and output files are
copied into the ledger.

Three layers. ``acp/outputs.py::kind_for`` and ``record_hop`` are exercised directly (AC2, AC3)
where a manufactured working directory gives precise control over confinement and the cap; a real
delegation through ``cli/routed.py::run_agent`` against ``tests/support/stub_cli_agent.py`` proves
the wiring end to end (AC1), the same way ``test_cli_runner.py`` insists on a real child process
rather than a mock; and ``GET /v1/outputs/{output_id}`` is exercised over a real gateway app
(AC4). AC5 (the CLI twin and the byte parity) lives in ``test_gateway_parity.py``, next to
``PARITY_SET`` and its own ``OUTSIDE_THE_SET`` entry; AC6 (migration 016) lives in
``test_migrations.py``, next to every other migration's own section.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from harnessed.acp import outputs as acp_outputs
from harnessed.acp.config import AgentSpec
from harnessed.cli import routed
from harnessed.events.bus import EventBus
from harnessed.gateway import create_app
from harnessed.memory import db

REPO_ROOT = Path(__file__).resolve().parents[1]
STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_cli_agent.py"
EVIDENCE = REPO_ROOT / "evidence" / "181"

# Ticket 117 (test_cli_runner.py): a stub started under `--cov` cold-starts too slowly.
pytestmark = pytest.mark.usefixtures("no_child_coverage")


def capture(name: str, text: str, *, append: bool = False) -> Path:
    """Write one evidence artefact under ``evidence/181/``. Regenerated on every verify run;
    never committed (``evidence/`` is gitignored)."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    mode = "a" if append and path.exists() else "w"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(text)
    return path


@pytest.fixture(autouse=True, scope="module")
def _fresh_kinds_evidence() -> None:
    """Start ``kinds.txt`` empty once per module run; the parametrized kind test appends a row."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "kinds.txt").write_text(
        "ticket 181 - one row per served output_kind (AC1)\n\n", encoding="utf-8"
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "harness.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    made = tmp_path / "home"
    made.mkdir()
    return made


def cli_spec(tmp_path: Path, **overrides: object) -> AgentSpec:
    """A ``kind = "cli"`` entry spawning the real stub, task on stdin, write-jail disabled - the
    tests here delegate precisely so that the stub can write its output files.

    The working directory is a subdirectory of ``tmp_path``, never ``tmp_path`` itself: the
    ``store`` fixture's own database file (and its ``-wal``/``-shm`` siblings) lives directly
    under ``tmp_path``, and an ``outputs`` glob of ``"*"`` would otherwise pick them up as
    changed files alongside the ones the stub actually wrote.
    """
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "name": "probe",
        "command": sys.executable,
        "args": (str(STUB_PATH),),
        "cwd": str(cwd),
        "timeout_s": 10.0,
        "kind": "cli",
        "input": "stdin",
        "allow_write": True,
    }
    fields.update(overrides)
    return AgentSpec(**fields)  # type: ignore[arg-type]


def file_row(conn: sqlite3.Connection, run_id: str, *, position: int = 1) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT name, output_kind, media_type, size_bytes, too_large, body FROM outputs "
        "WHERE run_id = ? AND position = ?",
        (run_id, position),
    ).fetchone()
    assert row is not None, f"no row at position {position} for {run_id!r}"
    return tuple(row)


def big_file(directory: Path, name: str = "big.bin") -> Path:
    """A file one byte past :data:`acp_outputs.OUTPUT_CAP_BYTES`."""
    path = directory / name
    path.write_bytes(b"x" * (acp_outputs.OUTPUT_CAP_BYTES + 1))
    return path


# ---- AC1: a stub cli entry's output files are served by their kind, and a Groq reply is text ---


def test_a_groq_replys_kind_is_text_by_contract() -> None:
    """The kind is stated as a constant here, never derived: a Groq turn never reaches this
    module, so there is no ``produces`` and no media type for :func:`kind_for` to see."""
    assert acp_outputs.KIND_GROQ_REPLY == acp_outputs.KIND_TEXT


@pytest.mark.parametrize(
    "filename, content, produces, expected_kind",
    [
        ("out.txt", "plain text", None, acp_outputs.KIND_TEXT),
        ("out.html", "<p>hi</p>", None, acp_outputs.KIND_UI),
        ("out.png", "not really a png", None, acp_outputs.KIND_IMAGE),
        ("out.png", "not really a png", "diagram", acp_outputs.KIND_DIAGRAM),
        ("out.py", "print('hi')", None, acp_outputs.KIND_CODE),
        ("out.json", '{"a": 1}', None, acp_outputs.KIND_RECORD),
        ("out.pdf", "%PDF-ish", None, "application/pdf"),
    ],
    ids=["text", "ui", "image", "diagram-exception", "code", "record", "verbatim-media-type"],
)
def test_a_stub_cli_entrys_output_file_is_served_by_its_output_kind(
    tmp_path: Path,
    store: sqlite3.Connection,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content: str,
    produces: str | None,
    expected_kind: str,
) -> None:
    """AC1: one file, written by a real delegated child, is served under the kind
    :func:`~harnessed.acp.outputs.kind_for` names for its media type (and, for the one named
    exception, ``produces``)."""
    monkeypatch.setenv("HARNESSED_STUB_WRITE_FILES", json.dumps({filename: content}))
    bus = EventBus(conn=store)
    spec = cli_spec(tmp_path, outputs="*", produces=produces)

    routed.run_agent(
        spec, "write the file", "done", home=home, bus=bus, store=store, message_seq=1, hop=0
    )

    name, kind, media_type, *_ = file_row(store, bus.run_id)
    assert name == filename
    assert kind == expected_kind
    capture(
        "kinds.txt",
        f"produces={produces!r:>10}  file={filename:<10}  media_type={media_type:<16}  "
        f"output_kind={kind}\n",
        append=True,
    )


# ---- AC2: a text output holding a seeded key is redacted; a binary output is stored as written --


def test_a_text_output_holding_a_seeded_key_is_redacted_and_a_binary_one_is_stored_as_written(
    tmp_path: Path, store: sqlite3.Connection, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-ant-abcdefgh12345678"
    monkeypatch.setenv(
        "HARNESSED_STUB_WRITE_FILES",
        json.dumps({"secret.txt": f"key: {secret} end", "secret.bin": f"key: {secret} end"}),
    )
    bus = EventBus(conn=store)
    spec = cli_spec(tmp_path, outputs="*")

    routed.run_agent(
        spec, "write the files", "done", home=home, bus=bus, store=store, message_seq=1, hop=0
    )

    rows = {
        name: (kind, bytes(body))
        for name, kind, body in store.execute(
            "SELECT name, output_kind, body FROM outputs WHERE run_id = ? AND position > 0",
            (bus.run_id,),
        )
    }
    text_kind, text_body = rows["secret.txt"]
    binary_kind, binary_body = rows["secret.bin"]
    assert text_kind == acp_outputs.KIND_TEXT
    assert text_body.decode("utf-8") == "key: [redacted:anthropic_key] end"
    assert secret not in text_body.decode("utf-8")
    assert binary_body.decode("utf-8") == f"key: {secret} end", "a binary body is not scrubbed"
    capture(
        "redaction.txt",
        "ticket 181 - a seeded key in a text output is redacted; a binary output is not\n\n"
        f"secret.txt (kind={text_kind}): stored as {text_body.decode('utf-8')!r}\n"
        f"secret.bin (kind={binary_kind}): stored unchanged, secret present: "
        f"{secret in binary_body.decode('utf-8')}\n",
    )


# ---- AC3: confinement and the cap ----------------------------------------------------------------


def test_a_file_outside_the_working_directory_is_never_stored(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("far away", encoding="utf-8")
    (cwd / "keep.txt").write_text("kept", encoding="utf-8")
    (cwd / "escaped.txt").symlink_to(target)

    ids = acp_outputs.record_hop(
        store,
        run_id="run-escape",
        message_seq=1,
        hop=0,
        task_hash=None,
        summary="ok",
        cwd=cwd,
        before={},
        produces=None,
        outputs_glob="*.txt",
    )
    assert len(ids) == 2, "position 0 plus exactly one file: escaped.txt is never counted"
    names = [
        row[0]
        for row in store.execute(
            "SELECT name FROM outputs WHERE run_id = ? AND position > 0", ("run-escape",)
        )
    ]
    assert names == ["keep.txt"]


def test_a_ninth_matching_file_is_never_stored(tmp_path: Path, store: sqlite3.Connection) -> None:
    for index in range(1, 10):
        (tmp_path / f"f{index}.txt").write_text(f"file {index}", encoding="utf-8")

    ids = acp_outputs.record_hop(
        store,
        run_id="run-cap",
        message_seq=1,
        hop=0,
        task_hash=None,
        summary="ok",
        cwd=tmp_path,
        before={},
        produces=None,
        outputs_glob="*.txt",
    )
    assert len(ids) == 1 + acp_outputs.MAX_FILES
    names = [
        row[0]
        for row in store.execute(
            "SELECT name FROM outputs WHERE run_id = ? AND position > 0 ORDER BY position",
            ("run-cap",),
        )
    ]
    assert names == [f"f{index}.txt" for index in range(1, 9)], "the first 8 by name, f9 dropped"


def test_a_file_over_the_cap_is_stored_too_large_with_a_null_body(
    tmp_path: Path, store: sqlite3.Connection
) -> None:
    big_file(tmp_path)

    ids = acp_outputs.record_hop(
        store,
        run_id="run-toolarge",
        message_seq=1,
        hop=0,
        task_hash=None,
        summary="ok",
        cwd=tmp_path,
        before={},
        produces=None,
        outputs_glob="*.bin",
    )
    assert len(ids) == 2
    name, kind, media_type, size_bytes, too_large, body = file_row(store, "run-toolarge")
    assert name == "big.bin"
    assert size_bytes == acp_outputs.OUTPUT_CAP_BYTES + 1
    assert too_large == 1
    assert body is None
    capture(
        "confinement-and-cap.txt",
        "ticket 181 - AC3: a file outside cwd, a ninth file, a file over the cap\n\n"
        "outside cwd: never stored (escaped.txt absent from the ledger)\n"
        "ninth file: never stored (f9.txt absent, f1..f8 present)\n"
        f"over the cap: stored too_large=1, size_bytes={size_bytes}, body=NULL "
        f"(kind={kind}, media_type={media_type})\n",
    )


# ---- AC4: GET /v1/outputs/{output_id} --------------------------------------------------------


def client_over(home: Path) -> TestClient:
    return TestClient(create_app(home / db.DATABASE_FILENAME), raise_server_exceptions=False)


def test_the_route_serves_stored_bytes_with_their_media_type_and_the_two_headers(
    tmp_harnessed_home: Path,
) -> None:
    with closing(db.connect()) as conn:
        (output_id,) = acp_outputs.record_hop(
            conn,
            run_id="run-serve",
            message_seq=1,
            hop=0,
            task_hash=None,
            summary="hello from the ledger",
            cwd=Path.cwd(),
            before={},
            produces=None,
            outputs_glob=None,
        )

    with client_over(tmp_harnessed_home) as client:
        response = client.get(f"/v1/outputs/{output_id}")

    assert response.status_code == 200
    assert response.content == b"hello from the ledger"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    capture(
        "served-headers.txt",
        "ticket 181 - AC4: the served headers over a known id\n\n"
        f"status: {response.status_code}\n"
        f"content-type: {response.headers['content-type']}\n"
        f"x-content-type-options: {response.headers['x-content-type-options']}\n"
        f"content-security-policy: {response.headers['content-security-policy']}\n",
    )


def test_an_unknown_or_too_large_id_is_404_surfacenotfound_naming_which(
    tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    big_file(tmp_path)
    with closing(db.connect()) as conn:
        ids = acp_outputs.record_hop(
            conn,
            run_id="run-404",
            message_seq=1,
            hop=0,
            task_hash=None,
            summary="ok",
            cwd=tmp_path,
            before={},
            produces=None,
            outputs_glob="*.bin",
        )
    too_large_id = ids[1]

    with client_over(tmp_harnessed_home) as client:
        unknown = client.get("/v1/outputs/out_0000000000000000")
        too_large = client.get(f"/v1/outputs/{too_large_id}")

    assert unknown.status_code == 404
    assert too_large.status_code == 404
    unknown_body = unknown.json()
    too_large_body = too_large.json()
    assert unknown_body["error"] == "SurfaceNotFound"
    assert too_large_body["error"] == "SurfaceNotFound"
    assert "no output" in unknown_body["message"]
    assert "over the serving cap" in too_large_body["message"]
    assert unknown_body["message"] != too_large_body["message"], "the two causes read differently"


def test_no_path_a_request_supplies_ever_reaches_open(tmp_harnessed_home: Path) -> None:
    """``body`` answers straight off the ``outputs`` table; the route never calls ``open()``."""
    with closing(db.connect()) as conn:
        (output_id,) = acp_outputs.record_hop(
            conn,
            run_id="run-noopen",
            message_seq=1,
            hop=0,
            task_hash=None,
            summary="never touch disk",
            cwd=Path.cwd(),
            before={},
            produces=None,
            outputs_glob=None,
        )

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a request path called open()")

    with (
        client_over(tmp_harnessed_home) as client,
        mock.patch("builtins.open", _forbidden),
        mock.patch.object(Path, "open", _forbidden),
    ):
        response = client.get(f"/v1/outputs/{output_id}")

    assert response.status_code == 200
    assert response.content == b"never touch disk"
