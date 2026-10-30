"""FR-67: ``GET /v1/runs/totals`` counts loop runs, turns and tool calls from the run journal.

The aggregate is ``loop/runs.py::totals`` over ``turns`` and ``tool_results``, emitted by
``totals_payload`` (decision 41): the handler makes that one store call and hands it to that
one emitter, and ``harnessed runs totals --json`` prints the same bytes (decision 38, MUST rule
16). Every count crosses the wire as a **string** (MUST rule 29), an empty journal counts to
``"0"`` and never ``null``, and ``runs`` is distinct run keys of both key kinds - a five-case eval
is five case runs and one ``harnessed run`` is one session, so a journal holding both counts six
(decision 24, MUST rule 15).

Decision 47 measured the read at 20.2 ms against 50,000 turns and 500,000 events and closed the
window question on that number: the route serves all time. The slow test here re-records the
timing at the same turn ceiling as evidence, and asserts only the 200 ms NFR; a read over 150 ms
is the trigger to reopen the decision, and that is the gate's call, not this file's.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sqlite3
import statistics
import textwrap
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnessed import clock
from harnessed.cli.app import app
from harnessed.evals import trace
from harnessed.gateway import create_app
from harnessed.gateway.routes import runs as runs_routes
from harnessed.loop import runs
from harnessed.loop.journal import Journal
from harnessed.memory import db
from harnessed.providers.translate import tool_call_payload
from harnessed.providers.types import (
    ChatRequest,
    ChatResponse,
    Message,
    ToolCall,
    ToolResult,
    Usage,
)
from harnessed.serialize import canonical
from test_gateway_parity import PARITY_SET, assert_parity

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids", "clean_env")

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "126"
ROUTES_MODULE = REPO_ROOT / "src" / "harnessed" / "gateway" / "routes" / "runs.py"

TOTALS_PATH = "/v1/runs/totals"
TOTALS_ARGV = ["runs", "totals", "--json"]

#: The shape FR-67 names, and the two figures beside the three counts.
PAYLOAD_KEYS = frozenset({"runs", "turns", "tool_calls", "key_kinds", "counted_at"})
COUNT_KEYS = ("runs", "turns", "tool_calls")

MODEL = "fake-model"

#: A SQL statement as SQLite would parse one: a verb, then its clause keyword, both upper case.
#: Prose in a docstring says "from the store" in lower case and is not a statement.
SQL_STATEMENT = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|WITH)\b.*\b(FROM|INTO|SET|AS)\b")

#: Decision 47's ceiling for this read, and the figure it measured there.
CEILING_TURNS = 50_000
CEILING_TOOL_RESULTS = 50_000
DECISION_47_MS = 20.2
NFR_SECONDS = 0.200


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- benches -------------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_harnessed_home: Path) -> Iterator[TestClient]:
    """The gateway over the database ``conn`` writes to."""
    application = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def journal_turns(conn: sqlite3.Connection, run_key: str, *tool_calls_per_turn: int) -> None:
    """One turn per entry under ``run_key``, each with that many tool rows, the last terminal."""
    journal = Journal(conn, run_key, serialize_call=tool_call_payload, provider="fake")
    request = ChatRequest(model=MODEL, system="", messages=(Message(role="user", content="hi"),))
    for iteration, calls in enumerate(tool_calls_per_turn, start=1):
        last = iteration == len(tool_calls_per_turn)
        wanted = tuple(
            ToolCall(id=f"call_{iteration}_{n}", name="echo", arguments={"n": n})
            for n in range(calls)
        )
        response = ChatResponse(
            response_id=f"resp_{iteration}",
            model=MODEL,
            content="done" if last else "",
            tool_calls=wanted,
            stop_reason="end_turn" if last else "tool_use",
        )
        results = tuple(ToolResult(call_id=call.id, content="ok") for call in wanted)
        journal.record(iteration, request, response, results)


SESSION_KEY = "run_01HZZZZZZZZZZZZZZZZZZZZZZ1"
CASE_RUN_KEYS = (
    "eval_01HZZZZZZZZZZZZZZZZZZZZZZ1:case-a:1",
    "eval_01HZZZZZZZZZZZZZZZZZZZZZZ1:case-b:1",
)


def seed_journal(conn: sqlite3.Connection) -> dict[str, int]:
    """One session of three turns with four tool rows, two case runs of one turn each with one.

    Returns what a reader should count: three keys, five turns, six tool rows.
    """
    journal_turns(conn, SESSION_KEY, 2, 2, 0)
    for key in CASE_RUN_KEYS:
        journal_turns(conn, key, 1)
    return {"runs": 3, "turns": 5, "tool_calls": 6}


def cli_line(argv: list[str]) -> bytes:
    """One ``--json`` line from the in-process CLI, as bytes."""
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, f"{argv} exited {result.exit_code}\n{result.output}"
    return result.stdout.encode("utf-8")


# ---- AC 1: the payload over a seeded journal --------------------------------------------------


def test_the_payload_counts_runs_of_both_key_kinds_turns_and_tool_calls(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    expected = seed_journal(conn)
    response = client.get(TOTALS_PATH)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == PAYLOAD_KEYS
    assert payload["runs"] == str(expected["runs"]), "one session plus two case runs is three"
    assert payload["turns"] == str(expected["turns"])
    assert payload["tool_calls"] == str(expected["tool_calls"])
    assert payload["key_kinds"] == ["session", "case_run"]
    assert payload["key_kinds"] == list(runs.KEY_KINDS)
    assert payload["counted_at"] == clock.isoformat(clock.now())
    capture(
        "totals-served.txt",
        "ticket 126 - GET /v1/runs/totals over one session (3 turns, 4 tool rows) and two "
        "case runs (1 turn, 1 tool row each)\n"
        f"GET {TOTALS_PATH}\n{response.content.decode()}\n",
    )


def test_runs_is_distinct_keys_never_rows(conn: sqlite3.Connection, client: TestClient) -> None:
    """Three turns under one key are one run; the count is over keys, not over rows."""
    journal_turns(conn, SESSION_KEY, 0, 0, 0)
    payload = client.get(TOTALS_PATH).json()
    assert payload["runs"] == "1"
    assert payload["turns"] == "3"
    assert payload["tool_calls"] == "0"


def test_the_store_read_is_one_callable_and_its_emitter(conn: sqlite3.Connection) -> None:
    """``runs.totals`` reads; ``runs.totals_payload`` spells the wire shape. Nothing else."""
    seed_journal(conn)
    found = runs.totals(conn)
    assert (found.runs, found.turns, found.tool_calls) == (3, 5, 6)
    assert found.counted_at == clock.isoformat(clock.now())
    payload = runs.totals_payload(found)
    assert list(payload) == ["runs", "turns", "tool_calls", "key_kinds", "counted_at"]
    assert payload == {
        "runs": "3",
        "turns": "5",
        "tool_calls": "6",
        "key_kinds": ["session", "case_run"],
        "counted_at": clock.isoformat(clock.now()),
    }


# ---- AC 2: every count is a string on the wire ------------------------------------------------


def test_every_count_is_a_string_on_the_wire(conn: sqlite3.Connection, client: TestClient) -> None:
    """MUST rule 29: the characters the tile renders are the characters that were sent."""
    seed_journal(conn)
    response = client.get(TOTALS_PATH)
    payload = json.loads(response.content)
    for key in COUNT_KEYS:
        assert isinstance(payload[key], str), f"{key} crossed as {type(payload[key]).__name__}"
        assert payload[key].isdigit(), f"{key} is not a decimal count: {payload[key]!r}"
    assert isinstance(payload["counted_at"], str)
    assert isinstance(payload["key_kinds"], list)
    for key in COUNT_KEYS:
        assert f'"{key}":"{payload[key]}"'.encode() in response.content, key
    capture(
        "wire-types.txt",
        "ticket 126 - typeof every count on the wire\n"
        + "\n".join(f"{key}: {type(payload[key]).__name__} {payload[key]!r}" for key in payload)
        + "\n",
    )


# ---- AC 3: an empty journal is a measurement of zero ------------------------------------------


def test_an_empty_journal_serves_zero_as_a_string_never_null(client: TestClient) -> None:
    response = client.get(TOTALS_PATH)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == PAYLOAD_KEYS
    for key in COUNT_KEYS:
        assert payload[key] == "0", (key, payload[key])
        assert payload[key] is not None
    assert None not in payload.values()
    capture(
        "empty-journal.txt",
        f"ticket 126 - GET {TOTALS_PATH} over an empty journal\n{response.content.decode()}\n",
    )


def test_totals_is_a_route_not_a_run_key(client: TestClient) -> None:
    """``/v1/runs/totals`` answers 200 on an empty store; as a run key it would be 404."""
    assert client.get(TOTALS_PATH).status_code == 200
    assert client.get("/v1/runs/totals_").status_code == 404


# ---- AC 4: the handler is one call, one emitter, no arithmetic, no SQL ------------------------


def scanned(func: Callable[..., Any]) -> ast.AST:
    """The handler's own body, annotations and decorators lifted off.

    ``str | None`` is a ``BinOp`` and ``@app.get(...)`` is a call; neither is what the handler
    does, and a scan that counted them would push the code around to stay green.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node.returns = None
            node.decorator_list = []
        elif isinstance(node, ast.arg | ast.AnnAssign):
            node.annotation = None  # type: ignore[assignment]
    return tree


def totals_handler(home: Path) -> Callable[..., Any]:
    application = create_app(home / db.DATABASE_FILENAME)
    matches = [
        route.endpoint
        for route in application.routes
        if getattr(route, "path", None) == TOTALS_PATH
    ]
    assert len(matches) == 1, f"{TOTALS_PATH} is registered {len(matches)} times"
    return matches[0]


def test_the_handler_calls_one_store_callable_plus_its_emitter_and_nothing_else(
    tmp_harnessed_home: Path,
) -> None:
    tree = scanned(totals_handler(tmp_harnessed_home))
    calls = [ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    passes_conn = [
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(isinstance(arg, ast.Name) and arg.id == "conn" for arg in node.args)
    ]
    assert passes_conn == ["runs.totals"], calls
    assert sorted(calls) == ["runs.totals", "runs.totals_payload"], calls
    arithmetic = [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp | ast.AugAssign | ast.UnaryOp | ast.Compare)
    ]
    assert arithmetic == [], "decision 37: the handler computes nothing"


def test_the_route_module_holds_no_sql() -> None:
    """MUST rule 14: a query belongs to the package that owns its table."""
    tree = ast.parse(ROUTES_MODULE.read_text(encoding="utf-8"))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    sql = [text for text in literals if SQL_STATEMENT.search(text)]
    assert sql == [], sql
    assert ".execute(" not in inspect.getsource(runs_routes)
    assert SQL_STATEMENT.search("SELECT COUNT(*) FROM turns"), "the scan can see a statement"
    assert not SQL_STATEMENT.search("404 from the store"), "prose is not a statement"


# ---- AC 5: the CLI twin is the same bytes and sits in the parity set --------------------------


def test_the_cli_twin_prints_the_route_bytes(conn: sqlite3.Connection, client: TestClient) -> None:
    seed_journal(conn)
    line = cli_line(TOTALS_ARGV)
    response = client.get(TOTALS_PATH)
    assert_parity(line, response)
    assert line[:-1] == canonical(response.json()).encode("utf-8")
    capture(
        "parity-diff.txt",
        "ticket 126 - harnessed runs totals --json against GET /v1/runs/totals\n"
        f"cli:  {line[:-1].decode()}\n"
        f"http: {response.content.decode()}\n"
        "parity: equal as bytes\n",
    )


def test_the_pair_is_in_the_parity_set() -> None:
    pairs = [pair for pair in PARITY_SET if pair.route == TOTALS_PATH]
    assert len(pairs) == 1, [pair.route for pair in PARITY_SET]
    (pair,) = pairs
    assert list(pair.cli_argv) == TOTALS_ARGV
    assert not pair.produces


def test_the_cli_twin_without_the_flag_is_prose() -> None:
    result = CliRunner().invoke(app, ["runs", "totals"])
    assert result.exit_code == 0, result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
    assert "0" in result.stdout


# ---- decision 47: the timing at the seeded ceiling, re-recorded -------------------------------


def seed_ceiling(conn: sqlite3.Connection) -> None:
    """Fifty thousand turns and as many tool rows, straight into the ledgers.

    Written as rows rather than through ``Journal.record`` because the journal's redaction and
    idempotency checks would make the seeding, not the read, the thing being timed.
    """
    at = clock.isoformat(clock.now())
    runs_per_kind = CEILING_TURNS // 10
    turns = (
        (
            f"run_{n:026d}" if n % 2 else f"eval_{n:026d}:case:{n % 5}",
            iteration,
            "{}",
            "",
            "end_turn",
            at,
        )
        for n in range(runs_per_kind)
        for iteration in range(1, 11)
    )
    conn.executemany(
        "INSERT INTO turns (run_id, iteration, request, response, stop_reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        turns,
    )
    results = (
        (
            f"run_{n:026d}" if n % 2 else f"eval_{n:026d}:case:{n % 5}",
            iteration,
            f"call_{n}_{iteration}",
            "echo",
            "{}",
            "ok",
            1,
            at,
        )
        for n in range(runs_per_kind)
        for iteration in range(1, 11)
    )
    conn.executemany(
        "INSERT INTO tool_results (run_id, iteration, call_id, tool_name, arguments, output, "
        "ok, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        results,
    )
    conn.commit()


@pytest.mark.slow
def test_the_read_at_the_ceiling_is_inside_the_nfr(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    seed_ceiling(conn)
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    samples: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        readonly = db.connect_readonly(path)
        try:
            found = runs.totals(readonly)
        finally:
            readonly.close()
        samples.append(time.perf_counter() - started)
    median = statistics.median(samples)
    assert (found.runs, found.turns, found.tool_calls) == (
        CEILING_TURNS // 10,
        CEILING_TURNS,
        CEILING_TOOL_RESULTS,
    )
    capture(
        "ceiling-timing.txt",
        f"ticket 126 - runs.totals over {CEILING_TURNS} turns and {CEILING_TOOL_RESULTS} tool "
        "results, five reads each through a fresh connect_readonly\n"
        + "\n".join(f"read {n + 1}: {seconds * 1000:.1f} ms" for n, seconds in enumerate(samples))
        + f"\nmedian: {median * 1000:.1f} ms\ndecision 47 measured: {DECISION_47_MS} ms\n"
        f"NFR: {NFR_SECONDS * 1000:.0f} ms; reopen trigger: 150 ms\n",
    )
    assert median < NFR_SECONDS, f"median {median * 1000:.1f} ms is over the 200 ms NFR"


# ===============================================================================================
# ticket 127 (FR-68): GET /v1/runs/turn_seconds, a run's span, and per-iteration usage and seconds
# ===============================================================================================
#
# Decision 54: ``created_at - requested_at`` already bounds a turn, so nothing here migrates. The
# count runs in SQL, the subtraction runs in Python through ``clock.parse``, and the mean is
# rounded to one decimal in ``loop/runs.py`` and emitted as a string (MUST rule 29). A turn
# recorded before migration 007 has no ``requested_at``: it is excluded and counted, never
# averaged as a silent subset, and a journal with nothing to average serves ``null``, never
# ``"0.0"``.

EVIDENCE_127 = REPO_ROOT / "evidence" / "127"
TURN_SECONDS_PATH = "/v1/runs/turn_seconds"
TURN_SECONDS_ARGV = ["runs", "turn-seconds", "--json"]
TURN_SECONDS_KEYS = ("average_seconds", "turns_counted", "turns_excluded", "excluded_reason")
EXCLUDED_REASON = "predates migration 007"
RUN_CMD_MODULE = REPO_ROOT / "src" / "harnessed" / "cli" / "run_cmd.py"


def capture_127(name: str, text: str) -> Path:
    EVIDENCE_127.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_127 / name
    path.write_text(text, encoding="utf-8")
    return path


def journal_timed_turns(
    conn: sqlite3.Connection, run_key: str, *seconds_per_turn: float | None
) -> None:
    """One turn per entry under ``run_key``: sent that many seconds before it was recorded, or
    with no send time at all (``None``), as a row from before migration 007 has."""
    journal = Journal(conn, run_key, serialize_call=tool_call_payload, provider="fake")
    request = ChatRequest(model=MODEL, system="", messages=(Message(role="user", content="hi"),))
    for iteration, seconds in enumerate(seconds_per_turn, start=1):
        last = iteration == len(seconds_per_turn)
        response = ChatResponse(
            response_id=f"resp_{iteration}",
            model=MODEL,
            content="done" if last else "",
            stop_reason="end_turn" if last else "tool_use",
            usage=Usage(
                input_tokens=10 * iteration, output_tokens=iteration, cached_input_tokens=1
            ),
        )
        sent = None if seconds is None else clock.now() - timedelta(seconds=seconds)
        journal.record(iteration, request, response, (), requested_at=sent)


def payload_of(client: TestClient, path: str) -> tuple[Any, dict[str, Any]]:
    response = client.get(path)
    assert response.status_code == 200, (path, response.text)
    return response, response.json()


# ---- AC 1: the payload over a seeded journal --------------------------------------------------


def test_turn_seconds_payload_is_the_four_named_keys_and_a_one_decimal_string(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    journal_timed_turns(conn, SESSION_KEY, 3, 4)
    response, payload = payload_of(client, TURN_SECONDS_PATH)
    assert set(payload) == set(TURN_SECONDS_KEYS)
    assert payload == {
        "average_seconds": "3.5",
        "turns_counted": "2",
        "turns_excluded": "0",
        "excluded_reason": EXCLUDED_REASON,
    }
    assert b'"average_seconds":"3.5"' in response.content, "a string on the wire, MUST rule 29"
    assert b'"turns_counted":"2"' in response.content
    capture_127(
        "payload-mean.txt",
        "ticket 127 - GET /v1/runs/turn_seconds over two timed turns of 3 s and 4 s\n"
        f"{response.content.decode()}\n",
    )


def test_the_mean_is_rounded_to_one_decimal_in_the_store(conn: sqlite3.Connection) -> None:
    """1.25 s and 1.0 s average 1.125; the store rounds to ``1.1`` and emits those characters."""
    journal_timed_turns(conn, SESSION_KEY, 1.25, 1.0)
    found = runs.turn_seconds(conn)
    assert (found.turns_counted, found.turns_excluded) == (2, 0)
    payload = runs.turn_seconds_payload(found)
    assert payload["average_seconds"] == "1.1"
    assert isinstance(payload["average_seconds"], str)
    assert payload["excluded_reason"] == runs.EXCLUDED_REASON == EXCLUDED_REASON


# ---- AC 2: nothing to average is null, never "0.0" --------------------------------------------


def test_zero_counted_serves_null_never_zero_point_zero(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    response, empty = payload_of(client, TURN_SECONDS_PATH)
    assert empty == {
        "average_seconds": None,
        "turns_counted": "0",
        "turns_excluded": "0",
        "excluded_reason": EXCLUDED_REASON,
    }
    assert b'"average_seconds":null' in response.content
    assert b'"0.0"' not in response.content

    journal_timed_turns(conn, SESSION_KEY, None, None)
    response, only_pre_007 = payload_of(client, TURN_SECONDS_PATH)
    assert only_pre_007["average_seconds"] is None
    assert only_pre_007["turns_counted"] == "0"
    assert only_pre_007["turns_excluded"] == "2"
    capture_127(
        "payload-null.txt",
        "ticket 127 - GET /v1/runs/turn_seconds with nothing to average\n"
        f"empty journal:       {canonical(empty)}\n"
        f"two pre-007 turns:   {canonical(only_pre_007)}\n",
    )


# ---- AC 3: the excluded turns are counted and named, never averaged as a silent subset -------


def test_a_pre_007_turn_is_excluded_and_counted(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    journal_timed_turns(conn, SESSION_KEY, None, 3, 4)
    response, payload = payload_of(client, TURN_SECONDS_PATH)
    assert payload["turns_excluded"] == "1"
    assert payload["turns_counted"] == "2"
    assert payload["average_seconds"] == "3.5", "the mean of the two timed turns"
    assert payload["excluded_reason"] == EXCLUDED_REASON
    capture_127(
        "payload-excluded.txt",
        "ticket 127 - one pre-007 turn (requested_at null) beside two timed turns (3 s, 4 s)\n"
        f"{response.content.decode()}\n",
    )


def test_the_count_is_sql_and_the_subtraction_is_python() -> None:
    """Decision 54: SQLite never subtracts the two timestamps; ``clock.parse`` does."""
    source = inspect.getsource(runs)
    assert "clock.parse(" in source
    for verb in ("julianday", "strftime", "unixepoch"):
        assert verb not in source, f"loop/runs.py bets a figure on SQLite's {verb}"
    assert re.search(r"COUNT\(", source), "the count runs in SQL"


# ---- AC 4: a run's span on /runs/{run_key} and harnessed run --json, from one module -----------


def test_run_payload_carries_duration_seconds_from_the_same_module(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    journal_timed_turns(conn, SESSION_KEY, 2)
    clock.freeze(clock.now() + timedelta(seconds=5))
    journal_timed_turns(conn, CASE_RUN_KEYS[0], 1.0)
    # A second turn under the session key, recorded five seconds later, stretches the span.
    Journal(conn, SESSION_KEY, serialize_call=tool_call_payload, provider="fake").record(
        2,
        ChatRequest(model=MODEL, system="", messages=(Message(role="user", content="hi"),)),
        ChatResponse(response_id="resp_2", model=MODEL, content="done", stop_reason="end_turn"),
        (),
        requested_at=clock.now() - timedelta(seconds=1),
    )
    response, payload = payload_of(client, f"/v1/runs/{SESSION_KEY}")
    assert payload["duration_seconds"] == "7.0", "first request sent to last turn recorded"
    assert payload["duration_reason"] is None
    assert payload == runs.run_payload(conn, SESSION_KEY)
    assert b'"duration_seconds":"7.0"' in response.content
    assert client.get(f"/v1/runs/{CASE_RUN_KEYS[0]}").json()["duration_seconds"] == "1.0"
    capture_127(
        "run-duration.txt",
        "ticket 127 - GET /v1/runs/{run_key} carries duration_seconds from loop/runs.py\n"
        f"{response.content.decode()}\n",
    )


def test_run_payload_duration_is_null_with_the_007_reason_when_the_first_turn_is_untimed(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    journal_timed_turns(conn, SESSION_KEY, None, 3)
    _response, payload = payload_of(client, f"/v1/runs/{SESSION_KEY}")
    assert payload["duration_seconds"] is None
    assert payload["duration_reason"] == EXCLUDED_REASON
    assert payload["started_at"], "started_at still falls back to created_at, never null"


def test_the_cli_and_the_route_read_the_run_payload_from_one_callable() -> None:
    """``harnessed run --json`` and ``GET /v1/runs/{run_key}`` both call ``runs.run_payload``;
    neither spells ``duration_seconds`` by hand."""
    run_cmd_source = RUN_CMD_MODULE.read_text(encoding="utf-8")
    routes_source = ROUTES_MODULE.read_text(encoding="utf-8")
    assert "runs.run_payload(" in run_cmd_source
    assert "runs.run_payload(" in routes_source
    assert "duration_seconds" not in run_cmd_source
    assert "duration_seconds" not in routes_source
    assert "duration_seconds" in inspect.getsource(runs.run_payload)


# ---- AC 5: the trace's per-iteration objects carry usage and seconds --------------------------


def test_trace_objects_carry_usage_and_seconds_per_iteration(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    journal_timed_turns(conn, SESSION_KEY, None, 2.5)
    objects = trace.assemble(conn, SESSION_KEY)
    assert [obj["iteration"] for obj in objects] == [1, 2]
    first, second = objects
    assert first["seconds"] is None, "a pre-007 turn has no send time"
    assert second["seconds"] == "2.5"
    assert first["usage"] == {"input_tokens": 10, "output_tokens": 1, "cached_input_tokens": 1}
    assert second["usage"] == {"input_tokens": 20, "output_tokens": 2, "cached_input_tokens": 1}
    response, served = payload_of(client, f"/v1/runs/{SESSION_KEY}/trace")
    assert served == json.loads(canonical(list(objects)))
    assert b'"seconds":"2.5"' in response.content
    capture_127(
        "trace-row.txt",
        "ticket 127 - one trace object per iteration, each carrying usage and seconds\n"
        + "\n".join(
            canonical({k: obj[k] for k in ("iteration", "usage", "seconds")}) for obj in objects
        )
        + "\n",
    )


def test_trace_usage_is_null_for_a_pre_006_turn(conn: sqlite3.Connection) -> None:
    journal_timed_turns(conn, SESSION_KEY, 1)
    conn.execute(
        "UPDATE turns SET input_tokens = NULL WHERE run_id = ? AND iteration = 1", (SESSION_KEY,)
    )
    conn.commit()
    (obj,) = trace.assemble(conn, SESSION_KEY)
    assert obj["usage"] is None
    assert obj["seconds"] == "1.0"


# ---- AC 6: the CLI twin is byte-identical and the pair is in the parity set -------------------


def test_the_turn_seconds_cli_twin_prints_the_route_bytes(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    journal_timed_turns(conn, SESSION_KEY, None, 3, 4)
    line = cli_line(TURN_SECONDS_ARGV)
    response = client.get(TURN_SECONDS_PATH)
    assert_parity(line, response)
    capture_127(
        "parity-diff.txt",
        "ticket 127 - harnessed runs turn-seconds --json against GET /v1/runs/turn_seconds\n"
        f"cli:  {line[:-1].decode()}\n"
        f"http: {response.content.decode()}\n"
        "parity: equal as bytes\n",
    )


def test_the_turn_seconds_pair_is_in_the_parity_set() -> None:
    pairs = [pair for pair in PARITY_SET if pair.route == TURN_SECONDS_PATH]
    assert len(pairs) == 1, [pair.route for pair in PARITY_SET]
    (pair,) = pairs
    assert list(pair.cli_argv) == TURN_SECONDS_ARGV
    assert not pair.produces


def test_the_turn_seconds_cli_twin_without_the_flag_is_prose(tmp_harnessed_home: Path) -> None:
    result = CliRunner().invoke(app, ["runs", "turn-seconds"])
    assert result.exit_code == 0, result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
    assert "unknown" in result.stdout
    assert "no turn carries both timestamps" in result.stdout


def test_turn_seconds_is_a_route_not_a_run_key(client: TestClient) -> None:
    assert client.get(TURN_SECONDS_PATH).status_code == 200
    assert client.get("/v1/runs/turn_seconds_").status_code == 404


def test_the_turn_seconds_handler_is_one_store_call_and_its_emitter(
    tmp_harnessed_home: Path,
) -> None:
    application = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    matches = [
        route.endpoint
        for route in application.routes
        if getattr(route, "path", None) == TURN_SECONDS_PATH
    ]
    assert len(matches) == 1
    tree = scanned(matches[0])
    calls = sorted(ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call))
    assert calls == ["runs.turn_seconds", "runs.turn_seconds_payload"], calls
    arithmetic = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp | ast.AugAssign | ast.UnaryOp | ast.Compare)
    ]
    assert arithmetic == [], "the handler computes nothing; the store rounds"
