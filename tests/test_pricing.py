"""FR-73: prices are fetched once, by an explicit command, stored as decimal strings (ticket 132).

Decision 53 and MUST rule 28. ``harnessed prices refresh`` is the only code path that ever asks a
provider for a price, so the runtime works with no network because it never asks. Five claims,
each a section below:

1. ``011_prices.sql`` creates ``prices`` keyed ``(provider, model)`` with every rate a ``TEXT``
   decimal string: the **declared** types are read back and **0** are ``REAL``.
2. ``refresh`` reads each configured provider's models listing through ``providers/pricing.py``,
   upserts, and names per provider how many models were priced and which publish no catalog.
   Offline it reads the **recorded catalogs** under ``tests/fixtures/catalogs/``, captured from a
   live run on 2026-09-23; the transport is swapped one layer below ``fetch`` so the real URL,
   headers and parse all run. The socket guard is never widened (MUST rules 8, 18).
3. ``pricing.py`` holds **0** literal prices, and its fetch is named by ``cli/prices_cmd.py``
   alone, asserted by AST over the whole package. Both scans are shown able to fire.
4. With no network, a run and a page work exactly as before: nothing on either path asks.
5. ``prices list --json`` and ``GET /v1/prices`` serve the stored rows as strings, byte-identical,
   each row carrying its **own** ``fetched_at``.

The live refresh is ``@pytest.mark.live`` and writes what each provider actually answered to
``evidence/132/``: that run is what settles decision 53's assumption about which catalogs exist.
"""

from __future__ import annotations

import ast
import io
import sqlite3
import urllib.error
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnessed import clock
from harnessed.cli.app import app, main
from harnessed.gateway import create_app
from harnessed.memory import db
from harnessed.providers import pricing
from test_migrations import MIGRATIONS_DIR, apply_through, migrated_through, user_version

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
#: `ux/design.md`'s null reason for an empty `prices` table, the one copy that names the refresh.
REFRESH_REASON = "no prices fetched: run `harnessed prices refresh`"
PRICING_SOURCE = PACKAGE_ROOT / "providers" / "pricing.py"
CATALOGS = Path(__file__).resolve().parent / "fixtures" / "catalogs"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "132"

MIGRATION_011 = "011_prices.sql"

#: The three providers whose listings were recorded, and the key variable each is configured by.
RECORDED = {
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

#: Placeholder credentials, assembled so no literal looks like a key (ticket 008's lesson).
DUMMY_KEY = "placeholder-" + "not-a-key"

cli = CliRunner(mix_stderr=False)


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


def recorded(provider: str) -> bytes:
    return (CATALOGS / f"{provider}.json").read_bytes()


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


# ---- 1. the migration --------------------------------------------------------------------------


def test_011_is_the_eleventh_shipped_migration_and_is_named_prices() -> None:
    """Contiguity assigns the number: 131 took 010, so FR-73's table is 011."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:11]] == list(range(1, 12))
    eleventh = shipped[10]
    assert (eleventh.version, eleventh.slug, eleventh.filename) == (11, "prices", MIGRATION_011)


def test_011_declares_every_column_text_and_0_real(tmp_path: Path) -> None:
    """AC 1: the declared types, read back from the table SQLite built, and 0 ``REAL``."""
    conn, partial = migrated_through(tmp_path, 10)
    try:
        assert user_version(conn) == 10
        report = apply_through(conn, partial, 11)
        assert report.applied == (11,)
        assert user_version(conn) == 11
        info = conn.execute("PRAGMA table_info(prices)").fetchall()
    finally:
        conn.close()
    declared = {str(row[1]): str(row[2]) for row in info}
    not_null = {str(row[1]): bool(row[3]) for row in info}
    key = {str(row[1]): int(row[5]) for row in info if int(row[5])}
    assert declared == {
        "provider": "TEXT",
        "model": "TEXT",
        "input": "TEXT",
        "output": "TEXT",
        "cached_input": "TEXT",
        "currency": "TEXT",
        "source_url": "TEXT",
        "fetched_at": "TEXT",
    }
    real = [name for name, kind in declared.items() if "REAL" in kind.upper()]
    assert real == []
    assert key == {"provider": 1, "model": 2}, "keyed (provider, model), in that order"
    assert not_null == {name: name != "cached_input" for name in declared}
    capture(
        "declared-types.txt",
        "ticket 132 - PRAGMA table_info(prices) after 011_prices\n"
        + "\n".join(
            f"{name:<13} {kind:<5} {'NULL' if not not_null[name] else 'NOT NULL'}"
            f"{'  pk ' + str(key[name]) if name in key else ''}"
            for name, kind in declared.items()
        )
        + f"\nREAL columns: {len(real)}\n"
        + "PRAGMA user_version before: 10\nPRAGMA user_version after: 11\n",
    )


def test_011_leaves_every_earlier_table_untouched(tmp_path: Path) -> None:
    """Forward-only: 011 adds one table and moves no column of any other."""
    conn, partial = migrated_through(tmp_path, 10)
    try:
        before = {
            name: conn.execute(f"PRAGMA table_info({name})").fetchall()
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        apply_through(conn, partial, 11)
        for name, columns in before.items():
            assert conn.execute(f"PRAGMA table_info({name})").fetchall() == columns, name
    finally:
        conn.close()
    text = (MIGRATIONS_DIR / MIGRATION_011).read_text(encoding="utf-8")
    statements = " ".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("--")
    ).upper()
    for forbidden in ("REAL", "FLOAT", "DOUBLE", "NUMERIC", "DROP", "ALTER"):
        assert forbidden not in statements, forbidden


# ---- 2. the catalog, parsed from what the providers recorded ------------------------------------


def parsed(provider: str) -> pricing.Catalog:
    return pricing.parse(
        provider,
        recorded(provider),
        source_url=pricing.catalog_url(provider),
        fetched_at=clock.isoformat(clock.now()),
    )


def test_openrouter_rates_become_per_million_decimal_strings() -> None:
    """Per-token strings in, per-million strings out, and not one float on the way."""
    catalog = parsed("openrouter")
    by_model = {price.model: price for price in catalog.prices}
    assert catalog.publishes
    assert catalog.listed == 5
    assert set(by_model) == {
        "anthropic/claude-sonnet-4.5",
        "openai/gpt-oss-120b",
        "deepseek/deepseek-chat-v3-0324",
        "stealth/space-bunny-alpha",
    }
    gpt = by_model["openai/gpt-oss-120b"]
    assert (gpt.input, gpt.output, gpt.cached_input) == ("0.15", "0.6", "0.075")
    claude = by_model["anthropic/claude-sonnet-4.5"]
    assert (claude.input, claude.output, claude.cached_input) == ("3", "15", "0.3")
    assert by_model["deepseek/deepseek-chat-v3-0324"].cached_input is None
    free = by_model["stealth/space-bunny-alpha"]
    assert (free.input, free.output) == ("0", "0"), "a published zero is a price, not a gap"
    for price in catalog.prices:
        assert price.provider == "openrouter"
        assert price.currency == pricing.CURRENCY == "USD"
        assert price.source_url == "https://openrouter.ai/api/v1/models"
        for rate in (price.input, price.output, price.cached_input):
            assert rate is None or isinstance(rate, str)


def test_every_stored_rate_is_the_published_rate_exactly() -> None:
    """``Decimal`` round trip: the stored string over a million is the catalog's own string."""
    import json

    listing = json.loads(recorded("openrouter"))["data"]
    published = {entry["id"]: entry["pricing"] for entry in listing}
    for price in parsed("openrouter").prices:
        rates = published[price.model]
        assert Decimal(price.input) / pricing.TOKENS_PER_MILLION == Decimal(rates["prompt"])
        assert Decimal(price.output) / pricing.TOKENS_PER_MILLION == Decimal(rates["completion"])


def test_a_variable_price_is_left_unpriced_rather_than_stored_negative() -> None:
    """OpenRouter's router models publish ``-1``: that is "it depends", never a rate."""
    catalog = parsed("openrouter")
    assert "openrouter/auto" not in {price.model for price in catalog.prices}
    assert catalog.unpriced == 1


def test_groq_publishes_a_catalog_and_a_model_without_an_output_rate_is_unpriced() -> None:
    """The live run of 2026-09-23 found Groq's listing carries ``pricing`` (decision 53 assumed
    it did not). Speech models with no ``completion`` rate stay unpriced rather than half-priced."""
    catalog = parsed("groq")
    assert catalog.publishes
    assert catalog.listed == 11
    assert len(catalog.prices) == 6
    assert catalog.unpriced == 5
    assert {"whisper-large-v3", "canopylabs/orpheus-v1-english"}.isdisjoint(
        price.model for price in catalog.prices
    )
    gpt = next(price for price in catalog.prices if price.model == "openai/gpt-oss-20b")
    assert (gpt.input, gpt.output, gpt.cached_input) == ("0.075", "0.3", "0.0375")


def test_deepseek_publishes_no_catalog() -> None:
    catalog = parsed("deepseek")
    assert not catalog.publishes
    assert catalog.listed == 2
    assert catalog.prices == ()


def test_a_body_that_is_not_a_models_listing_is_a_provider_error() -> None:
    with pytest.raises(pricing.ProviderError, match="groq"):
        pricing.parse("groq", b'{"error": "nope"}', source_url="u", fetched_at="t")


# ---- 2. the command, offline, over the recorded catalogs ----------------------------------------


class RecordedTransport:
    """Stands in for ``urlopen`` one layer below ``fetch``: the URL and headers are the real ones.

    Serves each provider's recorded listing by the URL ``fetch`` built, records what was asked,
    and refuses any URL it holds no recording for, so a stray request fails loudly.
    """

    def __init__(self, refuse: frozenset[str] = frozenset()) -> None:
        self.asked: list[tuple[str, dict[str, str]]] = []
        self.refuse = refuse
        self.by_url = {pricing.catalog_url(name): name for name in RECORDED}

    def __call__(self, request: Any, timeout: float) -> io.BytesIO:
        url = request.full_url
        self.asked.append((url, {k.lower(): v for k, v in request.header_items()}))
        provider = self.by_url[url]
        if provider in self.refuse:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)  # type: ignore[arg-type]
        assert timeout == pricing.FETCH_TIMEOUT_S
        return io.BytesIO(recorded(provider))


@pytest.fixture
def configured(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> RecordedTransport:
    """The three recorded providers configured, nothing else, and the transport swapped.

    ``chdir`` so a developer's own ``.env`` cannot configure a fourth (ticket 039's lesson).
    """
    monkeypatch.chdir(tmp_path)
    for var in RECORDED.values():
        monkeypatch.setenv(var, DUMMY_KEY)
    transport = RecordedTransport()
    monkeypatch.setattr(pricing, "urlopen", transport)
    return transport


def stored_rows(home: Path) -> list[tuple[str, ...]]:
    conn = db.connect_readonly(home / db.DATABASE_FILENAME)
    try:
        return [tuple(row) for row in conn.execute("SELECT * FROM prices ORDER BY 1, 2")]
    finally:
        conn.close()


def test_refresh_reads_each_configured_catalog_upserts_and_names_each_provider(
    configured: RecordedTransport, tmp_harnessed_home: Path, frozen_clock: object
) -> None:
    result = cli.invoke(app, ["prices", "refresh"])
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert {url for url, _ in configured.asked} == {pricing.catalog_url(name) for name in RECORDED}
    for _, headers in configured.asked:
        assert headers["authorization"] == f"Bearer {DUMMY_KEY}"
    assert DUMMY_KEY not in out
    line = {
        name: next(x for x in out.splitlines() if x.strip().startswith(name)) for name in RECORDED
    }
    assert "4 models priced" in line["openrouter"]
    assert "6 models priced" in line["groq"]
    assert "publishes no catalog" in line["deepseek"]
    assert "anthropic" in out and "openai" in out, "the unconfigured providers are named"
    rows = stored_rows(tmp_harnessed_home)
    assert len(rows) == 10
    assert {row[0] for row in rows} == {"openrouter", "groq"}
    assert all(isinstance(value, str) for row in rows for value in row if value is not None)
    capture("refresh-offline.txt", "$ harnessed prices refresh   (recorded catalogs)\n" + out)


def test_refresh_is_an_upsert_so_a_second_run_moves_fetched_at_and_adds_no_row(
    configured: RecordedTransport, tmp_harnessed_home: Path, frozen_clock: object
) -> None:
    assert cli.invoke(app, ["prices", "refresh"]).exit_code == 0
    first = stored_rows(tmp_harnessed_home)
    clock.freeze(clock.now() + timedelta(hours=1))
    assert cli.invoke(app, ["prices", "refresh"]).exit_code == 0
    second = stored_rows(tmp_harnessed_home)
    assert len(second) == len(first) == 10
    assert {row[:7] for row in second} == {row[:7] for row in first}
    assert {row[7] for row in second} == {clock.isoformat(clock.now())}


def test_refresh_with_provider_asks_that_provider_only(
    configured: RecordedTransport, tmp_harnessed_home: Path
) -> None:
    result = cli.invoke(app, ["prices", "refresh", "--provider", "groq"])
    assert result.exit_code == 0, result.stderr
    assert [url for url, _ in configured.asked] == [pricing.catalog_url("groq")]
    assert {row[0] for row in stored_rows(tmp_harnessed_home)} == {"groq"}


def test_a_refused_catalog_is_named_and_the_others_are_still_stored(
    configured: RecordedTransport,
    tmp_harnessed_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configured.refuse = frozenset({"groq"})
    with pytest.raises(SystemExit) as exited:
        _main(["prices", "refresh"])
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert "groq" in err and "401" in err
    assert {row[0] for row in stored_rows(tmp_harnessed_home)} == {"openrouter"}


def test_refresh_with_an_unconfigured_provider_names_its_key_variable(
    configured: RecordedTransport, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        _main(["prices", "refresh", "--provider", "anthropic"])
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert configured.asked == []


def _main(argv: list[str]) -> None:
    import sys

    saved = sys.argv
    sys.argv = ["harnessed", *argv]
    try:
        main()
    finally:
        sys.argv = saved


# ---- 3. static: 0 literal prices, and one caller of the fetch -----------------------------------


def literal_prices(source: str) -> list[str]:
    """Every constant in ``source`` that could be a price: a float anywhere, a string that reads as
    a decimal number, or any number inside a dict, tuple, list or set literal (Waku's shape)."""
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, float):
                found.append(f"line {node.lineno}: float {value!r}")
            elif isinstance(value, str) and _reads_as_number(value):
                found.append(f"line {node.lineno}: numeric string {value!r}")
        elif isinstance(node, ast.Dict | ast.Tuple | ast.List | ast.Set):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and type(inner.value) in (int, float):
                    found.append(f"line {inner.lineno}: {inner.value!r} in a literal collection")
    return found


def _reads_as_number(text: str) -> bool:
    try:
        return Decimal(text.strip()).is_finite() and bool(text.strip())
    except ArithmeticError:
        return False


WAKU = 'PRICING = {"anthropic": (3.0, 15.0), "groq": ("0.59", "0.79"), "x": [1, 2]}\n'


def test_pricing_holds_0_literal_prices() -> None:
    found = literal_prices(PRICING_SOURCE.read_text(encoding="utf-8"))
    capture(
        "literal-prices.txt",
        f"providers/pricing.py literal prices: {len(found)}\n" + "\n".join(found),
    )
    assert found == []


def test_the_literal_price_scan_fires_on_the_rejected_shape() -> None:
    """Not vacuous: Waku's table, and its string and int cousins, are each caught."""
    found = literal_prices(WAKU)
    assert len(found) >= 5
    assert any("3.0" in item for item in found)
    assert any("'0.59'" in item for item in found)


def modules_naming_the_fetch(root: Path) -> set[str]:
    """Every module under ``root`` that names ``pricing.fetch``, by import or by attribute."""
    namers: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative == "providers/pricing.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported = (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").endswith("providers.pricing")
                and any(alias.name == "fetch" for alias in node.names)
            )
            attribute = (
                isinstance(node, ast.Attribute)
                and node.attr == "fetch"
                and isinstance(node.value, ast.Name)
                and node.value.id == "pricing"
            )
            if imported or attribute:
                namers.add(relative)
    return namers


def test_the_fetch_is_named_by_cli_prices_cmd_only() -> None:
    namers = modules_naming_the_fetch(PACKAGE_ROOT)
    assert namers == {"cli/prices_cmd.py"}
    for area in ("loop/", "gateway/", "control/"):
        assert not any(name.startswith(area) for name in namers), area
    urlopen_modules = sorted(
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if "urlopen" in path.read_text(encoding="utf-8")
    )
    # v5 adds the Decider seam beside this one (decision 57, ticket 153): `providers/jev.py` is
    # the only other module under `src/` permitted to open a URL. `tests/test_boundaries.py`
    # carries the AST version of this same scan, extended for both modules.
    assert urlopen_modules == [
        "providers/jev.py",
        "providers/pricing.py",
    ], "outbound HTTP is quarantined to pricing.py and jev.py"
    # The one sanctioned mention is `ux/design.md`'s null reason for `spend.total`, which tells the
    # operator to run the command (FR-74, ticket 133). Naming it in copy is not asking; any other
    # mention in a dashboard source still fails.
    assert dashboard_refresh_hits(REPO_ROOT) == []
    capture(
        "import-scan.txt",
        "modules naming pricing.fetch: " + ", ".join(sorted(namers)) + "\n"
        "modules calling urlopen: " + ", ".join(urlopen_modules) + "\n"
        "dashboard sources naming the refresh: 0\n",
    )


def dashboard_refresh_hits(repo: Path) -> list[str]:
    """Dashboard sources naming ``prices refresh`` once only ``REFRESH_REASON`` is stripped."""
    return sorted(
        path.relative_to(repo).as_posix()
        for folder in ("app", "components", "lib")
        for path in (repo / "dashboard" / folder).rglob("*.ts*")
        if "prices refresh" in path.read_text(encoding="utf-8").replace(REFRESH_REASON, "")
    )


def test_the_refresh_scan_strips_only_the_ratified_sentence_and_fires_on_a_second_mention(
    tmp_path: Path,
) -> None:
    (tmp_path / "dashboard" / "lib").mkdir(parents=True)
    (tmp_path / "dashboard" / "components").mkdir()
    (tmp_path / "dashboard" / "app").mkdir()
    (tmp_path / "dashboard" / "lib" / "null-reasons.ts").write_text(
        f'export const NO_PRICES = "{REFRESH_REASON}";\n', encoding="utf-8"
    )
    assert dashboard_refresh_hits(tmp_path) == []
    (tmp_path / "dashboard" / "components" / "Seeded.tsx").write_text(
        f'const a = "{REFRESH_REASON}"; const b = "then prices refresh again";\n',
        encoding="utf-8",
    )
    assert dashboard_refresh_hits(tmp_path) == ["dashboard/components/Seeded.tsx"]


def test_the_fetch_scan_fires_on_a_gateway_that_asks(tmp_path: Path) -> None:
    root = tmp_path / "harnessed"
    (root / "gateway" / "routes").mkdir(parents=True)
    (root / "loop").mkdir()
    (root / "gateway" / "routes" / "prices.py").write_text(
        "from harnessed.providers import pricing\n\ndef h(s):\n    return pricing.fetch(s, 'x')\n",
        encoding="utf-8",
    )
    (root / "loop" / "engine.py").write_text(
        "from harnessed.providers.pricing import fetch\n", encoding="utf-8"
    )
    assert modules_naming_the_fetch(root) == {"gateway/routes/prices.py", "loop/engine.py"}


# ---- 4. no network: nothing at runtime asks -----------------------------------------------------


def test_a_run_and_the_prices_page_work_with_no_network_and_never_ask(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the socket guard, unwidened. ``fetch`` is booby-trapped: any caller fails the test."""

    def asked(*_: object, **__: object) -> None:
        raise AssertionError("something at runtime asked a provider for a price")

    monkeypatch.setattr(pricing, "fetch", asked)
    run = cli.invoke(app, ["run", "ping", "--json"])
    assert run.exit_code == 0, run.stderr
    listed = cli.invoke(app, ["prices", "list", "--json"])
    assert listed.exit_code == 0, listed.stderr
    assert listed.stdout == '{"prices":[]}\n'
    with TestClient(create_app(tmp_harnessed_home / db.DATABASE_FILENAME)) as client:
        response = client.get("/v1/prices")
    assert response.status_code == 200
    assert response.content == b'{"prices":[]}'


# ---- 5. prices list --json and GET /v1/prices ---------------------------------------------------


def seed(conn: sqlite3.Connection, provider: str) -> pricing.Catalog:
    catalog = parsed(provider)
    db.write(conn, pricing.upserts(catalog))
    return catalog


def test_list_and_the_route_serve_the_stored_rows_as_strings_byte_identical(
    clean_env: None, conn: sqlite3.Connection, tmp_harnessed_home: Path, frozen_clock: object
) -> None:
    seed(conn, "openrouter")
    groq_at = clock.now() + timedelta(days=3)
    clock.freeze(groq_at)
    seed(conn, "groq")
    clock.freeze(groq_at + timedelta(days=30))  # the reader's now, which no row may carry

    listed = cli.invoke(app, ["prices", "list", "--json"])
    assert listed.exit_code == 0, listed.stderr
    with TestClient(create_app(tmp_harnessed_home / db.DATABASE_FILENAME)) as client:
        body = client.get("/v1/prices").content
    assert listed.stdout.encode("utf-8") == body + b"\n"

    import json

    rows = json.loads(body)["prices"]
    assert len(rows) == 10
    for row in rows:
        assert set(row) == {
            "provider",
            "model",
            "input",
            "output",
            "cached_input",
            "currency",
            "source_url",
            "fetched_at",
        }
        for key, value in row.items():
            assert isinstance(value, str) or (key == "cached_input" and value is None), (key, row)
    stamps = {row["provider"]: row["fetched_at"] for row in rows}
    assert stamps["groq"] == clock.isoformat(groq_at)
    assert stamps["openrouter"] != stamps["groq"], "each row carries its own fetched_at"
    assert clock.isoformat(clock.now()) not in stamps.values(), "never the reader's clock"
    assert pricing.json_payload(pricing.stored(conn)) == json.loads(body)
    capture(
        "prices-parity.txt",
        "$ harnessed prices list --json\n"
        + listed.stdout
        + "GET /v1/prices\n"
        + body.decode("utf-8")
        + "\nparity: equal as bytes\n",
    )


def test_list_without_the_flag_labels_each_row_with_its_own_fetched_at(
    clean_env: None, conn: sqlite3.Connection, frozen_clock: object
) -> None:
    seed(conn, "groq")
    result = cli.invoke(app, ["prices", "list"])
    assert result.exit_code == 0, result.stderr
    assert "per million tokens" in result.stdout
    assert f"fetched_at {clock.isoformat(clock.now())}" in result.stdout
    empty_home_result = cli.invoke(app, ["prices", "list"])
    assert empty_home_result.exit_code == 0


# ---- the live run: which catalogs exist (decision 53, MUST rule 12) -----------------------------


@pytest.mark.live
def test_live_refresh_records_what_each_provider_answered(tmp_harnessed_home: Path) -> None:
    """Reads the operator's own configuration. Names every provider either way."""
    result = cli.invoke(app, ["prices", "refresh"])
    capture(
        "refresh-live.txt",
        f"$ harnessed prices refresh   (live, exit {result.exit_code})\n"
        + result.stdout
        + (f"stderr:\n{result.stderr}" if result.stderr else ""),
    )
    assert result.exit_code == 0, result.stderr
    assert "models priced" in result.stdout
