"""``harnessed memory search`` answers any typed text, and its twin answers identically
(ticket 121, FR-58).

The CLI is the other caller that handed raw operator text to FTS5, so the same corpus is typed at
it here. The twin assertion is the reason this file and ``test_gateway_memory.py`` exist as a
pair: ``harnessed memory search <q> --json`` and ``GET /v1/memory/facts?q=<q>`` are one surface
with two front doors, and a fix applied to one of them would be a second source of truth. Both
sides are compared as bytes over one database, not as parsed objects over two.

Every input is passed after ``--``. Click reads a leading hyphen as an option, and ``-vinci`` is
exactly the kind of thing an operator types into a search box.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnessed.cli.app import app
from harnessed.gateway import create_app
from harnessed.memory import db, semantic
from test_memory_search import (
    CORPUS,
    HOSTILE_INPUTS,
    MEASURED,
    TERMLESS,
    capture,
)

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids", "clean_env")

FACTS = "/v1/memory/facts"

cli = CliRunner(mix_stderr=False)


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated database holding the ticket's two facts."""
    connection = db.connect()
    for text in CORPUS:
        semantic.put(connection, text, "ticket 121")
    yield connection
    connection.close()


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_harnessed_home: Path) -> Iterator[TestClient]:
    """The gateway over the same database the CLI opens."""
    gateway = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    with TestClient(gateway, raise_server_exceptions=False) as started:
        yield started


def search(*arguments: str) -> object:
    """``harnessed memory search`` with the query fenced off from Click's option parsing."""
    return cli.invoke(app, ["memory", "search", "--", *arguments])


# ---- AC 1: no typed text faults --------------------------------------------------------------


def test_no_typed_query_exits_non_zero(conn: sqlite3.Connection) -> None:
    lines = [f"ticket 121 - harnessed memory search, {len(HOSTILE_INPUTS)} inputs\n"]
    for text in HOSTILE_INPUTS:
        result = cli.invoke(app, ["memory", "search", "--", text])
        assert result.exit_code == 0, (text, result.stdout, result.stderr)
        assert "StorageError" not in result.stdout
        lines.append(f"{text!r:44} exit={result.exit_code} {result.stdout.splitlines()[0]}")
    capture("cli-inputs.txt", "\n".join(lines) + "\n")


def test_the_question_that_was_a_500_is_two_facts_here_too(conn: sqlite3.Connection) -> None:
    result = cli.invoke(app, ["memory", "search", "--", "who is Vinci?"])
    assert result.exit_code == 0, result.stderr
    assert "2 fact(s) matching" in result.stdout
    capture("cli-who-is-vinci.txt", f"$ harnessed memory search 'who is Vinci?'\n{result.stdout}")


def test_the_measured_table_holds_at_the_command_line(conn: sqlite3.Connection) -> None:
    for text, expected in MEASURED:
        result = cli.invoke(app, ["memory", "search", "--json", "--", text])
        assert result.exit_code == 0, (text, result.stderr)
        payload = result.stdout.strip()
        if expected is None:
            assert semantic.NO_TERMS_NOTE in payload, text
        else:
            assert payload.count('"mem_id"') == expected, (text, payload)


# ---- AC 3: no searchable term is said out loud, in both renderings ---------------------------


def test_text_with_no_searchable_term_is_named_in_the_human_output(
    conn: sqlite3.Connection,
) -> None:
    for text in TERMLESS:
        result = cli.invoke(app, ["memory", "search", "--", text])
        assert result.exit_code == 0, (text, result.stderr)
        assert "0 fact(s) matching" in result.stdout, text
        assert "no searchable term" in result.stdout, text


def test_no_fact_matched_reads_differently_from_nothing_to_search_for(
    conn: sqlite3.Connection,
) -> None:
    missed = cli.invoke(app, ["memory", "search", "--", "unhelddictionaryword"])
    assert "0 fact(s) matching" in missed.stdout
    assert "no searchable term" not in missed.stdout


def test_a_paste_over_the_cap_says_so_rather_than_truncating_quietly(
    conn: sqlite3.Connection,
) -> None:
    pasted = " ".join(f"word{index}" for index in range(semantic.MAX_SEARCH_TERMS + 5))
    result = cli.invoke(app, ["memory", "search", "--", pasted])
    assert result.exit_code == 0, result.stderr
    assert str(semantic.MAX_SEARCH_TERMS) in result.stdout


# ---- AC 4: the twin stays a twin -------------------------------------------------------------


def test_the_cli_and_the_gateway_answer_every_input_identically(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    """One corpus, one database, two front doors, compared as bytes (decision 19, FR-35).

    The comparison is on the canonical JSON both sides emit, so a key order, a re-rounded number
    or a note on one side alone fails here rather than being noticed by an operator.
    """
    lines = [f"ticket 121 - twin answers over {len(HOSTILE_INPUTS)} inputs\n"]
    for text in HOSTILE_INPUTS:
        result = cli.invoke(app, ["memory", "search", "--json", "--", text])
        assert result.exit_code == 0, (text, result.stderr)
        response = client.get(FACTS, params={"q": text})
        assert response.status_code == 200, (text, response.text)
        assert result.stdout.strip() == response.text, text
        lines.append(f"{text!r:44} {response.text[:96]}")
    capture("twins.txt", "\n".join(lines) + "\n")


def test_the_twin_comparison_can_fail(conn: sqlite3.Connection, client: TestClient) -> None:
    """Not vacuous: two different strings must not produce the same answer (MUST rule 12)."""
    one = cli.invoke(app, ["memory", "search", "--json", "--", "vinci"])
    assert one.stdout.strip() != client.get(FACTS, params={"q": "???"}).text


def test_consolidate_help_lists_the_quality_flags() -> None:
    result = cli.invoke(app, ["memory", "consolidate", "--help"])
    assert result.exit_code == 0
    for flag in ("--quality", "--fresh", "--evidence"):
        assert flag in result.stdout
