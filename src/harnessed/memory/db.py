"""The one SQLite file, its connection pragmas and its forward-only migration runner (FR-10).

Zero infrastructure is a hard constraint, so semantic, episodic and procedural memory plus all
four ledgers live in one file, ``${HARNESSED_HOME}/harness.db``. There is no ORM, no driver
dependency and no second database.

Migrations are numbered from ``001``, contiguous, forward-only and checksummed. A committed
migration is never edited (``state-and-idempotency.md``); the checksum recorded in
``schema_migrations`` is the enforcement, and it is re-verified on every run before anything
new is applied.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Final

from harnessed import clock, config
from harnessed.errors import MigrationChecksumError, MigrationError, StorageError

__all__ = [
    "DATABASE_FILENAME",
    "Migration",
    "MigrationReport",
    "checksum",
    "connect",
    "connect_readonly",
    "data_version",
    "database_path",
    "load_migrations",
    "migrate",
    "schema_version",
    "write",
]

DATABASE_FILENAME: Final = "harness.db"

# Applied in this order by connect(). WAL so the reader and the writer never block each other,
# NORMAL because WAL already survives a process crash, foreign keys because the ledgers declare
# them, and a busy timeout so a concurrent writer waits rather than raising.
_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)

# What connect_readonly() applies, in order, and nothing else. Both are connection-local: the
# busy timeout so a checkpointing writer is waited for rather than raised on, and query_only so
# even a connection that had to fall back to mode=rw refuses every write at the engine.
_READONLY_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA busy_timeout=5000",
    "PRAGMA query_only = ON",
)

#: The one open failure connect_readonly() retries: a WAL file that needs recovery while no
#: writer holds the file, which a read-only connection is not allowed to perform (decision 18).
_RECOVERY_NEEDED: Final = sqlite3.SQLITE_READONLY_RECOVERY

_MIGRATION_NAME = re.compile(r"^(?P<version>\d{3})_(?P<slug>[a-z0-9_]+)\.sql$")
_MIGRATIONS_DIR: Final = Path(str(resources.files("harnessed.memory") / "migrations"))

_RECORD_MIGRATION: Final = (
    "INSERT INTO schema_migrations (version, slug, checksum, applied_at) VALUES (?, ?, ?, ?)"
)


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered migration file, already read and checksummed."""

    version: int
    slug: str
    filename: str
    sql: str
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """What one :func:`migrate` call did. ``applied`` is empty when it was a no-op."""

    version: int
    applied: tuple[int, ...]
    migrations: tuple[Migration, ...]


def checksum(text: str) -> str:
    """``sha256`` over the migration text, normalized so two checkouts agree.

    Newlines collapse to ``\\n`` and trailing whitespace is stripped per line, which is the
    whole difference between a Windows checkout and a POSIX one.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def database_path() -> Path:
    """``${HARNESSED_HOME}/harness.db``. The only database file this project has."""
    return config.load().home / DATABASE_FILENAME


def load_migrations(source: Path | None = None) -> tuple[Migration, ...]:
    """Read every migration file in ``source``, ordered, validated and checksummed.

    Raises :class:`MigrationError` on a gap, a duplicate number, a filename that is not
    ``NNN_<slug>.sql``, an empty set or a missing directory. Every one of those is a malformed
    migration set, and the honest moment to say so is load, not halfway through applying.
    """
    directory = _MIGRATIONS_DIR if source is None else source
    if not directory.is_dir():
        raise MigrationError(f"migration directory {directory} does not exist")

    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        matched = _MIGRATION_NAME.match(path.name)
        if matched is None:
            raise MigrationError(
                f"migration {path.name!r} is not named NNN_<slug>.sql, so its version is "
                f"unknowable. Rename it or move it out of {directory}."
            )
        version = int(matched["version"])
        if version in found:
            raise MigrationError(
                f"duplicate migration number {version:03d}: {found[version].filename!r} and "
                f"{path.name!r}. Numbers are unique and forward-only."
            )
        text = path.read_text(encoding="utf-8")
        found[version] = Migration(
            version=version,
            slug=matched["slug"],
            filename=path.name,
            sql=text,
            checksum=checksum(text),
        )

    if not found:
        raise MigrationError(f"no migrations found in {directory}")
    expected = list(range(1, len(found) + 1))
    if sorted(found) != expected:
        raise MigrationError(
            f"migration numbering must be contiguous from 001: expected "
            f"{[f'{v:03d}' for v in expected]}, found {[f'{v:03d}' for v in sorted(found)]}."
        )
    return tuple(found[version] for version in expected)


def schema_version(conn: sqlite3.Connection) -> int:
    """The highest applied migration number, or ``0`` on a database with no migrations."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def data_version(conn: sqlite3.Connection) -> int:
    """SQLite's change counter for ``conn``: moves only when **another** connection commits.

    The gateway's tail gate (decision 16). A read-only connection never commits, so on one the
    number changes exactly when the CLI writer does, and an unchanged number means no query is
    worth running. Its absolute value carries no meaning and is never stored or compared across
    connections.
    """
    row = conn.execute("PRAGMA data_version").fetchone()
    return int(row[0]) if row is not None else 0


def migrate(conn: sqlite3.Connection, source: Path | None = None) -> MigrationReport:
    """Apply every migration absent from ``schema_migrations``, in order.

    Idempotent: a second call applies nothing. Before applying anything it re-verifies the
    checksum of every already-applied migration, so an edited committed file is caught before
    it can be built on. Each migration runs inside its own transaction together with its
    ``schema_migrations`` row, so a failure leaves neither.
    """
    migrations = load_migrations(source)
    recorded = _recorded_checksums(conn)
    _verify_recorded(migrations, recorded)

    applied: list[int] = []
    for migration in migrations:
        if migration.version in recorded:
            continue
        _apply(conn, migration)
        applied.append(migration.version)
    return MigrationReport(
        version=schema_version(conn), applied=tuple(applied), migrations=migrations
    )


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the one database, apply the pragmas, then migrate it. One connection per process."""
    target = database_path() if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(target, isolation_level=None)
    except sqlite3.Error as exc:
        raise StorageError(f"could not open the database at {target}: {exc}") from exc
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    migrate(conn)
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open the one database for reading only. No migration, no writing pragma (decision 18).

    The gateway's opener, one connection per request. ``mode=ro`` on the URI makes read-only a
    property SQLite enforces rather than a promise the caller keeps, ``query_only`` closes the
    same door from inside the connection, and nothing here touches the schema: the file is
    exactly as it was found, or the open fails as a named error. A missing file is such a
    failure, never a fresh empty database.

    Known caveat, recorded in the architecture: a ``mode=ro`` open fails when the WAL needs
    recovery and no writer is present. Exactly that error is retried once with ``mode=rw``,
    still under ``query_only``; every other failure is raised as it stands.
    """
    target = path.resolve()
    try:
        return _open_readonly(target, "ro")
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) != _RECOVERY_NEEDED:
            raise StorageError(_readonly_failure(target, exc)) from exc
    try:
        return _open_readonly(target, "rw")
    except sqlite3.Error as exc:
        raise StorageError(_readonly_failure(target, exc)) from exc


def _open_readonly(target: Path, mode: str) -> sqlite3.Connection:
    """One attempt: open by URI in ``mode``, apply the read-only pragmas, prove the file reads."""
    # check_same_thread=False because FastAPI runs a sync dependency's enter/exit and the
    # endpoint on separate threadpool workers, and a streaming body hops threads per chunk. The
    # connection is still one request's own, used sequentially and never shared; sqlite3 is
    # built serialized (threadsafety 3), so the handoff is safe.
    conn = sqlite3.connect(
        f"file:{target}?mode={mode}", uri=True, isolation_level=None, check_same_thread=False
    )
    try:
        for pragma in _READONLY_PRAGMAS:
            conn.execute(pragma)
        # SQLite opens lazily; the first read is what meets a WAL needing recovery, so it
        # happens here, inside the attempt, rather than in the caller's first query.
        schema_version(conn)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _readonly_failure(target: Path, exc: sqlite3.Error) -> str:
    return (
        f"could not open the database at {target} read-only: {exc}. The gateway creates and "
        f"migrates nothing; `harnessed doctor` does both."
    )


def write(conn: sqlite3.Connection, statements: tuple[tuple[str, tuple[object, ...]], ...]) -> None:
    """Run ``statements`` as one short transaction, or leave the database untouched.

    The single write entry point for the three stores. Every ``put()`` routes here, which is
    what keeps ticket 008's redaction rewiring small: there is one place that turns a record
    into rows, and one transaction boundary to reason about.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement, parameters in statements:
            conn.execute(statement, parameters)
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        # rollback() is a no-op when the failure was the BEGIN itself, which ROLLBACK is not.
        conn.rollback()
        raise StorageError(f"write failed and was rolled back: {exc}") from exc


def _recorded_checksums(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """``version -> (slug, checksum)`` for what is already applied. Empty on a fresh file."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if present is None:
        return {}
    return {
        int(version): (slug, recorded)
        for version, slug, recorded in conn.execute(
            "SELECT version, slug, checksum FROM schema_migrations"
        )
    }


def _verify_recorded(
    migrations: tuple[Migration, ...], recorded: dict[int, tuple[str, str]]
) -> None:
    """Raise if an already-applied migration file has changed since it was applied."""
    by_version = {migration.version: migration for migration in migrations}
    for version, (slug, digest) in sorted(recorded.items()):
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"migration {version:03d}_{slug}.sql is recorded as applied but is missing from "
                f"the migration set. A committed migration is never removed."
            )
        if migration.checksum != digest:
            raise MigrationChecksumError(
                f"{migration.filename} changed after it was applied. Recorded checksum "
                f"{digest}, computed checksum {migration.checksum}. A committed migration is "
                f"never edited: add a new numbered migration instead."
            )


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    """Run one migration body and record it, inside a single transaction."""
    body = tuple((statement, ()) for statement in _statements(migration))
    record = (
        _RECORD_MIGRATION,
        (migration.version, migration.slug, migration.checksum, clock.isoformat(clock.now())),
    )
    # PRAGMA user_version takes no parameter, and the value is an int parsed from the filename.
    user_version = (f"PRAGMA user_version = {migration.version:d}", ())
    write(conn, (*body, record, user_version))


def _statements(migration: Migration) -> tuple[str, ...]:
    """Split a migration body into complete statements.

    ``sqlite3.complete_statement`` is what makes this safe: it knows a semicolon inside a
    string literal, a comment or a ``CREATE TRIGGER ... BEGIN ... END`` block does not end a
    statement, which a naive split on ``;`` does not.
    """
    statements: list[str] = []
    buffer = ""
    for line in migration.sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    # A tail with real SQL in it is an unterminated statement, and executing it is how the
    # migration says so. A tail of trailing comments is not a statement at all.
    if not _is_only_comments(buffer):
        statements.append(buffer)
    return tuple(statements)


def _is_only_comments(text: str) -> bool:
    """True when ``text`` holds nothing SQLite would execute."""
    return all(not line.strip() or line.strip().startswith("--") for line in text.splitlines())
