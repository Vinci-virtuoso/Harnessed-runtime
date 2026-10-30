"""Ticket 133 (FR-74): ``GET /v1/spend``, where an unpriced turn is not a free one.

Decision 53 and MUST rules 28 and 29. ``loop/runs.py::spend`` joins ``turns`` to ``prices``,
multiplies with ``decimal.Decimal``, rounds to four decimals in the store and emits a string.
The three null classes - a turn from before migration 006 (no usage), a turn from before 010 (no
provider or model), a turn whose model the catalog does not price - are counted and served
**beside** ``total``, never folded into it. Zero priced turns is ``total: null``, never
``"0.0000"``, and a missing cached rate makes a turn unpriced rather than priced at a substitute.

The bench writes ``turns`` and ``prices`` rows directly: a pre-006 or pre-010 row is exactly the
row a current ``Journal.record`` can no longer write, and ``prices refresh`` asks the network.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sqlite3
import statistics
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnessed import clock
from harnessed.acp import config
from harnessed.cli.app import app, main
from harnessed.gateway import create_app
from harnessed.loop import runs
from harnessed.memory import db
from harnessed.serialize import canonical
from test_gateway_parity import PARITY_SET, REFUSAL_SET, assert_parity

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "133"
EVIDENCE_180 = REPO_ROOT / "evidence" / "180"
RUNS_MODULE = REPO_ROOT / "src" / "harnessed" / "loop" / "runs.py"
NULL_REASONS_TS = REPO_ROOT / "dashboard" / "lib" / "null-reasons.ts"

SPEND_PATH = "/v1/spend"
SPEND_ARGV = ["spend", "--json"]
PAYLOAD_KEYS = frozenset(
    {
        "total",
        "currency",
        "priced_turns",
        "unknown_usage_turns",
        "unknown_model_turns",
        "unpriced_turns",
        "unpriced_models",
        "prices_fetched_at",
        "router_cost",
        "router_decisions",
        "agent_cost",
        "api_equivalent",
        "not_metered_turns",
        "handlers",
    }
)
COUNT_KEYS = (
    "priced_turns",
    "unknown_usage_turns",
    "unknown_model_turns",
    "unpriced_turns",
    "router_decisions",
    "not_metered_turns",
)
SIX_DECIMALS = re.compile(r"^\d+\.\d{6}$")
#: The five v5 fields over a journal with no decision and no agent turn (decision 64).
#: No decision and no agent turn: the router/agent buckets are all null or zero. ``handlers``
#: is not part of this - a session with chat turns still carries its chat-model handler rows -
#: so each call site spreads this and then states its own ``handlers`` list.
NO_V5 = {
    "router_cost": None,
    "router_decisions": "0",
    "agent_cost": None,
    "api_equivalent": None,
    "not_metered_turns": "0",
}

#: The two sentences ``ux/design.md`` gives ``spend.total`` when it is null, verbatim.
NO_PRICES = "no prices fetched: run `harnessed prices refresh`"
NONE_PRICED = "no turn could be priced"

SESSION_A = "run_01HZZZZZZZZZZZZZZZZZZZZZZA"
SESSION_B = "run_01HZZZZZZZZZZZZZZZZZZZZZZB"
UNKNOWN_SESSION = "run_00000000000000000000000000"
FETCHED_AT = "2026-09-23T12:00:00Z"
OLDER_FETCH = "2026-09-20T08:30:00Z"

CEILING_TURNS = 50_000
DECISION_47_MS = 171.6
NFR_SECONDS = 0.200
REOPEN_SECONDS = 0.150


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- benches -------------------------------------------------------------------------------------


@pytest.fixture
def conn(clean_env: None, tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
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


def turn(
    conn: sqlite3.Connection,
    run_key: str,
    iteration: int,
    usage: tuple[int, int, int] | None,
    provider: str | None,
    model: str | None,
) -> None:
    """One ``turns`` row as whichever build wrote it: ``usage`` ``None`` is pre-006, a ``None``
    provider or model is pre-010."""
    inputs, outputs, cached = (None, None, None) if usage is None else usage
    conn.execute(
        "INSERT INTO turns (run_id, iteration, request, response, stop_reason, created_at, "
        "input_tokens, output_tokens, cached_input_tokens, provider, model) "
        "VALUES (?, ?, '{}', '', 'end_turn', ?, ?, ?, ?, ?, ?)",
        (run_key, iteration, FETCHED_AT, inputs, outputs, cached, provider, model),
    )
    conn.commit()


def price(
    conn: sqlite3.Connection,
    provider: str,
    model: str,
    rates: tuple[str, str, str | None],
    fetched_at: str = FETCHED_AT,
) -> None:
    """One ``prices`` row, every rate a decimal string per million tokens, as 011 stores it."""
    conn.execute(
        "INSERT INTO prices (provider, model, input, output, cached_input, currency, source_url, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, 'USD', 'https://example.test/models', ?)",
        (provider, model, *rates, fetched_at),
    )
    conn.commit()


def seed_catalog(conn: sqlite3.Connection) -> None:
    """Two priced models with a cache rate, one priced model without one."""
    price(conn, "openrouter", "anthropic/claude-sonnet-5", ("3", "15", "0.3"))
    price(conn, "openrouter", "openai/gpt-mini", ("0.15", "0.6", "0.075"), OLDER_FETCH)
    price(conn, "groq", "llama-no-cache", ("0.05", "0.08", None))


#: What the two priced turns of :func:`seed_five` cost, worked by hand in exact decimals:
#: 1234 x 3 + 567 x 15 = 12207 micro-dollars; (1000 - 400) x 0.15 + 400 x 0.075 + 2000 x 0.6
#: = 1320. 13527 per million is 0.013527, exact at six places (decision 64).
FIVE_TOTAL = "0.013527"


def seed_five(conn: sqlite3.Connection) -> None:
    """The ticket's journal: one pre-006 turn, one pre-010 turn, one turn on an unpriced model
    and two priced turns, across two sessions."""
    seed_catalog(conn)
    turn(conn, SESSION_A, 1, None, None, None)
    turn(conn, SESSION_A, 2, (1234, 567, 0), "openrouter", "anthropic/claude-sonnet-5")
    turn(conn, SESSION_B, 1, (10, 5, 0), None, None)
    turn(conn, SESSION_B, 2, (100, 10, 0), "openrouter", "vendor/unlisted")
    turn(conn, SESSION_B, 3, (1000, 2000, 400), "openrouter", "openai/gpt-mini")


def payload_of(client: TestClient, path: str = SPEND_PATH) -> tuple[Any, dict[str, Any]]:
    response = client.get(path)
    assert response.status_code == 200, (path, response.text)
    return response, response.json()


def cli_line(argv: list[str]) -> bytes:
    """One ``--json`` line from the in-process CLI, as bytes."""
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, f"{argv} exited {result.exit_code}\n{result.output}"
    return result.stdout.encode("utf-8")


def prose(argv: list[str]) -> str:
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, f"{argv} exited {result.exit_code}\n{result.output}"
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
    return result.stdout


# ---- AC 1: the payload, every figure a string ---------------------------------------------------


def test_the_payload_is_the_named_keys_every_figure_a_string(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    seed_five(conn)
    response, payload = payload_of(client)
    assert set(payload) == PAYLOAD_KEYS
    assert list(runs.spend_payload(runs.spend(conn))) == [
        "total",
        "currency",
        "priced_turns",
        "unknown_usage_turns",
        "unknown_model_turns",
        "unpriced_turns",
        "unpriced_models",
        "prices_fetched_at",
        "router_cost",
        "router_decisions",
        "agent_cost",
        "api_equivalent",
        "not_metered_turns",
        "handlers",
    ]
    assert payload["currency"] == "USD"
    assert isinstance(payload["total"], str) and SIX_DECIMALS.match(payload["total"])
    for key in COUNT_KEYS:
        assert isinstance(payload[key], str), key
    assert all(isinstance(model, str) for model in payload["unpriced_models"])
    assert payload["prices_fetched_at"] == FETCHED_AT, "the newest row's own fetched_at"
    assert b'"0.013527"' in response.content, "rounded in the store, never by the tile"
    capture(
        "payload-five-turns.txt",
        "ticket 133 - GET /v1/spend over one pre-006, one pre-010, one unpriced and two priced "
        f"turns\n{response.content.decode()}\n",
    )


# ---- AC 2: three null classes, counted, never folded --------------------------------------------


def test_the_three_null_classes_are_counted_beside_the_total_never_in_it(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    seed_five(conn)
    _, payload = payload_of(client)
    assert payload == {
        "total": FIVE_TOTAL,
        "currency": "USD",
        "priced_turns": "2",
        "unknown_usage_turns": "1",
        "unknown_model_turns": "1",
        "unpriced_turns": "1",
        "unpriced_models": ["openrouter/vendor/unlisted"],
        "prices_fetched_at": FETCHED_AT,
        **NO_V5,
        "handlers": [
            {
                "handler": "openrouter",
                "kind": runs.HANDLER_CHAT_MODEL,
                "billing": None,
                "turns": "3",
                "cost": "0.013527",
                "api_equivalent": None,
                "not_metered_turns": "1",
                "reason": None,
            },
        ],
    }
    exact = Decimal(1234) * 3 + Decimal(567) * 15 + 600 * Decimal("0.15")
    exact += 400 * Decimal("0.075") + 2000 * Decimal("0.6")
    assert payload["total"] == str((exact / 1_000_000).quantize(Decimal("0.000001"), ROUND_HALF_UP))


def test_money_is_decimal_never_float(conn: sqlite3.Connection) -> None:
    """``0.1 + 0.2`` is the defect class with a dollar sign on it: a float sum drifts, this does
    not, and the store module names no ``float`` on the spend path."""
    price(conn, "openrouter", "m/tenth", ("0.1", "0.2", None))
    turn(conn, SESSION_A, 1, (1_000_000, 1_000_000, 0), "openrouter", "m/tenth")
    found = runs.spend(conn)
    assert found.total == Decimal("0.300000")
    assert runs.spend_payload(found)["total"] == "0.300000"
    assert 0.1 + 0.2 != 0.3, "the float sum this refuses"
    source = inspect.getsource(runs.spend) + inspect.getsource(runs.spend_payload)
    assert "float" not in source


def test_the_total_rounds_half_up_to_six_places(conn: sqlite3.Connection) -> None:
    price(conn, "openrouter", "m/half", ("0.5", "0", None))
    turn(conn, SESSION_A, 1, (1, 0, 0), "openrouter", "m/half")
    # 0.0000005 exactly: half-even would serve 0.000000, and the store names its rule.
    assert runs.spend_payload(runs.spend(conn))["total"] == "0.000001"


# ---- AC 3: zero priced turns is null, and the reason is the tile's -------------------------------


def test_no_prices_serves_null_never_zero_and_names_the_refresh(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    turn(conn, SESSION_A, 1, (10, 5, 0), "openrouter", "anthropic/claude-sonnet-5")
    response, payload = payload_of(client)
    assert payload["total"] is None
    assert payload["prices_fetched_at"] is None
    assert payload["priced_turns"] == "0"
    assert payload["unpriced_turns"] == "1"
    assert b'"0.000000"' not in response.content
    text = prose(["spend"])
    assert NO_PRICES in text
    assert "0.0000" not in text
    capture(
        "zero-priced-no-prices.txt",
        f"ticket 133 - prices table empty\nhttp: {response.content.decode()}\ncli prose:\n{text}",
    )


def test_prices_but_no_priced_turn_serves_null_with_the_other_reason(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    seed_catalog(conn)
    turn(conn, SESSION_A, 1, None, None, None)
    turn(conn, SESSION_A, 2, (1, 1, 0), "openrouter", "vendor/unlisted")
    response, payload = payload_of(client)
    assert payload["total"] is None
    assert payload["prices_fetched_at"] == FETCHED_AT
    text = prose(["spend"])
    assert NONE_PRICED in text
    assert NO_PRICES not in text
    capture(
        "zero-priced-with-prices.txt",
        f"ticket 133 - prices stored, no turn priceable\nhttp: {response.content.decode()}\n"
        f"cli prose:\n{text}",
    )


def test_an_empty_journal_counts_zero_and_serves_null(client: TestClient) -> None:
    _, payload = payload_of(client)
    assert payload["total"] is None
    assert [payload[key] for key in COUNT_KEYS] == ["0", "0", "0", "0", "0", "0"]
    assert payload["unpriced_models"] == []


def test_the_tile_owns_both_reasons_verbatim() -> None:
    """D-2: the sentence is static copy keyed on route and path in ``null-reasons.ts``."""
    source = NULL_REASONS_TS.read_text(encoding="utf-8")
    assert source.count("/v1/spend") >= 2
    for sentence in (NO_PRICES, NONE_PRICED):
        assert sentence.replace("`", "\\`") in source or sentence in source, sentence
    assert runs.NO_PRICES_REASON == NO_PRICES
    assert runs.NONE_PRICED_REASON == NONE_PRICED


# ---- AC 4: a missing cached rate is unpriced, never substituted ---------------------------------


def test_a_missing_cached_rate_makes_the_turn_unpriced(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    seed_catalog(conn)
    turn(conn, SESSION_A, 1, (1000, 100, 200), "groq", "llama-no-cache")
    turn(conn, SESSION_A, 2, (1000, 100, 0), "groq", "llama-no-cache")
    response, payload = payload_of(client)
    assert payload["unpriced_turns"] == "1"
    assert payload["priced_turns"] == "1"
    assert payload["unpriced_models"] == ["groq/llama-no-cache"]
    # Only the cache-free turn: 1000 x 0.05 + 100 x 0.08 = 58 per million. Neither the input
    # rate nor zero stood in for the missing cached one.
    assert payload["total"] == "0.000058"
    assert runs.spend(conn).total == Decimal("0.000058")
    capture(
        "missing-cached-rate.txt",
        "ticket 133 - a cached_input_tokens > 0 turn on a model with no cached rate\n"
        f"{response.content.decode()}\npriced at a substituted rate: 0 turns\n",
    )


def test_a_non_usd_row_is_not_priced_as_dollars(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO prices VALUES ('openrouter', 'm/eur', '1', '1', NULL, 'EUR', 'x', ?)",
        (FETCHED_AT,),
    )
    turn(conn, SESSION_A, 1, (10, 10, 0), "openrouter", "m/eur")
    found = runs.spend(conn)
    assert (found.total, found.unpriced_turns) == (None, 1)


# ---- AC 5: ?session= scopes the same payload; an unknown id is 404 -------------------------------


def test_session_scopes_the_same_payload_to_one_run_key(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    seed_five(conn)
    response_a, a = payload_of(client, f"{SPEND_PATH}?session={SESSION_A}")
    _, b = payload_of(client, f"{SPEND_PATH}?session={SESSION_B}")
    assert set(a) == PAYLOAD_KEYS
    assert a == {
        "total": "0.012207",
        "currency": "USD",
        "priced_turns": "1",
        "unknown_usage_turns": "1",
        "unknown_model_turns": "0",
        "unpriced_turns": "0",
        "unpriced_models": [],
        "prices_fetched_at": FETCHED_AT,
        **NO_V5,
        "handlers": [
            {
                "handler": "openrouter",
                "kind": runs.HANDLER_CHAT_MODEL,
                "billing": None,
                "turns": "1",
                "cost": "0.012207",
                "api_equivalent": None,
                "not_metered_turns": "0",
                "reason": None,
            },
        ],
    }
    assert (b["priced_turns"], b["unknown_model_turns"], b["unpriced_turns"]) == ("1", "1", "1")
    assert b["total"] == "0.001320"
    missing = client.get(f"{SPEND_PATH}?session={UNKNOWN_SESSION}")
    assert missing.status_code == 404
    assert missing.json()["error"] == "SurfaceNotFound"
    assert UNKNOWN_SESSION in missing.json()["message"]
    capture(
        "session-scope.txt",
        f"ticket 133 - GET /v1/spend?session=\n{SESSION_A}: {response_a.content.decode()}\n"
        f"{UNKNOWN_SESSION}: {missing.status_code} {missing.content.decode()}\n",
    )


def test_the_cli_refuses_an_unknown_session(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harnessed", "spend", "--session", UNKNOWN_SESSION, "--json"])
    with pytest.raises(SystemExit) as exited:
        main()
    captured = capsys.readouterr()
    assert exited.value.code == 1
    assert captured.out == ""
    assert UNKNOWN_SESSION in captured.err


# ---- AC 6: the CLI twin is byte-identical and in the parity set ---------------------------------


def test_the_cli_twin_prints_the_route_bytes_scoped_and_not(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    seed_five(conn)
    lines = []
    for argv, path in (
        (SPEND_ARGV, SPEND_PATH),
        (["spend", "--session", SESSION_B, "--json"], f"{SPEND_PATH}?session={SESSION_B}"),
    ):
        line = cli_line(argv)
        response = client.get(path)
        assert_parity(line, response)
        assert line[:-1] == canonical(response.json()).encode("utf-8")
        lines += [f"cli:  harnessed {' '.join(argv)}", f"  {line[:-1].decode()}"]
        lines += [f"http: GET {path}", f"  {response.content.decode()}", "parity: equal as bytes"]
    capture(
        "parity-diff.txt", "ticket 133 - spend --json against GET /v1/spend\n" + "\n".join(lines)
    )


def test_both_pairs_and_the_refusal_are_in_the_parity_set() -> None:
    pairs = {pair.cli_argv: pair.http_path for pair in PARITY_SET if pair.route == SPEND_PATH}
    assert pairs == {
        ("spend", "--json"): SPEND_PATH,
        ("spend", "--session", "{session_id}", "--json"): f"{SPEND_PATH}?session={{session_id}}",
    }
    assert any(refusal.route == SPEND_PATH for refusal in REFUSAL_SET)


def test_the_prose_form_names_every_count(conn: sqlite3.Connection) -> None:
    seed_five(conn)
    text = prose(["spend"])
    assert FIVE_TOTAL in text
    assert "openrouter/vendor/unlisted" in text
    for label in ("priced turns", "unknown usage", "unknown model", "unpriced turns"):
        assert label in text, label


# ---- the handler: one store call plus its emitter, no arithmetic --------------------------------


def scanned(func: Callable[..., Any]) -> ast.AST:
    """The handler's own body, annotations and decorators lifted off (ticket 126's lesson)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node.returns = None
            node.decorator_list = []
        elif isinstance(node, ast.arg | ast.AnnAssign):
            node.annotation = None  # type: ignore[assignment]
    return tree


def test_the_handler_calls_one_store_callable_plus_its_emitter(tmp_harnessed_home: Path) -> None:
    application = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    (endpoint,) = (
        route.endpoint  # type: ignore[attr-defined]
        for route in application.routes
        if getattr(route, "path", None) == SPEND_PATH
    )
    tree = scanned(endpoint)
    calls = sorted(ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call))
    assert calls == ["runs.spend", "runs.spend_payload"], calls
    arithmetic = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp | ast.AugAssign | ast.UnaryOp | ast.Compare)
    ]
    assert arithmetic == [], "decision 37: the handler computes nothing"


def test_spend_never_names_the_pricing_fetch() -> None:
    """MUST rule 28: spend reads stored rows and never asks a provider."""
    source = RUNS_MODULE.read_text(encoding="utf-8")
    assert "pricing" not in source.split('"""', 2)[2].replace("# ", "")
    assert "urlopen" not in source


# ---- decision 47: the read at the seeded ceiling ------------------------------------------------


def seed_ceiling(conn: sqlite3.Connection) -> None:
    """Fifty thousand turns, about half on priced models, written as rows."""
    for n in range(50):
        price(conn, "openrouter", f"m/{n}", ("3", "15", "0.3" if n % 2 else None))
    turns = (
        (
            f"run_{n:026d}",
            iteration,
            FETCHED_AT,
            100 * iteration,
            10 * iteration,
            n % 3,
            "openrouter",
            f"m/{n % 100}",
        )
        for n in range(CEILING_TURNS // 10)
        for iteration in range(1, 11)
    )
    conn.executemany(
        "INSERT INTO turns (run_id, iteration, request, response, stop_reason, created_at, "
        "input_tokens, output_tokens, cached_input_tokens, provider, model) "
        "VALUES (?, ?, '{}', '', 'end_turn', ?, ?, ?, ?, ?, ?)",
        turns,
    )
    conn.commit()


@pytest.mark.slow
def test_the_read_at_the_ceiling_is_measured(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    seed_ceiling(conn)
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    samples: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        readonly = db.connect_readonly(path)
        try:
            found = runs.spend(readonly)
        finally:
            readonly.close()
        samples.append(time.perf_counter() - started)
    median = statistics.median(samples)
    counted = found.priced_turns + found.unpriced_turns
    assert counted == CEILING_TURNS
    assert found.priced_turns > 0 and found.total is not None
    capture(
        "ceiling-timing.txt",
        f"ticket 133 - runs.spend over {CEILING_TURNS} turns ({found.priced_turns} priced), "
        "five reads each through a fresh connect_readonly\n"
        + "\n".join(f"read {n + 1}: {seconds * 1000:.1f} ms" for n, seconds in enumerate(samples))
        + f"\nmedian: {median * 1000:.1f} ms\ndecision 47 measured: {DECISION_47_MS} ms\n"
        f"NFR: {NFR_SECONDS * 1000:.0f} ms; reopen trigger: {REOPEN_SECONDS * 1000:.0f} ms "
        f"({'OVER, reopen decision 47' if median > REOPEN_SECONDS else 'under'})\n"
        f"measured at {clock.isoformat(clock.now())}\n",
    )
    assert median < NFR_SECONDS, f"median {median * 1000:.1f} ms is over the 200 ms NFR"


# ---- ticket 174 (FR-115): a reported cost counts as a price, and unpriced stays a state ---------

EVIDENCE_174 = REPO_ROOT / "evidence" / "174"
PRICING_MODULE = REPO_ROOT / "src" / "harnessed" / "providers" / "pricing.py"


def capture_174(name: str, text: str) -> Path:
    """Write one ticket-174 evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_174.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_174 / name
    path.write_text(text, encoding="utf-8")
    return path


def decision(conn: sqlite3.Connection, run_key: str, idx: int, cost: str | None) -> None:
    """One ``decisions`` row as ``router/decisions.py`` writes it: ``cost`` the reported figure
    as TEXT and ``cost_nanos`` its integer twin, both null when the router reported none."""
    nanos = None if cost is None else int(Decimal(cost) * 1_000_000_000)
    conn.execute(
        "INSERT INTO decisions (run_id, idx, message_seq, hop, outcome, route, handler, "
        "state_truncated, latency_seconds, input_tokens, output_tokens, cost, cost_nanos, model, "
        "created_at) VALUES (?, ?, 1, 0, 'routed', 'groq', 'groq', 0, '0.4', 900, 40, ?, ?, "
        "'jev/router-1', ?)",
        (run_key, idx, cost, nanos, FETCHED_AT),
    )
    conn.commit()


def agent_turn(
    conn: sqlite3.Connection,
    run_key: str,
    idx: int,
    entry: str,
    billing: str | None,
    cost: tuple[str, str] | None,
    usage: tuple[int, int] | None = (1200, 300),
    model: str | None = None,
) -> None:
    """One ``agent_turns`` row as ``acp/turns.py`` writes it: ``cost`` is ``(figure, cost_kind)``,
    or ``None`` for a turn that is not metered - a null cost and a null kind, never a zero."""
    figure, kind = (None, None) if cost is None else cost
    nanos = None if figure is None else int(Decimal(figure) * 1_000_000_000)
    inputs, outputs = (None, None) if usage is None else usage
    conn.execute(
        "INSERT INTO agent_turns (run_id, idx, message_seq, hop, entry, billing, source, model, "
        "response, input_tokens, output_tokens, cost, cost_nanos, currency, cost_kind, complete, "
        "created_at) VALUES (?, ?, 1, 0, ?, ?, 'acp', ?, 'done', ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            run_key,
            idx,
            entry,
            billing,
            model,
            inputs,
            outputs,
            figure,
            nanos,
            None if figure is None else "USD",
            kind,
            FETCHED_AT,
        ),
    )
    conn.commit()


#: The five buckets of FR-115's seeded session, each figure worked by hand:
#: Groq, 1000 x 0.59 + 200 x 0.79 = 748 per million = 0.000748; the decision reported
#: 0.000020916 (Jev's figure); Pi, an ``openrouter`` bill, reported 0.00001368; Claude Code's
#: ``usage_update.cost`` is 0.0412, an API equivalent; Grok reported nothing.
GROQ_COST = Decimal("0.000748")
DECISION_COST = "0.000020916"
PI_COST = "0.00001368"
CLAUDE_CODE_COST = "0.0412"
FIVE_BUCKETS_TOTAL = (GROQ_COST + Decimal(DECISION_COST) + Decimal(PI_COST)).quantize(
    Decimal("0.000001"), ROUND_HALF_UP
)


def seed_five_buckets(conn: sqlite3.Connection) -> None:
    """FR-115's session in ``SESSION_A``, plus one of each bucket in ``SESSION_B`` so a scoped
    read is seen to drop them."""
    price(conn, "groq", "llama-3.3-70b", ("0.59", "0.79", None))
    turn(conn, SESSION_A, 1, (1000, 200, 0), "groq", "llama-3.3-70b")
    decision(conn, SESSION_A, 0, DECISION_COST)
    agent_turn(conn, SESSION_A, 0, "pi", config.BILLING_OPENROUTER, (PI_COST, "reported"))
    agent_turn(
        conn,
        SESSION_A,
        1,
        "subscription-agent",
        config.BILLING_SUBSCRIPTION,
        (CLAUDE_CODE_COST, "api_equivalent"),
    )
    agent_turn(conn, SESSION_A, 2, "grok", config.BILLING_SUBSCRIPTION, None)
    turn(conn, SESSION_B, 1, (1000, 200, 0), "groq", "llama-3.3-70b")
    decision(conn, SESSION_B, 0, "1")
    agent_turn(conn, SESSION_B, 0, "pi", config.BILLING_OPENROUTER, ("2", "reported"))
    agent_turn(
        conn,
        SESSION_B,
        1,
        "other-subscription-agent",
        config.BILLING_SUBSCRIPTION,
        ("3", "api_equivalent"),
    )
    agent_turn(conn, SESSION_B, 2, "grok", config.BILLING_SUBSCRIPTION, None)


def test_the_five_bucket_session_serves_each_bucket_exactly(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    seed_five_buckets(conn)
    response, payload = payload_of(client, f"{SPEND_PATH}?session={SESSION_A}")
    assert payload == {
        "total": str(FIVE_BUCKETS_TOTAL),
        "currency": "USD",
        "priced_turns": "1",
        "unknown_usage_turns": "0",
        "unknown_model_turns": "0",
        "unpriced_turns": "0",
        "unpriced_models": [],
        "prices_fetched_at": FETCHED_AT,
        "router_cost": "0.000021",
        "router_decisions": "1",
        "agent_cost": "0.000014",
        "api_equivalent": "0.041200",
        "not_metered_turns": "1",
        "handlers": [
            {
                "handler": "groq",
                "kind": runs.HANDLER_CHAT_MODEL,
                "billing": None,
                "turns": "1",
                "cost": "0.000748",
                "api_equivalent": None,
                "not_metered_turns": "0",
                "reason": None,
            },
            {
                "handler": "router",
                "kind": runs.HANDLER_ROUTER,
                "billing": None,
                "turns": "1",
                "cost": "0.000021",
                "api_equivalent": None,
                "not_metered_turns": "0",
                "reason": None,
            },
            {
                "handler": "pi",
                "kind": runs.HANDLER_AGENT,
                "billing": config.BILLING_OPENROUTER,
                "turns": "1",
                "cost": "0.000014",
                "api_equivalent": None,
                "not_metered_turns": "0",
                "reason": None,
            },
            {
                "handler": "subscription-agent",
                "kind": runs.HANDLER_AGENT,
                "billing": config.BILLING_SUBSCRIPTION,
                "turns": "1",
                "cost": None,
                "api_equivalent": "0.041200",
                "not_metered_turns": "0",
                "reason": None,
            },
            {
                "handler": "grok",
                "kind": runs.HANDLER_AGENT,
                "billing": config.BILLING_SUBSCRIPTION,
                "turns": "1",
                "cost": None,
                "api_equivalent": None,
                "not_metered_turns": "1",
                "reason": runs.NO_AGENT_COST_REASON,
            },
        ],
    }
    assert payload["total"] == "0.000783"
    found = runs.spend(conn, SESSION_A)
    assert found.total == FIVE_BUCKETS_TOTAL
    assert found.api_equivalent == Decimal(CLAUDE_CODE_COST)
    # The API equivalent is beside the total, never in it: adding it would read 0.041983.
    assert Decimal(payload["total"]) < Decimal(CLAUDE_CODE_COST)
    # AC1: five handler rows, Grok's not metered with a reason and never a bare "0".
    assert len(payload["handlers"]) == 5
    grok_row = payload["handlers"][-1]
    assert grok_row["handler"] == "grok"
    assert grok_row["not_metered_turns"] == "1" and grok_row["reason"] is not None
    assert grok_row["cost"] is None and grok_row["api_equivalent"] is None
    assert b'"reason":"0"' not in response.content
    capture_174(
        "five-buckets.txt",
        "ticket 174 - GET /v1/spend?session= over one priced Groq turn, one decision, one Pi "
        "turn, one Claude Code turn with usage_update.cost and one Grok turn\n"
        f"{response.content.decode()}\n",
    )
    EVIDENCE_180.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_180 / "handlers.txt").write_text(
        "ticket 180 - GET /v1/spend?session=: the five-row handlers list, in store order\n\n"
        + json.dumps(payload["handlers"], indent=2)
        + "\n",
        encoding="utf-8",
    )


def test_all_time_sums_every_session_and_the_reported_costs_join_the_total(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    seed_five_buckets(conn)
    _, payload = payload_of(client)
    assert payload["router_cost"] == "1.000021"
    assert payload["router_decisions"] == "2"
    assert payload["agent_cost"] == "2.000014"
    assert payload["api_equivalent"] == "3.041200"
    assert payload["not_metered_turns"] == "2"
    exact = 2 * GROQ_COST + 1 + Decimal(DECISION_COST) + 2 + Decimal(PI_COST)
    assert payload["total"] == str(exact.quantize(Decimal("0.000001"), ROUND_HALF_UP))


def test_a_reported_cost_alone_is_a_price_so_the_total_is_not_null(
    conn: sqlite3.Connection,
) -> None:
    """No catalog, no loop turn: a decision's reported figure is still a price (FR-115)."""
    decision(conn, SESSION_A, 0, DECISION_COST)
    payload = runs.spend_payload(runs.spend(conn, SESSION_A))
    assert payload["total"] == "0.000021"
    assert payload["router_cost"] == "0.000021"
    assert payload["prices_fetched_at"] is None


def test_nothing_priced_is_still_null_and_every_new_bucket_null_or_counted(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    decision(conn, SESSION_A, 0, None)
    agent_turn(conn, SESSION_A, 0, "grok", config.BILLING_SUBSCRIPTION, None)
    agent_turn(
        conn,
        SESSION_A,
        1,
        "other-subscription-agent",
        config.BILLING_SUBSCRIPTION,
        (CLAUDE_CODE_COST, "api_equivalent"),
    )
    response, payload = payload_of(client, f"{SPEND_PATH}?session={SESSION_A}")
    assert payload["total"] is None, "an API equivalent alone is not a price"
    assert payload["router_cost"] is None
    assert payload["router_decisions"] == "1"
    assert payload["agent_cost"] is None
    assert payload["api_equivalent"] == "0.041200"
    assert payload["not_metered_turns"] == "1"
    assert b'"0.000000"' not in response.content


def test_no_agent_turn_or_decision_enters_the_total_at_a_computed_rate(
    conn: sqlite3.Connection,
) -> None:
    """A catalog rate for the very model an agent turn and a decision used prices neither: only a
    reported figure is a price, and nothing is multiplied for them (FR-115)."""
    price(conn, "openrouter", "moonshotai/kimi-k2", ("1", "1", None))
    price(conn, "groq", "jev/router-1", ("1", "1", None))
    agent_turn(
        conn,
        SESSION_A,
        0,
        "pi",
        config.BILLING_OPENROUTER,
        None,
        usage=(1_000_000, 1_000_000),
        model="moonshotai/kimi-k2",
    )
    decision(conn, SESSION_A, 0, None)
    found = runs.spend(conn, SESSION_A)
    assert found.total is None, "0 agent turns or decisions priced at a computed rate"
    assert (found.agent_cost, found.router_cost, found.not_metered_turns) == (None, None, 1)
    # providers/pricing.py still holds 0 literal prices: no float and no rate table in it.
    source = PRICING_MODULE.read_text(encoding="utf-8")
    floats = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert floats == []
    assert "PRICING = {" not in source and "PRICES = {" not in source


def test_an_agent_only_session_is_held_not_refused(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    """A routed message that only delegated has no ``turns`` row, and it still cost what it cost."""
    agent_turn(conn, SESSION_A, 0, "pi", config.BILLING_OPENROUTER, (PI_COST, "reported"))
    _, payload = payload_of(client, f"{SPEND_PATH}?session={SESSION_A}")
    assert payload["agent_cost"] == "0.000014"
    assert payload["total"] == "0.000014"
    assert client.get(f"{SPEND_PATH}?session={UNKNOWN_SESSION}").status_code == 404


def test_the_five_bucket_payload_is_byte_identical_on_both_halves(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    seed_five_buckets(conn)
    lines = []
    for argv, path in (
        (SPEND_ARGV, SPEND_PATH),
        (["spend", "--session", SESSION_A, "--json"], f"{SPEND_PATH}?session={SESSION_A}"),
    ):
        line = cli_line(argv)
        response = client.get(path)
        assert_parity(line, response)
        assert line[:-1] == canonical(response.json()).encode("utf-8")
        lines += [f"cli:  harnessed {' '.join(argv)}", f"  {line[:-1].decode()}"]
        lines += [f"http: GET {path}", f"  {response.content.decode()}", "parity: equal as bytes"]
    capture_174(
        "parity-pair.txt",
        "ticket 174 - spend --json against GET /v1/spend, five buckets\n" + "\n".join(lines),
    )


def test_the_prose_form_names_the_new_buckets(conn: sqlite3.Connection) -> None:
    seed_five_buckets(conn)
    text = prose(["spend", "--session", SESSION_A])
    for label in ("router", "agents", "api equivalent", "not metered"):
        assert label in text, label
    assert "0.041200" in text and "not in total" in text


#: Decision 66's seeded database, the one decision 64 measured on: 50,000 turns, 14,098 decisions
#: and 30,000 agent turns.
CEILING_DECISIONS = 14_098
CEILING_AGENT_TURNS = 30_000
DECISION_64_MS = 44.3
CEILING_AGENTS = (
    ("pi", config.BILLING_OPENROUTER, PI_COST, 13_680, "reported"),
    (
        "subscription-agent",
        config.BILLING_SUBSCRIPTION,
        CLAUDE_CODE_COST,
        41_200_000,
        "api_equivalent",
    ),
    ("grok", config.BILLING_SUBSCRIPTION, None, None, None),
)


def seed_decision_66(conn: sqlite3.Connection) -> None:
    seed_ceiling(conn)
    conn.executemany(
        "INSERT INTO decisions (run_id, idx, message_seq, hop, outcome, state_truncated, "
        "latency_seconds, cost, cost_nanos, model, created_at) "
        "VALUES (?, ?, 1, 0, 'routed', 0, '0.4', ?, ?, 'jev/router-1', ?)",
        (
            (f"run_{n % 5000:026d}", n, DECISION_COST, 20_916, FETCHED_AT)
            for n in range(CEILING_DECISIONS)
        ),
    )
    conn.executemany(
        "INSERT INTO agent_turns (run_id, idx, hop, entry, billing, source, response, cost, "
        "cost_nanos, currency, cost_kind, complete, created_at) "
        "VALUES (?, ?, 0, ?, ?, 'acp', 'done', ?, ?, ?, ?, 1, ?)",
        (
            (
                f"run_{n % 5000:026d}",
                n,
                entry,
                billing,
                cost,
                nanos,
                None if cost is None else "USD",
                kind,
                FETCHED_AT,
            )
            for n in range(CEILING_AGENT_TURNS)
            for entry, billing, cost, nanos, kind in (CEILING_AGENTS[n % 3],)
        ),
    )
    conn.commit()


@pytest.mark.slow
def test_all_time_spend_at_the_decision_66_ceiling_is_measured(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    seed_decision_66(conn)
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    samples: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        readonly = db.connect_readonly(path)
        try:
            body = canonical(runs.spend_payload(runs.spend(readonly)))
        finally:
            readonly.close()
        samples.append(time.perf_counter() - started)
    median = statistics.median(samples)
    payload = json.loads(body)
    assert payload["router_decisions"] == str(CEILING_DECISIONS)
    assert payload["not_metered_turns"] == str(CEILING_AGENT_TURNS // 3)
    assert payload["total"] is not None
    capture_174(
        "ceiling-timing.txt",
        f"ticket 174 - all-time /v1/spend read over {CEILING_TURNS} turns, {CEILING_DECISIONS} "
        f"decisions and {CEILING_AGENT_TURNS} agent turns (decision 66's shape), five reads each "
        "through a fresh connect_readonly\n"
        + "\n".join(f"read {n + 1}: {seconds * 1000:.1f} ms" for n, seconds in enumerate(samples))
        + f"\nmedian: {median * 1000:.1f} ms\ndecision 64 measured: {DECISION_64_MS} ms\n"
        f"decision 47 trigger: {REOPEN_SECONDS * 1000:.0f} ms "
        f"({'OVER, reopen decision 47' if median > REOPEN_SECONDS else 'under'})\n"
        f"body: {body}\nmeasured at {clock.isoformat(clock.now())}\n",
    )
    assert median < NFR_SECONDS, f"median {median * 1000:.1f} ms is over the 200 ms NFR"
