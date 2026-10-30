"""FR-10: one SQLite file, its migrations and the three memory stores.

Everything here runs against a throwaway database under a temporary ``HARNESSED_HOME``. The
migration-failure cases build their own migration directory rather than editing a committed
file, because editing a committed migration is exactly the thing the checksum forbids.
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnessed import clock
from harnessed.cli.app import app
from harnessed.errors import MigrationChecksumError, MigrationError, StorageError
from harnessed.events.bus import EventBus
from harnessed.gateway import create_app
from harnessed.memory import db, episodic, procedural, semantic, totals

# The migration set the architecture fixes for v1 (open question 9). A further file is a spec
# change, not a build decision, so the set is asserted rather than discovered - and this
# assertion is what made ticket 047's fifth migration go through the architecture document
# instead of arriving quietly with the code that needed it.
EXPECTED_MIGRATIONS = (
    (1, "init"),
    (2, "memory"),
    (3, "ledgers"),
    (4, "flywheel"),
    (5, "fact_metadata"),
    (6, "turn_usage"),
    (7, "turn_requested_at"),
    (8, "agent_probes"),
    (9, "event_indexes"),
    (10, "turn_provider_model"),
    (11, "prices"),
    (12, "run_messages"),
    (13, "router_probes"),
    (14, "decisions"),
    (15, "agent_turns"),
    (16, "outputs"),
    (17, "eval_retries"),
    (18, "hop_results"),
    (19, "permissions"),
    (20, "tasks"),
    (21, "repo_records"),
    (22, "repo_dirty_paths"),
    (23, "consolidation"),
    (24, "fact_link_relation"),
    (25, "permission_exchange"),
    (26, "v6_debts"),
    (27, "schedule_backends"),
    (28, "consolidation_diagnostics"),
)


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database file under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


def write_migrations(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (directory / name).write_text(body, encoding="utf-8")
    return directory


# ---- the migration set -------------------------------------------------------------------------


def test_the_committed_migration_set_is_the_one_the_architecture_fixes() -> None:
    loaded = db.load_migrations()
    assert tuple((m.version, m.slug) for m in loaded) == EXPECTED_MIGRATIONS


def test_a_fresh_database_applies_every_migration_in_order(tmp_harnessed_home: Path) -> None:
    raw = sqlite3.connect(db.database_path())
    try:
        report = db.migrate(raw)
    finally:
        raw.close()
    assert report.applied == tuple(v for v, _ in EXPECTED_MIGRATIONS)
    assert report.version == max(report.applied)
    assert report.version == max(m.version for m in db.load_migrations())


def test_migrating_an_already_migrated_database_applies_nothing(conn: sqlite3.Connection) -> None:
    before = conn.execute("SELECT version, checksum, applied_at FROM schema_migrations").fetchall()
    report = db.migrate(conn)
    after = conn.execute("SELECT version, checksum, applied_at FROM schema_migrations").fetchall()
    assert report.applied == ()
    assert report.version == EXPECTED_MIGRATIONS[-1][0]
    assert after == before


def test_schema_migrations_records_slug_checksum_and_time(conn: sqlite3.Connection) -> None:
    rows = {
        version: (slug, checksum, applied_at)
        for version, slug, checksum, applied_at in conn.execute(
            "SELECT version, slug, checksum, applied_at FROM schema_migrations"
        )
    }
    for migration in db.load_migrations():
        slug, checksum, applied_at = rows[migration.version]
        assert slug == migration.slug
        assert checksum == migration.checksum
        assert clock.parse(applied_at).tzinfo is not None


def test_user_version_equals_the_highest_applied_version(conn: sqlite3.Connection) -> None:
    (user_version,) = conn.execute("PRAGMA user_version").fetchone()
    assert user_version == db.schema_version(conn) == EXPECTED_MIGRATIONS[-1][0]


def test_every_planned_table_exists(conn: sqlite3.Connection) -> None:
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    assert {
        "schema_migrations",
        "events",
        "mem_semantic",
        "mem_semantic_fts",
        "mem_episodic",
        "procedures",
        "turns",
        "tool_results",
        "eval_runs",
        "eval_results",
        "improvement_runs",
        "improvement_steps",
        "delegations",
        "candidates",
        "promotions",
        "active_procedures",
    } <= names


# ---- pragmas -----------------------------------------------------------------------------------


def test_connect_opens_the_one_file_under_harnessed_home(tmp_harnessed_home: Path) -> None:
    connection = db.connect()
    try:
        assert db.database_path() == tmp_harnessed_home / "harness.db"
        assert db.database_path().exists()
    finally:
        connection.close()


def test_connect_sets_wal_and_the_three_other_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_connect_creates_the_home_directory_when_absent(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "missing" / ".harnessed"
    monkeypatch.setenv("HARNESSED_HOME", str(home))
    connection = db.connect()
    try:
        assert (home / "harness.db").exists()
    finally:
        connection.close()


# ---- checksums ---------------------------------------------------------------------------------


def test_checksums_agree_across_windows_and_posix_checkouts() -> None:
    posix = "CREATE TABLE t (a TEXT);\nCREATE INDEX i ON t (a);\n"
    windows = "CREATE TABLE t (a TEXT);   \r\nCREATE INDEX i ON t (a);\t\r\n"
    assert db.checksum(posix) == db.checksum(windows)


def test_a_checksum_is_sensitive_to_a_real_edit() -> None:
    assert db.checksum("CREATE TABLE t (a TEXT);") != db.checksum("CREATE TABLE t (b TEXT);")


def test_editing_a_committed_migration_raises_naming_both_checksums(tmp_path: Path) -> None:
    source = write_migrations(
        tmp_path / "migrations",
        {
            "001_init.sql": db.load_migrations()[0].sql,
            "002_extra.sql": "CREATE TABLE widgets (widget_id TEXT PRIMARY KEY);\n",
        },
    )
    raw = sqlite3.connect(tmp_path / "edited.db")
    try:
        first = db.migrate(raw, source=source)
        assert first.applied == (1, 2)
        recorded = first.migrations[1].checksum
        (source / "002_extra.sql").write_text(
            "CREATE TABLE widgets (widget_id TEXT PRIMARY KEY, name TEXT);\n", encoding="utf-8"
        )
        computed = db.checksum((source / "002_extra.sql").read_text(encoding="utf-8"))
        with pytest.raises(MigrationChecksumError) as caught:
            db.migrate(raw, source=source)
    finally:
        raw.close()
    message = str(caught.value)
    assert "002_extra.sql" in message
    assert recorded in message
    assert computed in message
    assert recorded != computed


# ---- a malformed migration set fails at load -----------------------------------------------------


@pytest.mark.parametrize(
    ("case", "files"),
    [
        (
            "gap",
            {
                "001_init.sql": "CREATE TABLE a (a TEXT);\n",
                "003_late.sql": "CREATE TABLE c (c TEXT);\n",
            },
        ),
        (
            "duplicate",
            {
                "001_init.sql": "CREATE TABLE a (a TEXT);\n",
                "001_again.sql": "CREATE TABLE b (b TEXT);\n",
            },
        ),
        ("does not start at one", {"002_second.sql": "CREATE TABLE b (b TEXT);\n"}),
        (
            "unnumbered filename",
            {
                "001_init.sql": "CREATE TABLE a (a TEXT);\n",
                "notes.sql": "CREATE TABLE n (n TEXT);\n",
            },
        ),
        ("empty set", {}),
    ],
)
def test_a_malformed_migration_set_raises_at_load(
    tmp_path: Path, case: str, files: dict[str, str]
) -> None:
    source = write_migrations(tmp_path / case.replace(" ", "_"), files)
    with pytest.raises(MigrationError):
        db.load_migrations(source)


def test_a_missing_migration_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(MigrationError):
        db.load_migrations(tmp_path / "nowhere")


def test_a_migration_that_will_not_execute_raises_storage_error(tmp_path: Path) -> None:
    source = write_migrations(tmp_path / "broken", {"001_init.sql": "CREATE TABL oops (;\n"})
    raw = sqlite3.connect(tmp_path / "broken.db")
    try:
        with pytest.raises(StorageError):
            db.migrate(raw, source=source)
        # The failed migration rolled back: nothing was recorded.
        assert db.schema_version(raw) == 0
    finally:
        raw.close()


# ---- the three memory kinds round trip -----------------------------------------------------------


def test_semantic_memory_round_trips(conn: sqlite3.Connection, frozen_clock: object) -> None:
    written = semantic.put(conn, text="the harness stores one file", source="ticket-007")
    assert semantic.get(conn, written.mem_id) == written
    assert written.mem_id.startswith("mem_")
    assert written.created_at == clock.isoformat(clock.now())


def test_semantic_search_finds_a_written_fact(conn: sqlite3.Connection) -> None:
    hit = semantic.put(conn, text="WAL keeps the reader and the writer apart", source="arch")
    semantic.put(conn, text="an unrelated fact about tomatoes", source="arch")
    found = semantic.search(conn, "reader")
    assert [fact.mem_id for fact in found] == [hit.mem_id]


def test_semantic_search_returns_nothing_for_a_miss(conn: sqlite3.Connection) -> None:
    semantic.put(conn, text="one fact", source="arch")
    assert semantic.search(conn, "absent") == ()


def test_semantic_get_returns_none_for_an_unknown_id(conn: sqlite3.Connection) -> None:
    assert semantic.get(conn, "mem_nope") is None


def test_episodic_memory_round_trips(conn: sqlite3.Connection, frozen_clock: object) -> None:
    written = episodic.put(
        conn, run_id="run_0001", kind="run", text="iteration 1: called echo, got 'hi'"
    )
    assert episodic.get(conn, written.episode_id) == written
    assert episodic.for_run(conn, "run_0001") == (written,)
    assert written.created_at == clock.isoformat(clock.now())


def test_episodic_for_run_is_ordered_by_insertion(conn: sqlite3.Connection) -> None:
    first = episodic.put(conn, run_id="run_0002", kind="run", text="first")
    second = episodic.put(conn, run_id="run_0002", kind="run", text="second")
    episodic.put(conn, run_id="run_0003", kind="delegation", text="other run")
    assert [e.episode_id for e in episodic.for_run(conn, "run_0002")] == [
        first.episode_id,
        second.episode_id,
    ]


def test_episodic_get_returns_none_for_an_unknown_id(conn: sqlite3.Connection) -> None:
    assert episodic.get(conn, "epi_nope") is None


def test_episodic_for_session_tells_absent_from_empty(conn: sqlite3.Connection) -> None:
    """Ticket 076: ``None`` is "no such session", ``()`` is "held nothing", rows are rows.

    A session id is minted where its first event is published, so the existence probe is over
    ``events``. One test holds all three answers so none can silently become another.
    """
    assert episodic.for_session(conn, "run_00000000000000000000000000") is None
    silent = EventBus(conn=conn)
    silent.publish("run.started", {"provider": "fake"})
    assert episodic.for_session(conn, silent.run_id) == ()
    assert episodic.for_run(conn, silent.run_id) == ()
    spoken = EventBus(conn=conn)
    spoken.publish("run.started", {"provider": "fake"})
    first = episodic.put(conn, run_id=spoken.run_id, kind="run", text="first")
    second = episodic.put(conn, run_id=spoken.run_id, kind="run", text="second")
    assert episodic.for_session(conn, spoken.run_id) == (first, second)
    assert episodic.for_session(conn, spoken.run_id) == episodic.for_run(conn, spoken.run_id)


def test_episodic_for_session_does_not_treat_unheld_episodes_as_a_session(
    conn: sqlite3.Connection,
) -> None:
    """A ``mem_episodic`` row under an id no event was published for is not a held session."""
    orphan = episodic.put(conn, run_id="run_orphan_0000000000000000000", kind="run", text="x")
    assert episodic.for_run(conn, orphan.run_id) == (orphan,)
    assert episodic.for_session(conn, orphan.run_id) is None


def test_procedural_memory_round_trips(conn: sqlite3.Connection, frozen_clock: object) -> None:
    written = procedural.put(conn, text="Answer with JSON only.", source="ticket-007")
    assert procedural.get(conn, written.proc_id) == written
    assert written.created_at == clock.isoformat(clock.now())


def test_a_procedure_id_is_content_addressed_and_rewriting_it_is_a_no_op(
    conn: sqlite3.Connection,
) -> None:
    first = procedural.put(conn, text="Answer with JSON only.", source="a")
    again = procedural.put(conn, text="Answer with JSON only.", source="b")
    other = procedural.put(conn, text="Answer with prose.", source="a")
    assert first.proc_id == again.proc_id != other.proc_id
    # Never deleted, never overwritten: the first write is the one that stands.
    assert procedural.get(conn, first.proc_id) == first
    assert len(procedural.all(conn)) == 2


def test_procedural_get_returns_none_for_an_unknown_id(conn: sqlite3.Connection) -> None:
    assert procedural.get(conn, "proc_nope") is None


def test_every_store_survives_a_reopen(tmp_harnessed_home: Path) -> None:
    first = db.connect()
    try:
        fact = semantic.put(first, text="durable", source="arch")
        episode = episodic.put(first, run_id="run_0004", kind="run", text="durable")
        procedure = procedural.put(first, text="durable", source="arch")
    finally:
        first.close()
    second = db.connect()
    try:
        assert semantic.get(second, fact.mem_id) == fact
        assert episodic.get(second, episode.episode_id) == episode
        assert procedural.get(second, procedure.proc_id) == procedure
    finally:
        second.close()


# ---- a fact's status, tags and links (ticket 047) -----------------------------------------------


def test_a_fact_defaults_to_active_with_no_tags_and_no_links(conn: sqlite3.Connection) -> None:
    """The metadata is additive: a caller that names none of it writes what it always wrote."""
    fact = semantic.put(conn, "the ingest service listens on port 8471", source="test")
    assert fact.status == semantic.ACTIVE
    assert fact.tags == ()
    assert fact.links == ()
    assert semantic.get(conn, fact.mem_id) == fact


def test_tags_and_links_round_trip(conn: sqlite3.Connection) -> None:
    target = semantic.put(conn, "the platform repository deploys the ingest service", source="t")
    fact = semantic.put(
        conn,
        "the ingest service listens on port 8471",
        source="t",
        tags=("infra", "ports"),
        links=(target.mem_id,),
    )
    stored = semantic.get(conn, fact.mem_id)
    assert stored is not None
    assert stored.tags == ("infra", "ports")
    assert stored.links == (target.mem_id,)


def test_a_link_to_a_fact_that_does_not_exist_is_refused(conn: sqlite3.Connection) -> None:
    """A dangling link is harmless in a vault and is a retrieval bug here.

    Expansion would follow it and find nothing, and nothing is indistinguishable from a fact that
    simply did not match - so the store refuses the write rather than leaving a link that reads
    as a connection and behaves as an absence.
    """
    with pytest.raises(StorageError):
        semantic.put(conn, "a fact", source="t", links=("mem_never_written",))
    assert semantic.search(conn, "fact") == ()


def test_archiving_hides_a_fact_from_search_without_deleting_it(
    conn: sqlite3.Connection,
) -> None:
    """The vault archives rather than deletes, and so does this. Nothing in memory is destroyed."""
    fact = semantic.put(conn, "the ingest service listens on port 8471", source="t")
    assert semantic.search(conn, "ingest")

    archived = semantic.archive(conn, fact.mem_id)
    assert archived is not None and archived.status == semantic.ARCHIVED

    assert semantic.search(conn, "ingest") == ()
    assert semantic.search(conn, "ingest", include_archived=True)
    still_there = semantic.get(conn, fact.mem_id)
    assert still_there is not None and still_there.text == fact.text


def test_archiving_twice_reports_the_existing_state(conn: sqlite3.Connection) -> None:
    """The idempotency rule every other write path here follows."""
    fact = semantic.put(conn, "a fact worth retiring", source="t")
    first = semantic.archive(conn, fact.mem_id)
    second = semantic.archive(conn, fact.mem_id)
    assert first == second


def test_archiving_a_fact_that_does_not_exist_is_a_miss_not_an_error(
    conn: sqlite3.Connection,
) -> None:
    assert semantic.archive(conn, "mem_never_written") is None


def test_a_tag_is_scrubbed_like_every_other_stored_string(conn: sqlite3.Connection) -> None:
    """FR-13 has no exemption for short strings."""
    secret = "gsk" + "_" + "AAAABBBBCCCCDDDDEEEEFFFF"
    fact = semantic.put(conn, "a fact", source="t", tags=(secret,))
    stored = semantic.get(conn, fact.mem_id)
    assert stored is not None
    assert secret not in stored.tags[0]


def test_facts_can_be_narrowed_to_a_tag(conn: sqlite3.Connection) -> None:
    """A filter narrows the candidate set before the token ceiling counts anything."""
    semantic.put(conn, "the ingest service listens on port 8471", source="t", tags=("infra",))
    semantic.put(conn, "the ingest report is filed monthly", source="t", tags=("admin",))
    found = semantic.search(conn, "ingest", tag="infra")
    assert [f.text for f in found] == ["the ingest service listens on port 8471"]


# ---- ticket 129 (FR-70): GET /v1/memory/totals, four figures over the three stores -----------
#
# Decision 41: an aggregate over one package's tables is that package's store read, so
# ``memory/totals.py`` owns it and the gateway carries the dict. Archived facts are their own
# count and never filtered into the active one (005's rule); ``procedures_active`` is the
# active set, not every procedure ever written.

EVIDENCE_129 = Path(__file__).resolve().parents[1] / "evidence" / "129"
MEMORY_TOTALS_ROUTE = "/v1/memory/totals"
MEMORY_TOTALS_ARGV = ["memory", "totals", "--json"]
MEMORY_TOTALS_KEYS = ("facts_active", "facts_archived", "episodes", "procedures_active")
MEMORY_ROUTES = Path(__file__).resolve().parents[1] / "src" / "harnessed" / "gateway" / "routes"

cli = CliRunner()


def capture_129(name: str, text: str) -> Path:
    """Write ticket 129's evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_129.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_129 / name
    path.write_text(text, encoding="utf-8")
    return path


def seed_memory(conn: sqlite3.Connection) -> None:
    """Three facts with one archived, two episodes, three procedures with one active."""
    semantic.put(conn, "the ingest service listens on port 8471", source="t")
    semantic.put(conn, "the report is filed monthly", source="t")
    retired = semantic.put(conn, "the old port was 8470", source="t")
    semantic.archive(conn, retired.mem_id)
    episodic.put(conn, "run_seeded", "transcript", "hello")
    episodic.put(conn, "run_seeded", "transcript", "goodbye")
    first = procedural.put(conn, text="Answer with JSON only.", source="t")
    procedural.put(conn, text="Answer with prose.", source="t")
    procedural.put(conn, text="Answer briefly.", source="t")
    procedural.activate(conn, first.proc_id, cand_id=None)


def served_memory_totals(home: Path) -> tuple[dict[str, object], bytes]:
    client = TestClient(create_app(home / db.DATABASE_FILENAME), raise_server_exceptions=False)
    with client:
        response = client.get(MEMORY_TOTALS_ROUTE)
    assert response.status_code == 200, response.text
    return response.json(), response.content


def test_the_memory_totals_route_serves_four_figures_as_strings(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC 2: ``{"facts_active","facts_archived","episodes","procedures_active"}``, every figure
    a string, archived facts counted apart from the active ones."""
    seed_memory(conn)
    payload, body = served_memory_totals(tmp_harnessed_home)
    assert set(payload) == set(MEMORY_TOTALS_KEYS)
    for key in MEMORY_TOTALS_KEYS:
        assert isinstance(payload[key], str), (key, payload[key])
    assert payload == {
        "facts_active": "2",
        "facts_archived": "1",
        "episodes": "2",
        "procedures_active": "1",
    }
    capture_129(
        "memory-totals.txt",
        "ticket 129 - GET /v1/memory/totals over 3 facts (1 archived), 2 episodes and 3 "
        "procedures (1 active)\n"
        f"{body.decode()}\n"
        + "\n".join(f"{key}: {type(payload[key]).__name__} {payload[key]!r}" for key in payload)
        + "\n",
    )


def test_the_memory_totals_read_and_its_emitter_are_the_one_projection(
    conn: sqlite3.Connection,
) -> None:
    seed_memory(conn)
    found = totals.totals(conn)
    assert isinstance(found, totals.MemoryTotals)
    assert (found.facts_active, found.facts_archived, found.episodes) == (2, 1, 2)
    assert found.procedures_active == 1
    payload = totals.totals_payload(found)
    assert list(payload) == list(MEMORY_TOTALS_KEYS), "the emitter's own order"
    assert set(totals.__all__) >= {"MemoryTotals", "totals", "totals_payload"}


def test_an_empty_memory_totals_to_zero_never_null(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    """AC 3: every figure is ``"0"`` over empty tables, never ``null``."""
    db.connect().close()
    payload, body = served_memory_totals(tmp_harnessed_home)
    assert payload == {key: "0" for key in MEMORY_TOTALS_KEYS}
    assert "null" not in body.decode()
    prose = cli.invoke(app, ["memory", "totals"])
    assert prose.exit_code == 0, prose.output
    for label in ("facts active", "facts archived", "episodes", "procedures active"):
        line = next(ln for ln in prose.output.splitlines() if ln.strip().startswith(label))
        assert line.split()[-1] == "0", line
    capture_129(
        "memory-empty-table.txt",
        f"ticket 129 - the empty memory tables\nGET {MEMORY_TOTALS_ROUTE}\n{body.decode()}\n\n"
        "harnessed memory totals\n" + "".join(f"  {ln}\n" for ln in prose.output.splitlines()),
    )


def test_the_memory_totals_handler_calls_the_read_and_its_emitter_and_nothing_else() -> None:
    """AC 4: one callable from one package plus its emitter, 0 arithmetic, 0 SQL."""
    tree = ast.parse((MEMORY_ROUTES / "memory.py").read_text(encoding="utf-8"))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "memory_totals"
    )
    body = [node for stmt in handler.body for node in ast.walk(stmt)]
    calls = [ast.unparse(node.func) for node in body if isinstance(node, ast.Call)]
    assert calls == ["totals.totals_payload", "totals.totals"]
    assert not any(isinstance(node, ast.BinOp | ast.AugAssign) for node in body)
    strings = [n.value for n in body if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("SELECT" in value or "FROM " in value for value in strings), strings


def test_the_memory_totals_cli_twin_prints_the_bytes_the_route_serves(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    """AC 5: ``harnessed memory totals --json`` is the route's bytes; ``PARITY_SET`` holds the
    pair (asserted in ``tests/test_gateway_parity.py``)."""
    conn = db.connect()
    try:
        seed_memory(conn)
    finally:
        conn.close()
    result = cli.invoke(app, MEMORY_TOTALS_ARGV)
    assert result.exit_code == 0, result.output
    stdout = result.stdout.encode("utf-8")
    assert stdout.endswith(b"\n") and b"\n" not in stdout[:-1], stdout
    _, body = served_memory_totals(tmp_harnessed_home)
    assert stdout[:-1] == body
    prose = cli.invoke(app, ["memory", "totals"])
    assert prose.exit_code == 0, prose.output
    capture_129(
        "memory-parity-diff.txt",
        f"cli  harnessed {' '.join(MEMORY_TOTALS_ARGV)}\n  {stdout[:-1].decode()}\n"
        f"http GET {MEMORY_TOTALS_ROUTE}\n  {body.decode()}\nparity: equal as bytes\n\n"
        "cli  harnessed memory totals\n"
        + "".join(f"  {line}\n" for line in prose.output.splitlines()),
    )
