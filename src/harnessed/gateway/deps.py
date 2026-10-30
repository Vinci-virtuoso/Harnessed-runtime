"""Per-request read-only access to the one database, and the schema guard (decision 18).

Two things ride together here because they are the two halves of "the gateway cannot write".
:func:`connection` opens one ``db.connect_readonly`` connection per request, on the threadpool
worker that serves it, and closes it when the response is done: nothing is shared, so
``check_same_thread`` never arises and there is no pool to protect (MUST rule 17). :func:`guard`
runs once in ``create_app`` and compares the on-disk ``PRAGMA user_version`` with the number of
migrations the package ships; behind means :class:`SchemaOutOfDateError`, and the fix is a CLI
command, never this process (MUST rule 13). Neither function spells any SQL: the version read
and the migration count are ``memory/db.py``'s (MUST rule 14).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request

from harnessed.errors import SchemaOutOfDateError
from harnessed.memory import db

__all__ = ["check_schema", "connection", "database_path_of", "guard", "shipped_version"]


def shipped_version() -> int:
    """The highest migration number the package carries: what an up-to-date file reports."""
    return db.load_migrations()[-1].version


def check_schema(conn: sqlite3.Connection) -> None:
    """Raise :class:`SchemaOutOfDateError` when ``conn``'s file is behind the shipped set."""
    expected = shipped_version()
    actual = db.schema_version(conn)
    if actual < expected:
        raise SchemaOutOfDateError(expected=expected, actual=actual)


def guard(database_path: Path) -> None:
    """Compare the on-disk version with the shipped one before serving. Migrates nothing.

    A path with no file yet is left alone: there is no version to compare, and creating the file
    to read one would be the write this module exists to refuse. A request that needs the file
    then fails as a named ``StorageError`` from ``connect_readonly`` rather than as an empty
    database that looks like a real one.
    """
    if not database_path.is_file():
        return
    conn = db.connect_readonly(database_path)
    try:
        check_schema(conn)
    finally:
        conn.close()


def database_path_of(request: Request) -> Path:
    """The absolute path ``create_app`` was handed, carried on the app rather than a global."""
    path: Path = request.app.state.database_path
    return path


def connection(request: Request) -> Iterator[sqlite3.Connection]:
    """One read-only connection for the request being served, closed when it is done.

    A plain generator, not a coroutine: Starlette runs it in the threadpool beside the sync
    handler that depends on it, which is exactly the thread the connection must stay on.
    """
    conn = db.connect_readonly(database_path_of(request))
    try:
        yield conn
    finally:
        conn.close()
