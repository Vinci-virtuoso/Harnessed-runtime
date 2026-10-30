"""The v4 migrations, one ticket per file, starting with ``009_event_indexes`` (ticket 128, FR-69).

Migrations are contiguous and forward-only (decision 9): a gap raises ``MigrationError`` at
load, so the number is assigned by **build order**, not by the order decision 47 listed them.
This file is created by the first v4 migration ticket and extended by 131 (``010``), 132
(``011``) and 138 (``012``), each adding its own section below.

``009_event_indexes`` is decision 47's answer to a measurement: the gate split read 208.2 ms
over 50,000 gate rows, above the 200 ms NFR, and the already shipped ``/v1/sessions?limit=20``
read 163.1 ms. Two partial indexes over ``events`` and nothing else bring them to 64.1 ms and
8.8 ms. So the tests here pin what the migration *is* (the two statements decision 47 spells,
verbatim), what it *does* (``user_version`` 8 to 9 and one ``schema_migrations`` row, zero
columns touched) and what it *buys* (SQLite's planner picks each index for the query it was
made for, read off ``EXPLAIN QUERY PLAN`` rather than assumed).
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from harnessed.errors import MigrationError
from harnessed.events import read
from harnessed.gateway import deps
from harnessed.memory import db

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "src" / "harnessed" / "memory" / "migrations"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "128"

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def columns_of(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """Every table's ``PRAGMA table_info`` rows, keyed by table: the whole column surface."""
    return {
        table: [tuple(row) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for table in table_names(conn)
    }


def index_rows(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """``name -> (table, sql)`` for every named index. Auto-indexes have no ``sql``."""
    rows = conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
    ).fetchall()
    return {str(name): (str(table), str(sql)) for name, table, sql in rows}


def plan_for(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> str:
    """The ``detail`` column of ``EXPLAIN QUERY PLAN``, one line per step."""
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", parameters).fetchall()
    return "\n".join(str(row[-1]) for row in rows)


def migrated_through(tmp_path: Path, count: int) -> tuple[sqlite3.Connection, Path]:
    """A fresh database migrated through the first ``count`` shipped migrations only."""
    partial = tmp_path / f"migrations_through_{count:03d}"
    partial.mkdir()
    shipped = db.load_migrations()
    for migration in shipped[:count]:
        shutil.copy(MIGRATIONS_DIR / migration.filename, partial / migration.filename)
    conn = sqlite3.connect(tmp_path / f"through_{count:03d}.db", isolation_level=None)
    db.migrate(conn, partial)
    return conn, partial


def apply_through(conn: sqlite3.Connection, partial: Path, count: int) -> db.MigrationReport:
    """Add shipped migrations up to ``count`` to ``partial`` and apply only those.

    What lets each migration's section test *its* step alone once a later migration ships:
    ``db.migrate(conn)`` with no source would apply every outstanding file at once.
    """
    for migration in db.load_migrations()[:count]:
        shutil.copy(MIGRATIONS_DIR / migration.filename, partial / migration.filename)
    return db.migrate(conn, partial)


# ===============================================================================================
# ticket 128 (FR-69, decision 47): 009_event_indexes
# ===============================================================================================

MIGRATION_009 = "009_event_indexes.sql"

#: Decision 47's two statements, verbatim. The migration adds these and nothing else.
INDEX_009 = {
    "events_first_idx": (
        "CREATE INDEX events_first_idx ON events (created_at DESC, run_id DESC) WHERE seq = 1"
    ),
    "events_gate_idx": (
        "CREATE INDEX events_gate_idx  ON events (name, payload) "
        "WHERE name = 'retrieval.gate.decided'"
    ),
}


def normalised(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";")


def test_009_is_the_ninth_shipped_migration_and_is_named_event_indexes() -> None:
    """Contiguity assigns the number: 009 is the first v4 migration because it is built first."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:9]] == list(range(1, 10)), "001 to 009 come first"
    ninth = shipped[8]
    assert (ninth.version, ninth.slug, ninth.filename) == (9, "event_indexes", MIGRATION_009)
    assert (MIGRATIONS_DIR / MIGRATION_009).is_file()


def test_009_spells_decision_47s_two_statements_and_no_other_statement() -> None:
    """The migration is the two partial indexes, verbatim, and nothing that touches a column."""
    text = (MIGRATIONS_DIR / MIGRATION_009).read_text(encoding="utf-8")
    statements = [
        normalised(line)
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("--")
    ]
    assert statements == [normalised(sql) for sql in INDEX_009.values()], statements
    lowered = text.lower()
    for forbidden in ("alter table", "create table", "add column", "drop", "insert", "update"):
        assert forbidden not in lowered, f"009 must add indexes only; found {forbidden!r}"


def test_009_bumps_user_version_from_8_to_exactly_9_and_records_itself(tmp_path: Path) -> None:
    """AC: ``user_version`` bumps by exactly one to 9 and ``schema_migrations`` records it."""
    conn, partial = migrated_through(tmp_path, 8)
    try:
        assert user_version(conn) == 8
        before = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        report = apply_through(conn, partial, 9)
        assert report.applied == (9,), "one migration applied, the ninth"
        assert user_version(conn) == 9
        assert report.version == 9
        rows = conn.execute(
            "SELECT version, slug, checksum FROM schema_migrations WHERE version = 9"
        ).fetchall()
        assert len(rows) == 1
        version, slug, checksum = rows[0]
        assert (version, slug) == (9, "event_indexes")
        assert checksum == db.load_migrations()[8].checksum
        after = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert after == before + 1
        capture(
            "user-version.txt",
            "ticket 128 - migration 009_event_indexes applied to a database at 008\n"
            f"PRAGMA user_version before: 8\nPRAGMA user_version after: {user_version(conn)}\n"
            f"schema_migrations row: version={version} slug={slug} checksum={checksum}\n",
        )
    finally:
        conn.close()


def test_009_adds_zero_columns_to_any_table(tmp_path: Path) -> None:
    """AC: the migration adds 0 columns. Every table's column surface is identical before and
    after; the only change in ``sqlite_master`` is the two index rows."""
    conn, partial = migrated_through(tmp_path, 8)
    try:
        tables_before = table_names(conn)
        columns_before = columns_of(conn)
        indexes_before = index_rows(conn)
        apply_through(conn, partial, 9)
        assert table_names(conn) == tables_before, "009 creates or drops no table"
        assert columns_of(conn) == columns_before, "009 adds, drops or alters no column"
        added = set(index_rows(conn)) - set(indexes_before)
        assert added == set(INDEX_009), f"009 adds exactly the two indexes, found {sorted(added)}"
    finally:
        conn.close()


def test_009s_indexes_are_the_two_partial_indexes_decision_47_spells(tmp_path: Path) -> None:
    """AC: the indexes are ``events_first_idx`` and ``events_gate_idx``, both partial, on
    ``events``, with the columns and the ``WHERE`` decision 47 spells."""
    conn, _ = migrated_through(tmp_path, 9)
    try:
        indexes = index_rows(conn)
        for name, expected in INDEX_009.items():
            assert name in indexes, f"{name} is missing after 009"
            table, sql = indexes[name]
            assert table == "events"
            assert normalised(sql) == normalised(expected), name
            assert " WHERE " in sql, f"{name} is not a partial index"
            partial = conn.execute("SELECT * FROM pragma_index_list('events')").fetchall()
            row = next(r for r in partial if r[1] == name)
            assert row[4] == 1, f"pragma_index_list says {name} is not partial"
    finally:
        conn.close()


def test_the_migrated_connection_the_package_opens_carries_009s_indexes(
    tmp_harnessed_home: Path,
) -> None:
    """``db.connect()`` on a fresh home lands at the shipped version with both indexes present:
    the path every CLI command takes, not only the loader called by hand."""
    conn = db.connect()
    try:
        assert db.schema_version(conn) == deps.shipped_version()
        assert set(INDEX_009) <= set(index_rows(conn))
    finally:
        conn.close()


def test_the_planner_picks_events_gate_idx_for_the_gate_split(tmp_harnessed_home: Path) -> None:
    """What the migration buys: the gate split reads through the partial index, never a scan."""
    conn = db.connect()
    try:
        plan = plan_for(conn, read._SELECT_GATE_SPLIT)
        assert "events_gate_idx" in plan, plan
        assert "SCAN events" not in plan, plan
    finally:
        conn.close()


def test_the_planner_picks_events_first_idx_for_the_session_listing(
    tmp_harnessed_home: Path,
) -> None:
    """Decision 47's second measurement: the shipped listing's ``seq = 1`` read is now indexed
    and ordered by the index, so the 163.1 ms scan is gone. The listing's SQL is decision 36's,
    verbatim and unchanged; only the plan moves."""
    conn = db.connect()
    try:
        plan = plan_for(conn, read._SELECT_SESSIONS, (20,))
        assert "USING INDEX events_first_idx" in plan, plan
        assert "TEMP B-TREE" not in plan, "the index supplies the order: " + plan
    finally:
        conn.close()


def test_a_database_at_8_is_refused_by_the_gateway_until_a_cli_command_migrates_it(
    tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    """Decision 18: the read gateway never migrates. A pre-009 file is ``SchemaOutOfDateError``
    there and current after any CLI-side ``connect``."""
    from harnessed.errors import SchemaOutOfDateError

    conn, _ = migrated_through(tmp_path, 8)
    conn.close()
    behind = tmp_path / "through_008.db"
    with pytest.raises(SchemaOutOfDateError):
        deps.guard(behind)
    current = db.connect(behind)
    try:
        assert db.schema_version(current) == deps.shipped_version()
    finally:
        current.close()
    deps.guard(behind)


def test_a_gap_before_009_is_a_migration_error_at_load(tmp_path: Path) -> None:
    """Decision 9, the warrant for the numbering: 001 to 008 plus a 010 with no 009 is refused."""
    partial = tmp_path / "gapped"
    partial.mkdir()
    shipped = db.load_migrations()
    for migration in shipped[:8]:
        shutil.copy(MIGRATIONS_DIR / migration.filename, partial / migration.filename)
    (partial / "010_turn_provider_model.sql").write_text(
        "ALTER TABLE turns ADD COLUMN provider TEXT;\n", encoding="utf-8"
    )
    with pytest.raises(MigrationError, match="contiguous"):
        db.load_migrations(partial)


def test_the_committed_migrations_before_010_are_byte_unchanged() -> None:
    """MUST rule 5: 001 to 009 are committed and never edited. Their checksums are pinned."""
    shipped = {m.version: m for m in db.load_migrations()}
    pins = {
        1: "001_init.sql",
        2: "002_memory.sql",
        3: "003_ledgers.sql",
        4: "004_flywheel.sql",
        5: "005_fact_metadata.sql",
        6: "006_turn_usage.sql",
        7: "007_turn_requested_at.sql",
        8: "008_agent_probes.sql",
        9: "009_event_indexes.sql",
    }
    for version, filename in pins.items():
        assert shipped[version].filename == filename
        text = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
        assert shipped[version].checksum == db.checksum(text)


# ===============================================================================================
# ticket 131 (FR-72, decision 53): 010_turn_provider_model
# ===============================================================================================

MIGRATION_010 = "010_turn_provider_model.sql"
EVIDENCE_131 = REPO_ROOT / "evidence" / "131"

#: The four statements FR-72 needs: nullable ``TEXT``, no default, on the two ledgers a score and
#: a token come from.
STATEMENTS_010 = [
    "ALTER TABLE turns ADD COLUMN provider TEXT",
    "ALTER TABLE turns ADD COLUMN model TEXT",
    "ALTER TABLE eval_runs ADD COLUMN provider TEXT",
    "ALTER TABLE eval_runs ADD COLUMN model TEXT",
]


def capture_131(name: str, text: str) -> Path:
    EVIDENCE_131.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_131 / name
    path.write_text(text, encoding="utf-8")
    return path


def test_010_is_the_tenth_shipped_migration_and_is_named_turn_provider_model() -> None:
    """Contiguity assigns the number: 128 took 009, so FR-72's migration is 010."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:10]] == list(range(1, 11))
    tenth = shipped[9]
    assert (tenth.version, tenth.slug, tenth.filename) == (10, "turn_provider_model", MIGRATION_010)


def test_010_spells_four_nullable_text_columns_and_nothing_else() -> None:
    text = (MIGRATIONS_DIR / MIGRATION_010).read_text(encoding="utf-8")
    statements = [
        normalised(line)
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("--")
    ]
    assert statements == STATEMENTS_010, statements
    lowered = "\n".join(statements).lower()
    for forbidden in ("default", "not null", "create", "drop", "insert", "update"):
        assert forbidden not in lowered, f"010 adds nullable columns only; found {forbidden!r}"


def test_010_bumps_user_version_from_9_to_exactly_10_and_records_itself(tmp_path: Path) -> None:
    """AC: ``user_version`` bumps by exactly one to 10, one ``schema_migrations`` row."""
    conn, partial = migrated_through(tmp_path, 9)
    try:
        assert user_version(conn) == 9
        before = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        report = apply_through(conn, partial, 10)
        assert report.applied == (10,)
        assert user_version(conn) == 10
        (slug,) = conn.execute("SELECT slug FROM schema_migrations WHERE version = 10").fetchone()
        assert slug == "turn_provider_model"
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == before + 1
        capture_131(
            "user-version.txt",
            "ticket 131 - migration 010_turn_provider_model applied to a database at 009\n"
            f"PRAGMA user_version before: 9\nPRAGMA user_version after: {user_version(conn)}\n",
        )
    finally:
        conn.close()


def test_010_adds_nullable_provider_and_model_text_to_turns_and_eval_runs_only(
    tmp_path: Path,
) -> None:
    """AC: ``turns`` and ``eval_runs`` each gain nullable ``provider`` and ``model`` ``TEXT``
    columns, and no other table's column surface moves."""
    conn, partial = migrated_through(tmp_path, 9)
    try:
        before = columns_of(conn)
        apply_through(conn, partial, 10)
        after = columns_of(conn)
        assert set(after) == set(before), "010 creates or drops no table"
        for table in after:
            if table in ("turns", "eval_runs"):
                continue
            assert after[table] == before[table], f"010 touched {table}"
        for table in ("turns", "eval_runs"):
            added = after[table][len(before[table]) :]
            assert after[table][: len(before[table])] == before[table]
            # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
            assert [(row[1], row[2], row[3], row[4], row[5]) for row in added] == [
                ("provider", "TEXT", 0, None, 0),
                ("model", "TEXT", 0, None, 0),
            ], table
    finally:
        conn.close()


def test_a_pre_010_turn_and_eval_run_read_null_provider_and_model(tmp_path: Path) -> None:
    """AC: a row written before 010 reads ``null`` for both, never a default provider."""
    conn, partial = migrated_through(tmp_path, 9)
    try:
        conn.execute(
            "INSERT INTO turns (run_id, iteration, request, response, stop_reason, created_at) "
            "VALUES ('run_old', 1, '{}', 'hi', 'end_turn', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO eval_runs (eval_run_id, suite, label, started_at) "
            "VALUES ('eval_old', 'smoke', 'default', '2026-01-01T00:00:00Z')"
        )
        apply_through(conn, partial, 10)
        turn = conn.execute("SELECT provider, model FROM turns WHERE run_id = 'run_old'").fetchone()
        run = conn.execute(
            "SELECT provider, model FROM eval_runs WHERE eval_run_id = 'eval_old'"
        ).fetchone()
        assert tuple(turn) == (None, None)
        assert tuple(run) == (None, None)
    finally:
        conn.close()


def test_doctor_reports_version_10(tmp_harnessed_home: Path) -> None:
    """AC: ``doctor`` reports the new version."""
    from harnessed.cli import doctor_cmd

    check = doctor_cmd._check_migrations(None)
    assert check.status == "ok", check
    shipped = len(db.load_migrations())
    assert f"at version {shipped} of {shipped}" in check.detail
    assert shipped >= 10
    assert deps.shipped_version() == shipped
    capture_131("doctor.txt", f"ticket 131 - doctor migrations.current: {check.detail}\n")


# ===============================================================================================
# ticket 138 (FR-80, decision 50): 012_run_messages
# ===============================================================================================

MIGRATION_012 = "012_run_messages.sql"
EVIDENCE_138 = REPO_ROOT / "evidence" / "138"

#: Decision 50's table, as ``PRAGMA table_info`` reads it: ``(name, type, notnull, pk)``. The two
#: JSON columns are nullable because most messages carry no call and no result.
RUN_MESSAGES_COLUMNS = [
    ("run_id", "TEXT", 1, 1),
    ("idx", "INTEGER", 1, 2),
    ("role", "TEXT", 1, 0),
    ("content", "TEXT", 1, 0),
    ("tool_calls", "TEXT", 0, 0),
    ("tool_results", "TEXT", 0, 0),
    ("created_at", "TEXT", 1, 0),
]


def capture_138(name: str, text: str) -> Path:
    EVIDENCE_138.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_138 / name
    path.write_text(text, encoding="utf-8")
    return path


def test_012_is_the_twelfth_shipped_migration_and_is_named_run_messages() -> None:
    """Contiguity assigns the number: 009, 010 and 011 precede, so FR-80's migration is 012."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:12]] == list(range(1, 13))
    twelfth = shipped[11]
    assert (twelfth.version, twelfth.slug, twelfth.filename) == (12, "run_messages", MIGRATION_012)


def test_012_bumps_user_version_from_11_to_exactly_12_and_adds_only_run_messages(
    tmp_path: Path,
) -> None:
    """AC: ``run_messages`` exists keyed ``(run_id, idx)`` and ``user_version`` bumps by one."""
    conn, partial = migrated_through(tmp_path, 11)
    try:
        assert user_version(conn) == 11
        before = columns_of(conn)
        report = apply_through(conn, partial, 12)
        assert report.applied == (12,)
        assert user_version(conn) == 12
        after = columns_of(conn)
        assert set(after) - set(before) == {"run_messages"}, "012 creates one table"
        assert {table: after[table] for table in before} == before, "012 alters no other table"
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        shape = [(row[1], row[2], row[3], row[5]) for row in after["run_messages"]]
        assert shape == RUN_MESSAGES_COLUMNS
        (slug,) = conn.execute("SELECT slug FROM schema_migrations WHERE version = 12").fetchone()
        assert slug == "run_messages"
        capture_138(
            "user-version.txt",
            "ticket 138 - migration 012_run_messages applied to a database at 011\n"
            f"PRAGMA user_version before: 11\nPRAGMA user_version after: {user_version(conn)}\n"
            f"run_messages columns: {shape}\n",
        )
    finally:
        conn.close()


def test_the_committed_migrations_before_012_are_byte_unchanged() -> None:
    """MUST rule 5: 010 and 011 are committed and never edited, like 001 to 009 above."""
    shipped = {m.version: m for m in db.load_migrations()}
    for version, filename in ((10, "010_turn_provider_model.sql"), (11, "011_prices.sql")):
        assert shipped[version].filename == filename
        text = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
        assert shipped[version].checksum == db.checksum(text)


# ===============================================================================================
# ticket 156 (FR-97, decision 57): 013_router_probes
# ===============================================================================================

MIGRATION_013 = "013_router_probes.sql"
EVIDENCE_156 = REPO_ROOT / "evidence" / "156"

#: ``PRAGMA table_info`` reads as ``(name, type, notnull, pk)``. ``cost`` is nullable: a failed
#: probe never reaches a reported ``usage.cost`` to round. 0 ``REAL`` columns (AC).
ROUTER_PROBES_COLUMNS = [
    ("probe_id", "INTEGER", 0, 1),
    ("model", "TEXT", 1, 0),
    ("status", "TEXT", 1, 0),
    ("latency_seconds", "TEXT", 1, 0),
    ("cost", "TEXT", 0, 0),
    ("detail", "TEXT", 1, 0),
    ("probed_at", "TEXT", 1, 0),
]


def capture_156(name: str, text: str) -> Path:
    EVIDENCE_156.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_156 / name
    path.write_text(text, encoding="utf-8")
    return path


def test_013_is_the_thirteenth_shipped_migration_and_is_named_router_probes() -> None:
    """Contiguity assigns the number: 009 to 012 precede, so FR-97's migration is 013."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:13]] == list(range(1, 14))
    thirteenth = shipped[12]
    assert (thirteenth.version, thirteenth.slug, thirteenth.filename) == (
        13,
        "router_probes",
        MIGRATION_013,
    )


def test_013_bumps_user_version_from_12_to_exactly_13_and_adds_only_router_probes(
    tmp_path: Path,
) -> None:
    """AC: ``user_version`` moves 12 to exactly 13, adding only ``router_probes``, 0 ``REAL``
    columns, and every column the migration's own text names."""
    conn, partial = migrated_through(tmp_path, 12)
    try:
        assert user_version(conn) == 12
        before = columns_of(conn)
        report = apply_through(conn, partial, 13)
        assert report.applied == (13,)
        assert user_version(conn) == 13
        after = columns_of(conn)
        assert set(after) - set(before) == {"router_probes"}, "013 creates one table"
        assert {table: after[table] for table in before} == before, "013 alters no other table"
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        shape = [(row[1], row[2], row[3], row[5]) for row in after["router_probes"]]
        assert shape == ROUTER_PROBES_COLUMNS
        real_columns = [column for column in shape if column[1] == "REAL"]
        assert real_columns == [], "0 REAL columns (AC)"
        (slug,) = conn.execute("SELECT slug FROM schema_migrations WHERE version = 13").fetchone()
        assert slug == "router_probes"
        capture_156(
            "user-version.txt",
            "ticket 156 - migration 013_router_probes applied to a database at 12\n"
            f"PRAGMA user_version before: 12\nPRAGMA user_version after: {user_version(conn)}\n"
            f"router_probes columns: {shape}\n",
        )
    finally:
        conn.close()


def test_the_committed_migrations_before_013_are_byte_unchanged() -> None:
    """MUST rule 5: 012 is committed and never edited, like every migration above it."""
    shipped = {m.version: m for m in db.load_migrations()}
    text = (MIGRATIONS_DIR / "012_run_messages.sql").read_text(encoding="utf-8")
    assert shipped[12].filename == "012_run_messages.sql"
    assert shipped[12].checksum == db.checksum(text)


def test_no_table_or_view_is_named_like_a_conversation(tmp_harnessed_home: Path) -> None:
    """Decision 50: a session is one run. 0 tables or views named conversation, chat or thread."""
    conn = db.connect()
    try:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert "run_messages" in names
    for word in ("conversation", "chat", "thread"):
        assert not [name for name in names if word in name.lower()], word


# ===============================================================================================
# ticket 141 (FR-83, decision 50): no conversations table, after any migration
# ===============================================================================================

#: The words a table or view keeping a transcript beside the run would be named with.
CONVERSATION_WORDS = ("conversation", "chat", "thread")


def test_no_table_or_view_is_named_like_a_conversation_after_any_migration(
    tmp_path: Path,
) -> None:
    """FR-83 (c): ``sqlite_master`` after **every** migration, not only the last, holds 0 tables or
    views named conversation, chat or thread. HISTORY is ``GET /v1/sessions?limit=`` and nothing
    else; the message list is ``run_messages``, keyed by run key (decision 50)."""
    conn, partial = migrated_through(tmp_path, 1)
    lines = ["ticket 141 - sqlite_master after each shipped migration (tables and views)"]
    try:
        for migration in db.load_migrations():
            assert apply_through(conn, partial, migration.version).version == migration.version
            names = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
                ).fetchall()
            ]
            matched = [
                name for name in names for word in CONVERSATION_WORDS if word in name.lower()
            ]
            assert matched == [], (migration.filename, matched)
            lines.append(f"{migration.filename}: {len(names)} names, {len(matched)} matched")
    finally:
        conn.close()
    lines.append(f"words: {', '.join(CONVERSATION_WORDS)}")
    EVIDENCE_141 = REPO_ROOT / "evidence" / "141"
    EVIDENCE_141.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_141 / "sqlite-master.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ===============================================================================================
# ticket 161 (FR-108, decision 61): 014_decisions
# ===============================================================================================

MIGRATION_014 = "014_decisions.sql"
EVIDENCE_161 = REPO_ROOT / "evidence" / "161"

# PRAGMA table_info: (name, type, notnull, pk)
DECISIONS_COLUMNS = [
    ("run_id", "TEXT", 1, 1),
    ("idx", "INTEGER", 1, 2),
    ("message_seq", "INTEGER", 1, 0),
    ("hop", "INTEGER", 1, 0),
    ("task", "TEXT", 0, 0),
    ("outcome", "TEXT", 1, 0),
    ("route", "TEXT", 0, 0),
    ("handler", "TEXT", 0, 0),
    ("confidence", "TEXT", 0, 0),
    ("confidence_percent", "TEXT", 0, 0),
    ("floor", "TEXT", 0, 0),
    ("floor_percent", "TEXT", 0, 0),
    ("needs_memory", "TEXT", 0, 0),
    ("needs_memory_percent", "TEXT", 0, 0),
    ("memory_kind", "TEXT", 0, 0),
    ("stop_reason", "TEXT", 0, 0),
    ("fallback_cause", "TEXT", 0, 0),
    ("state_truncated", "INTEGER", 1, 0),
    ("latency_seconds", "TEXT", 1, 0),
    ("input_tokens", "INTEGER", 0, 0),
    ("output_tokens", "INTEGER", 0, 0),
    ("cost", "TEXT", 0, 0),
    ("cost_nanos", "INTEGER", 0, 0),
    ("model", "TEXT", 1, 0),
    ("decision_id", "TEXT", 0, 0),
    ("task_hash", "TEXT", 0, 0),
    ("created_at", "TEXT", 1, 0),
]

DECISION_OPTIONS_COLUMNS = [
    ("run_id", "TEXT", 1, 1),
    ("idx", "INTEGER", 1, 2),
    ("question", "TEXT", 1, 3),
    ("position", "INTEGER", 1, 0),
    ("option", "TEXT", 1, 4),
    ("description", "TEXT", 0, 0),
    ("probability", "TEXT", 1, 0),
    ("probability_percent", "TEXT", 1, 0),
]


def capture_161(name: str, text: str) -> Path:
    EVIDENCE_161.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_161 / name
    path.write_text(text, encoding="utf-8")
    return path


def test_014_is_the_fourteenth_shipped_migration_and_is_named_decisions() -> None:
    """Contiguity assigns the number: 156 took 013, so FR-108's migration is 014."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:14]] == list(range(1, 15))
    fourteenth = shipped[13]
    assert (fourteenth.version, fourteenth.slug, fourteenth.filename) == (
        14,
        "decisions",
        MIGRATION_014,
    )


def test_014_bumps_user_version_from_13_to_exactly_14_and_adds_two_tables_and_one_column(
    tmp_path: Path,
) -> None:
    """AC: ``user_version`` moves 13 to exactly 14, adding only ``decisions`` and
    ``decision_options`` as new tables, one nullable ``message_seq`` on ``turns``, and 0 ``REAL``
    columns across the two new tables."""
    conn, partial = migrated_through(tmp_path, 13)
    try:
        assert user_version(conn) == 13
        before = columns_of(conn)
        report = apply_through(conn, partial, 14)
        assert report.applied == (14,)
        assert user_version(conn) == 14
        after = columns_of(conn)
        assert set(after) - set(before) == {"decisions", "decision_options"}, "014 creates two"
        for table in before:
            if table == "turns":
                continue
            assert after[table] == before[table], f"014 touched {table}"

        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        decisions_shape = [(row[1], row[2], row[3], row[5]) for row in after["decisions"]]
        options_shape = [(row[1], row[2], row[3], row[5]) for row in after["decision_options"]]
        assert decisions_shape == DECISIONS_COLUMNS
        assert options_shape == DECISION_OPTIONS_COLUMNS
        real_columns = [c for c in (*decisions_shape, *options_shape) if c[1] == "REAL"]
        assert real_columns == [], "0 REAL columns (AC)"

        turns_added = after["turns"][len(before["turns"]) :]
        assert after["turns"][: len(before["turns"])] == before["turns"], "turns is append-only"
        assert [(row[1], row[2], row[3], row[4], row[5]) for row in turns_added] == [
            ("message_seq", "INTEGER", 0, None, 0)
        ]

        (slug,) = conn.execute("SELECT slug FROM schema_migrations WHERE version = 14").fetchone()
        assert slug == "decisions"
        capture_161(
            "user-version.txt",
            "ticket 161 - migration 014_decisions applied to a database at 13\n"
            f"PRAGMA user_version before: 13\nPRAGMA user_version after: {user_version(conn)}\n"
            f"decisions columns: {decisions_shape}\n"
            f"decision_options columns: {options_shape}\n"
            f"turns gained: {turns_added and [(r[1], r[2]) for r in turns_added]}\n",
        )
    finally:
        conn.close()


def test_a_pre_014_turn_reads_null_message_seq(tmp_path: Path) -> None:
    """AC: a turn written before 014 reads ``null`` for ``message_seq``, never a guessed one."""
    conn, partial = migrated_through(tmp_path, 13)
    try:
        conn.execute(
            "INSERT INTO turns (run_id, iteration, request, response, stop_reason, created_at) "
            "VALUES ('run_old', 1, '{}', 'hi', 'end_turn', '2026-01-01T00:00:00Z')"
        )
        apply_through(conn, partial, 14)
        (message_seq,) = conn.execute(
            "SELECT message_seq FROM turns WHERE run_id = 'run_old'"
        ).fetchone()
        assert message_seq is None
    finally:
        conn.close()


def test_the_committed_migrations_before_014_are_byte_unchanged() -> None:
    """MUST rule 5: 013 is committed and never edited, like every migration above it."""
    shipped = {m.version: m for m in db.load_migrations()}
    text = (MIGRATIONS_DIR / "013_router_probes.sql").read_text(encoding="utf-8")
    assert shipped[13].filename == "013_router_probes.sql"
    assert shipped[13].checksum == db.checksum(text)


# ===============================================================================================
# ticket 171 (FR-112, decisions 63 and 64): 015_agent_turns
# ===============================================================================================

MIGRATION_015 = "015_agent_turns.sql"
EVIDENCE_171 = REPO_ROOT / "evidence" / "171"

# PRAGMA table_info: (name, type, notnull, pk)
AGENT_TURNS_COLUMNS = [
    ("run_id", "TEXT", 1, 1),
    ("idx", "INTEGER", 1, 2),
    ("message_seq", "INTEGER", 0, 0),
    ("hop", "INTEGER", 1, 0),
    ("task_hash", "TEXT", 0, 0),
    ("entry", "TEXT", 1, 0),
    ("billing", "TEXT", 0, 0),
    ("source", "TEXT", 1, 0),
    ("model", "TEXT", 0, 0),
    ("request", "TEXT", 0, 0),
    ("response", "TEXT", 1, 0),
    ("tool_calls", "TEXT", 0, 0),
    ("input_tokens", "INTEGER", 0, 0),
    ("output_tokens", "INTEGER", 0, 0),
    ("cached_input_tokens", "INTEGER", 0, 0),
    ("reasoning_tokens", "INTEGER", 0, 0),
    ("cost", "TEXT", 0, 0),
    ("cost_nanos", "INTEGER", 0, 0),
    ("currency", "TEXT", 0, 0),
    ("cost_kind", "TEXT", 0, 0),
    ("generation_id", "TEXT", 0, 0),
    ("complete", "INTEGER", 1, 0),
    ("created_at", "TEXT", 1, 0),
]

#: Decision 64's three covering indexes, by name and table. 015 adds these and no other index.
INDEX_015 = {
    "agent_turns_cost_idx": (
        "agent_turns",
        "CREATE INDEX agent_turns_cost_idx ON agent_turns (entry, billing, cost_kind, cost_nanos)",
    ),
    "decisions_cost_idx": (
        "decisions",
        "CREATE INDEX decisions_cost_idx ON decisions (cost_nanos)",
    ),
    "turns_spend_idx": (
        "turns",
        "CREATE INDEX turns_spend_idx ON turns "
        "(provider, model, input_tokens, output_tokens, cached_input_tokens)",
    ),
}


def capture_171(name: str, text: str) -> Path:
    EVIDENCE_171.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_171 / name
    path.write_text(text, encoding="utf-8")
    return path


def test_015_is_the_fifteenth_shipped_migration_and_is_named_agent_turns() -> None:
    """Contiguity assigns the number: 161 took 014, so FR-112's migration is 015."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:15]] == list(range(1, 16))
    fifteenth = shipped[14]
    assert (fifteenth.version, fifteenth.slug, fifteenth.filename) == (
        15,
        "agent_turns",
        MIGRATION_015,
    )


def test_015_bumps_user_version_from_14_to_exactly_15_and_adds_agent_turns_and_three_indexes(
    tmp_path: Path,
) -> None:
    """AC: ``user_version`` moves 14 to exactly 15, adding ``agent_turns`` and decision 64's three
    indexes only, with 0 ``REAL`` columns."""
    conn, partial = migrated_through(tmp_path, 14)
    try:
        assert user_version(conn) == 14
        before = columns_of(conn)
        indexes_before = index_rows(conn)
        report = apply_through(conn, partial, 15)
        assert report.applied == (15,)
        assert user_version(conn) == 15
        after = columns_of(conn)
        assert set(after) - set(before) == {"agent_turns"}, "015 creates one table"
        assert {table: after[table] for table in before} == before, "015 alters no other table"

        shape = [(row[1], row[2], row[3], row[5]) for row in after["agent_turns"]]
        assert shape == AGENT_TURNS_COLUMNS
        assert [column for column in shape if column[1] == "REAL"] == [], "0 REAL columns (AC)"

        indexes_after = index_rows(conn)
        added = {name: indexes_after[name] for name in set(indexes_after) - set(indexes_before)}
        assert {name: (table, normalised(sql)) for name, (table, sql) in added.items()} == {
            name: (table, normalised(sql)) for name, (table, sql) in INDEX_015.items()
        }

        (slug,) = conn.execute("SELECT slug FROM schema_migrations WHERE version = 15").fetchone()
        assert slug == "agent_turns"
        capture_171(
            "user-version.txt",
            "ticket 171 - migration 015_agent_turns applied to a database at 14\n"
            f"PRAGMA user_version before: 14\nPRAGMA user_version after: {user_version(conn)}\n"
            f"agent_turns columns: {shape}\n"
            f"indexes added: {sorted(added)}\n",
        )
    finally:
        conn.close()


def test_015s_checks_refuse_an_unknown_source_and_cost_kind(tmp_path: Path) -> None:
    """``source`` is ``acp|proxy|cli`` and ``cost_kind`` ``reported|api_equivalent`` or null."""
    conn, _partial = migrated_through(tmp_path, 15)
    insert = (
        "INSERT INTO agent_turns (run_id, idx, hop, entry, source, response, cost_kind, complete,"
        " created_at) VALUES ('run_x', ?, 0, 'probe', ?, '', ?, 1, '2026-01-01T00:00:00Z')"
    )
    try:
        conn.execute(insert, (0, "acp", None))
        conn.execute(insert, (1, "proxy", "reported"))
        conn.execute(insert, (2, "cli", "api_equivalent"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (3, "loop", None))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (4, "acp", "computed"))
    finally:
        conn.close()


#: Every committed migration's normalised checksum as it stood when 015 was written. Pinned as
#: literals, so an edit to any earlier file fails here rather than agreeing with itself.
COMMITTED_BEFORE_015 = {
    1: "c144a1dc1f9070e6b46248d668cf899cf4f73fdc9861704192d503da37ae35c3",
    2: "2ea1d607d42443620e27991c5eeb09fbb13b4a9b80f7bf0afa48d848e70495bc",
    3: "9948cd0b7e7a5ee9202cba3b76c6e6ff162e083db64147a6603a47f20a559748",
    4: "b1ca9efc25774a1dee4350d313a22162ba2ec36544383ba8a5c6fa019eb3df50",
    5: "212fd27c310c949555a0c36a0f935d5ecabfe31b666def2f51d2dc1f2fcadae1",
    6: "80b43f53ac7120b388cfa3a06f8939d3fa4decc0037f594065188e4e667144d6",
    7: "db910ce8a46af90918d469f05a39d110257a5a8e0c09530cc5ed3516749178f8",
    8: "aa4aa01bd337149bfc1f50df0667a55ef63a95ca8b3c1db4e6fda3ff4e483209",
    9: "de4242d44545036308d62136c5fa5e5ef0d788b0d5bdf4bfaadac17513968b1c",
    10: "b67aa5cb22b0841bb3724676f60da33ef3c9912cc151f1926eddd30e8bb47cc7",
    11: "9d8a2aa122d198d8387ae78e335f3c03b43198d0aa11dfb63771c782de374ba0",
    12: "58125c732487dce2fb7b19521cd34104b84944e03b08cc9c03cf1125594ebc7b",
    13: "9c38f419b2560b918e76438d29cc9b7633bcac794ac75f07b9a0f69a94bd1aa5",
    14: "b83a692d5b804baf00aff918405dbc207ff3d3d2f875cc7ab696a6746666f4f6",
}


def test_the_committed_migrations_before_015_are_byte_unchanged() -> None:
    """MUST rule 5: every migration 001 to 014 is committed and never edited."""
    shipped = {m.version: m for m in db.load_migrations()}
    for version, pinned in COMMITTED_BEFORE_015.items():
        text = (MIGRATIONS_DIR / shipped[version].filename).read_text(encoding="utf-8")
        assert db.checksum(text) == pinned, f"{shipped[version].filename} was edited"
    assert shipped[14].filename == "014_decisions.sql"


# ===============================================================================================
# ticket 181 (FR-127, decision 65): 016_outputs
# ===============================================================================================

MIGRATION_016 = "016_outputs.sql"

# PRAGMA table_info: (name, type, notnull, pk)
OUTPUTS_COLUMNS = [
    ("output_id", "TEXT", 0, 1),
    ("run_id", "TEXT", 1, 0),
    ("message_seq", "INTEGER", 0, 0),
    ("hop", "INTEGER", 1, 0),
    ("position", "INTEGER", 1, 0),
    ("task_hash", "TEXT", 0, 0),
    ("output_kind", "TEXT", 1, 0),
    ("media_type", "TEXT", 1, 0),
    ("name", "TEXT", 0, 0),
    ("caption", "TEXT", 0, 0),
    ("size_bytes", "INTEGER", 1, 0),
    ("too_large", "INTEGER", 1, 0),
    ("body", "BLOB", 0, 0),
    ("created_at", "TEXT", 1, 0),
]

#: 016 adds this one index and no other (the auto-index behind the ``output_id`` primary key
#: carries no ``sql`` and never appears in ``index_rows``).
INDEX_016 = {
    "outputs_hop_idx": (
        "outputs",
        "CREATE INDEX outputs_hop_idx ON outputs (run_id, message_seq, hop)",
    ),
}


def test_016_is_the_sixteenth_shipped_migration_and_is_named_outputs() -> None:
    """Contiguity assigns the number: 174 took 015, so FR-127's migration is 016."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:16]] == list(range(1, 17))
    sixteenth = shipped[15]
    assert (sixteenth.version, sixteenth.slug, sixteenth.filename) == (16, "outputs", MIGRATION_016)


def test_016_bumps_user_version_from_15_to_exactly_16_and_adds_only_outputs(tmp_path: Path) -> None:
    """AC6: ``user_version`` moves 15 to exactly 16, adding ``outputs`` and its one index only."""
    conn, partial = migrated_through(tmp_path, 15)
    try:
        assert user_version(conn) == 15
        before = columns_of(conn)
        indexes_before = index_rows(conn)
        report = apply_through(conn, partial, 16)
        assert report.applied == (16,)
        assert user_version(conn) == 16
        after = columns_of(conn)
        assert set(after) - set(before) == {"outputs"}, "016 creates one table"
        assert {table: after[table] for table in before} == before, "016 alters no other table"

        shape = [(row[1], row[2], row[3], row[5]) for row in after["outputs"]]
        assert shape == OUTPUTS_COLUMNS

        indexes_after = index_rows(conn)
        added = {name: indexes_after[name] for name in set(indexes_after) - set(indexes_before)}
        assert {name: (table, normalised(sql)) for name, (table, sql) in added.items()} == {
            name: (table, normalised(sql)) for name, (table, sql) in INDEX_016.items()
        }

        (slug,) = conn.execute("SELECT slug FROM schema_migrations WHERE version = 16").fetchone()
        assert slug == "outputs"
    finally:
        conn.close()


#: Every committed migration's normalised checksum as it stood when 016 was written: 001-014
#: unchanged from ``COMMITTED_BEFORE_015``, plus 015 itself, now that 016 ships behind it.
COMMITTED_BEFORE_016 = {
    **COMMITTED_BEFORE_015,
    15: "45fb71af562aec14b42b0318eb5ea9e5f6770a231891f7763954fea980465ff0",
}


def test_the_committed_migrations_before_016_are_byte_unchanged() -> None:
    """MUST rule 5: every migration 001 to 015 is committed and never edited."""
    shipped = {m.version: m for m in db.load_migrations()}
    for version, pinned in COMMITTED_BEFORE_016.items():
        text = (MIGRATIONS_DIR / shipped[version].filename).read_text(encoding="utf-8")
        assert db.checksum(text) == pinned, f"{shipped[version].filename} was edited"
    assert shipped[15].filename == "015_agent_turns.sql"


# ticket 231 (FR-160, decision 72): 017_eval_retries
MIGRATION_017 = "017_eval_retries.sql"
EVAL_RETRIES_COLUMNS = [
    ("eval_run_id", "TEXT", 1),
    ("case_id", "TEXT", 1),
    ("call", "TEXT", 1),
    ("attempt", "INTEGER", 1),
    ("error_class", "TEXT", 1),
    ("backoff_s", "REAL", 1),
    ("created_at", "TEXT", 1),
]


def test_017_is_the_seventeenth_shipped_migration_and_is_named_eval_retries() -> None:
    """The schema version is 17 and contiguous (ticket 231)."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:17]] == list(range(1, 18))
    seventeenth = shipped[16]
    assert (seventeenth.version, seventeenth.slug, seventeenth.filename) == (
        17,
        "eval_retries",
        MIGRATION_017,
    )


def test_017_bumps_user_version_from_16_to_exactly_17_and_adds_only_eval_retries(
    tmp_path: Path,
) -> None:
    conn, partial = migrated_through(tmp_path, 16)
    try:
        assert user_version(conn) == 16
        before = columns_of(conn)
        report = apply_through(conn, partial, 17)
        assert report.applied == (17,)
        assert user_version(conn) == 17
        after = columns_of(conn)
        assert set(after) - set(before) == {"eval_retries"}, "017 creates one table"
        assert {table: after[table] for table in before} == before, "017 alters no other table"
        shape = [(row[1], row[2], row[3]) for row in after["eval_retries"]]
        assert shape == EVAL_RETRIES_COLUMNS
        (slug,) = conn.execute("SELECT slug FROM schema_migrations WHERE version = 17").fetchone()
        assert slug == "eval_retries"
    finally:
        conn.close()


COMMITTED_BEFORE_017 = {
    **COMMITTED_BEFORE_016,
    16: "16ec4a4d174db2bfb8f48f304ce2ec94aefd35585ae1528fb14794a36ec72d7e",
}


def test_the_committed_migrations_before_017_are_byte_unchanged() -> None:
    """MUST rule 5: every migration 001 to 016 is committed and never edited."""
    shipped = {m.version: m for m in db.load_migrations()}
    for version, pinned in COMMITTED_BEFORE_017.items():
        text = (MIGRATIONS_DIR / shipped[version].filename).read_text(encoding="utf-8")
        assert db.checksum(text) == pinned, f"{shipped[version].filename} was edited"
    assert shipped[16].filename == "016_outputs.sql"


def test_018_is_the_eighteenth_shipped_migration_and_is_named_hop_results() -> None:
    """The schema version is 18 and contiguous (ticket 232)."""
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:18]] == list(range(1, 19))
    eighteenth = shipped[17]
    assert (eighteenth.version, eighteenth.slug, eighteenth.filename) == (
        18,
        "hop_results",
        "018_hop_results.sql",
    )


def test_018_bumps_user_version_from_17_to_exactly_18_and_adds_only_two_decisions_columns(
    tmp_path: Path,
) -> None:
    conn, partial = migrated_through(tmp_path, 17)
    try:
        assert user_version(conn) == 17
        before = columns_of(conn)
        report = apply_through(conn, partial, 18)
        assert report.applied == (18,)
        assert user_version(conn) == 18
        after = columns_of(conn)
        assert set(after) == set(before), "018 creates no table"
        assert {table: after[table] for table in before if table != "decisions"} == {
            table: before[table] for table in before if table != "decisions"
        }, "018 alters no other table"
        added = [row[1] for row in after["decisions"][len(before["decisions"]) :]]
        assert added == ["result", "result_reason"]
        (slug,) = conn.execute("SELECT slug FROM schema_migrations WHERE version = 18").fetchone()
        assert slug == "hop_results"
    finally:
        conn.close()


def test_018_leaves_a_pre_018_hop_null_and_refuses_a_word_outside_its_checks(
    tmp_path: Path,
) -> None:
    conn, partial = migrated_through(tmp_path, 17)
    try:
        conn.execute(
            "INSERT INTO decisions (run_id, idx, message_seq, hop, outcome, state_truncated, "
            "latency_seconds, model, created_at) VALUES ('r', 0, 1, 0, 'routed', 0, '0.1', 'm', "
            "'2026-01-01T00:00:00Z')"
        )
        conn.commit()
        apply_through(conn, partial, 18)
        row = conn.execute("SELECT result, result_reason FROM decisions").fetchone()
        assert tuple(row) == (None, None)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE decisions SET result = 'maybe'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE decisions SET result_reason = 'bored'")
    finally:
        conn.close()


def test_the_committed_migration_017_is_byte_unchanged() -> None:
    """MUST rule 5: 017 is committed and never edited."""
    text = (MIGRATIONS_DIR / "017_eval_retries.sql").read_text(encoding="utf-8")
    assert db.checksum(text) == "295075b2c96075267bdf66a11b8008e0912c0449123aac238ee244af313e09a4"


def test_the_committed_migration_018_is_byte_unchanged() -> None:
    """MUST rule 5: 018 is committed and never edited."""
    text = (MIGRATIONS_DIR / "018_hop_results.sql").read_text(encoding="utf-8")
    assert db.checksum(text) == "50b06a6d0c21ba4c5c2ba1d32763996cd2d56f6c3ef80576ace5d9cd650cc79c"


# ===============================================================================================
# ticket 234 (FR-130, decisions 74 and 78): 019_permissions
# ===============================================================================================


def test_019_is_the_nineteenth_shipped_migration_and_is_named_permissions() -> None:
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:19]] == list(range(1, 20))
    nineteenth = shipped[18]
    assert (nineteenth.version, nineteenth.slug, nineteenth.filename) == (
        19,
        "permissions",
        "019_permissions.sql",
    )


def test_019_bumps_user_version_from_18_to_exactly_19_and_adds_only_its_two_tables(
    tmp_path: Path,
) -> None:
    conn, partial = migrated_through(tmp_path, 18)
    try:
        assert user_version(conn) == 18
        before = columns_of(conn)
        report = apply_through(conn, partial, 19)
        assert report.applied == (19,)
        assert user_version(conn) == 19
        after = columns_of(conn)
        assert set(after) - set(before) == {"permission_decisions", "run_lease"}
        assert {table: after[table] for table in before} == before, "019 alters no other table"
        (slug,) = conn.execute("SELECT slug FROM schema_migrations WHERE version = 19").fetchone()
        assert slug == "permissions"
    finally:
        conn.close()


def test_019_refuses_a_word_outside_its_checks_and_a_second_lease_row(tmp_path: Path) -> None:
    conn, partial = migrated_through(tmp_path, 18)
    try:
        apply_through(conn, partial, 19)
        insert = (
            "INSERT INTO permission_decisions (permission_id, session_id, agent, tool_kind, "
            "requested_at, answer, rule, answered_at) "
            "VALUES ('p', 's', 'a', 'execute', 't', ?, ?, ?)"
        )
        conn.execute(insert, (None, None, None))  # an open request
        for refused in (
            ("maybe", "operator", "t"),
            ("approve", "bored", "t"),
            ("approve", None, "t"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert.replace("'p'", "'q'"), refused)
        conn.execute("INSERT INTO run_lease VALUES (1, 's', 1, NULL, 't')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO run_lease VALUES (2, 's', 1, NULL, 't')")
    finally:
        conn.close()


# ===============================================================================================
# ticket 239 (FR-140, decisions 76 and 79): 020_tasks
# ===============================================================================================


def test_020_is_the_twentieth_shipped_migration_and_is_named_tasks() -> None:
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:20]] == list(range(1, 21))
    assert (shipped[19].version, shipped[19].slug, shipped[19].filename) == (
        20,
        "tasks",
        "020_tasks.sql",
    )


def test_020_bumps_user_version_from_19_to_exactly_20_and_adds_only_its_four_tables(
    tmp_path: Path,
) -> None:
    conn, partial = migrated_through(tmp_path, 19)
    try:
        before = columns_of(conn)
        report = apply_through(conn, partial, 20)
        assert report.applied == (20,)
        assert user_version(conn) == 20
        after = columns_of(conn)
        assert set(after) - set(before) == {
            "tasks",
            "task_messages",
            "schedules",
            "schedule_installs",
        }
        assert {table: after[table] for table in before} == before, "020 alters no other table"
        indexes = {
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tasks'"
            )
        }
        assert {"tasks_state_idx", "tasks_paused_idx"} <= indexes
        assert [row[1] for row in conn.execute("PRAGMA table_info(task_messages)")] == [
            "run_id",
            "message_seq",
            "task_id",
            "kind",
        ]
        keyed = sorted(row[1] for row in conn.execute("PRAGMA table_info(task_messages)") if row[5])
        assert keyed == ["message_seq", "run_id"]
    finally:
        conn.close()


# ===============================================================================================
# ticket 244 (FR-145, decision 80): 021_repo_records
# ===============================================================================================


def test_021_is_the_twenty_first_shipped_migration_and_is_named_repo_records() -> None:
    shipped = db.load_migrations()
    assert [m.version for m in shipped[:21]] == list(range(1, 22))
    assert (shipped[20].version, shipped[20].slug, shipped[20].filename) == (
        21,
        "repo_records",
        "021_repo_records.sql",
    )


def test_021_bumps_user_version_from_20_to_exactly_21_and_adds_only_its_two_tables(
    tmp_path: Path,
) -> None:
    conn, partial = migrated_through(tmp_path, 20)
    try:
        before = columns_of(conn)
        report = apply_through(conn, partial, 21)
        assert report.applied == (21,)
        assert user_version(conn) == 21
        after = columns_of(conn)
        assert set(after) - set(before) == {"repo_records", "repo_paths"}
        assert {table: after[table] for table in before} == before, "021 alters no other table"
        keyed = sorted(row[1] for row in conn.execute("PRAGMA table_info(repo_records)") if row[5])
        assert keyed == ["attempt", "task_hash"]
        assert {
            "head_before",
            "head_after",
            "head_before_short",
            "head_after_short",
            "branch",
            "diff_stat",
            "changed_count",
            "started_dirty",
        } <= {row[1] for row in conn.execute("PRAGMA table_info(repo_records)")}
    finally:
        conn.close()


# ===============================================================================================
# ticket 245 (FR-146, decision 80): 022_repo_dirty_paths
# ===============================================================================================


def test_022_widens_repo_paths_kind_to_dirty_and_keeps_its_rows(tmp_path: Path) -> None:
    conn, partial = migrated_through(tmp_path, 21)
    try:
        conn.execute(
            "INSERT INTO repo_paths (task_hash, attempt, position, path, kind) "
            "VALUES ('task_a', 1, 0, 'a.txt', 'changed')"
        )
        conn.commit()
        before = columns_of(conn)
        report = apply_through(conn, partial, 22)
        assert report.applied == (22,)
        assert user_version(conn) == 22
        assert columns_of(conn) == before, "022 keeps every table's columns"
        assert conn.execute("SELECT path, kind FROM repo_paths").fetchall() == [
            ("a.txt", "changed")
        ]
        conn.execute(
            "INSERT INTO repo_paths (task_hash, attempt, position, path, kind) "
            "VALUES ('task_a', 1, 1, 'b.txt', 'dirty')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO repo_paths (task_hash, attempt, position, path, kind) "
                "VALUES ('task_a', 1, 2, 'c.txt', 'other')"
            )
    finally:
        conn.close()


# ===============================================================================================
# ticket 247 (FR-148, decision 81): 023_consolidation
# ===============================================================================================


def test_023_adds_only_the_two_consolidation_tables_and_the_source_index(tmp_path: Path) -> None:
    conn, partial = migrated_through(tmp_path, 22)
    try:
        before = columns_of(conn)
        report = apply_through(conn, partial, 23)
        assert report.applied == (23,)
        assert user_version(conn) == 23
        after = columns_of(conn)
        assert set(after) - set(before) == {"consolidation_batches", "mem_consolidated"}
        assert {table: after[table] for table in before} == before, "023 alters no other table"
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(mem_semantic)")}
        assert "mem_semantic_source_idx" in indexes
    finally:
        conn.close()


# ===============================================================================================
# ticket 287 (FR-167, FR-170, decisions 99, 100): 026_v6_debts
# ===============================================================================================

MIGRATION_026 = "026_v6_debts.sql"
NEW_026 = {
    "permission_decisions": [
        "task_hash",
        "detail",
        "warning",
        "waiter_pid",
        "waiter_started_at",
    ],
    "consolidation_batches": ["facts_skipped"],
}


def test_026_is_the_26th_shipped_migration_and_is_named_v6_debts() -> None:
    shipped = db.load_migrations()
    assert (shipped[25].version, shipped[25].slug) == (26, "v6_debts")
    assert shipped[25].filename == MIGRATION_026


def test_026_is_six_add_column_statements_and_no_rebuild() -> None:
    text = (MIGRATIONS_DIR / MIGRATION_026).read_text(encoding="utf-8")
    statements = [
        " ".join(s.split())
        for s in "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("--")
        ).split(";")
        if s.strip()
    ]
    assert len(statements) == 6
    assert all(s.startswith("ALTER TABLE ") and " ADD COLUMN " in s for s in statements), statements
    assert not any(word in text.upper() for word in ("DROP ", "CREATE TABLE", "RENAME"))


def test_026_adds_six_nullable_columns_and_a_version_25_database_keeps_its_rows(
    tmp_path: Path,
) -> None:
    conn, partial = migrated_through(tmp_path, 25)
    try:
        conn.execute(
            "INSERT INTO permission_decisions (permission_id, session_id, agent, tool_kind, "
            "description, requested_at) VALUES ('perm_1', 'ses_1', 'claude', 'edit', 'd', "
            "'2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO consolidation_batches (batch_id, session_id, status, episode_count, "
            "started_at) VALUES ('bat_1', 'ses_1', 'done', 3, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        old_perm = conn.execute("SELECT * FROM permission_decisions").fetchall()
        old_batch = conn.execute("SELECT * FROM consolidation_batches").fetchall()
        before = columns_of(conn)
        report = apply_through(conn, partial, 26)
        assert report.applied == (26,)
        assert user_version(conn) == 26
        after = columns_of(conn)
        assert set(after) == set(before), "026 creates no table"
        for table, names in NEW_026.items():
            assert [c[1] for c in after[table]][-len(names) :] == names
            assert after[table][: len(before[table])] == before[table]
            for column in after[table][len(before[table]) :]:
                assert column[3] == 0, f"{table}.{column[1]} is nullable"
        for table in set(after) - set(NEW_026):
            assert after[table] == before[table]
        new_perm = conn.execute("SELECT * FROM permission_decisions").fetchall()
        new_batch = conn.execute("SELECT * FROM consolidation_batches").fetchall()
        assert [r[: len(old_perm[0])] for r in new_perm] == old_perm
        assert [r[: len(old_batch[0])] for r in new_batch] == old_batch
        assert set(new_perm[0][len(old_perm[0]) :]) == {None}
        assert len(new_perm[0]) - len(old_perm[0]) == 5
        assert new_batch[0][len(old_batch[0]) :] == (None,)
    finally:
        conn.close()


# ticket 319 (FR-178, decision 92): 027_schedule_backends

MIGRATION_027 = "027_schedule_backends.sql"


def test_027_is_the_27th_shipped_migration_and_is_named_schedule_backends() -> None:
    shipped = db.load_migrations()
    assert (shipped[26].version, shipped[26].slug) == (27, "schedule_backends")
    assert shipped[26].filename == MIGRATION_027


def test_027_rebuilds_schedule_installs_to_admit_four_words_and_keeps_its_row(
    tmp_path: Path,
) -> None:
    conn, partial = migrated_through(tmp_path, 26)
    try:
        conn.execute(
            'INSERT INTO schedule_installs ("where", entry_name, command, arguments, '
            "working_dir, entry_sha256, installed_at) VALUES ('crontab', 'e', 'python', "
            "'-m harnessed task run --due', '/w', 'abc', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        old = conn.execute("SELECT * FROM schedule_installs").fetchall()
        cols = [c[1] for c in conn.execute("PRAGMA table_info(schedule_installs)")]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO schedule_installs ("where", entry_name, command, arguments, '
                "working_dir, entry_sha256, installed_at) VALUES ('launchd', 'e', 'p', 'a', "
                "'/w', 'x', 't')"
            )
        conn.rollback()
        report = apply_through(conn, partial, 27)
        assert report.applied == (27,)
        assert user_version(conn) == 27
        assert [c[1] for c in conn.execute("PRAGMA table_info(schedule_installs)")] == cols
        assert conn.execute("SELECT * FROM schedule_installs").fetchall() == old
        for word in ("task_scheduler", "crontab", "launchd", "systemd"):
            conn.execute(
                'INSERT INTO schedule_installs ("where", entry_name, command, arguments, '
                "working_dir, entry_sha256, installed_at) "
                "VALUES (?, 'e', 'p', 'a', '/w', 'x', 't')",
                (word,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO schedule_installs ("where", entry_name, command, arguments, '
                "working_dir, entry_sha256, installed_at) VALUES ('cron', 'e', 'p', 'a', '/w', "
                "'x', 't')"
            )
    finally:
        conn.close()
