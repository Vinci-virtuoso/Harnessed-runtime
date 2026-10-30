"""``GET /v1/memory/facts`` answers any typed text with a 200 (ticket 121, FR-58).

The defect this file closes was found through this route: the dashboard's search box puts the
operator's text in ``?q=`` unaltered, and a question mark in it was a 500 with a ``StorageError``
in the body. The route is where the corpus in ``test_memory_search.py`` meets HTTP, so it is
swept here in full rather than sampled.

``raise_server_exceptions=False`` on the client is deliberate: with it left on, an unhandled
handler exception is re-raised into the test and reads as an error rather than as the 500 the
operator actually saw. This file asserts on status codes, so it must receive them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harnessed.gateway import create_app
from harnessed.memory import db, semantic
from test_memory_search import (
    CORPUS,
    HOSTILE_INPUTS,
    MEASURED,
    TERMLESS,
    capture,
)

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")

FACTS = "/v1/memory/facts"


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
    """The app over the database ``conn`` wrote to."""
    app = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    with TestClient(app, raise_server_exceptions=False) as started:
        yield started


def test_no_typed_query_is_a_500(client: TestClient) -> None:
    """The whole corpus through the route. Every answer is a 200 carrying the facts key."""
    lines = [f"ticket 121 - GET {FACTS}?q=<typed>, {len(HOSTILE_INPUTS)} inputs\n"]
    for text in HOSTILE_INPUTS:
        response = client.get(FACTS, params={"q": text})
        assert response.status_code == 200, (text, response.status_code, response.text)
        payload = response.json()
        assert "facts" in payload, text
        assert set(payload) <= {"facts", "note"}, text
        lines.append(
            f"{text!r:44} {response.status_code} facts={len(payload['facts'])} "
            f"note={'note' in payload}"
        )
    capture("gateway-inputs.txt", "\n".join(lines) + "\n")


def test_the_question_that_was_a_500_is_now_two_facts(client: TestClient) -> None:
    """The operator's own report, end to end: ``who is Vinci?`` over the store it was typed at."""
    response = client.get(FACTS, params={"q": "who is Vinci?"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["facts"]) == 2
    assert "note" not in payload
    assert "StorageError" not in response.text
    capture(
        "gateway-who-is-vinci.txt",
        f"GET {FACTS}?q=who is Vinci?\n{response.status_code}\n{response.text}\n",
    )


def test_the_measured_table_holds_over_http(client: TestClient) -> None:
    for text, expected in MEASURED:
        payload = client.get(FACTS, params={"q": text}).json()
        if expected is None:
            assert payload["facts"] == [], text
            assert payload["note"] == semantic.NO_TERMS_NOTE, text
        else:
            assert len(payload["facts"]) == expected, (text, payload)
            assert "note" not in payload, text


def test_a_query_with_no_searchable_term_answers_200_and_names_why(client: TestClient) -> None:
    for text in TERMLESS:
        response = client.get(FACTS, params={"q": text})
        assert response.status_code == 200, (text, response.text)
        payload = response.json()
        assert payload == {"facts": [], "note": semantic.NO_TERMS_NOTE}, text


def test_a_missing_q_is_the_same_answer_as_a_termless_one(client: TestClient) -> None:
    """A scan that requests every registered path bare still reads an answer, not a fault."""
    response = client.get(FACTS)
    assert response.status_code == 200, response.text
    assert response.json() == {"facts": [], "note": semantic.NO_TERMS_NOTE}


def test_an_ordinary_query_still_carries_the_facts_key_alone(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    """Nothing that answered before this ticket answers differently after it."""
    payload = client.get(FACTS, params={"q": "harnessed"}).json()
    assert set(payload) == {"facts"}
    assert [row["text"] for row in payload["facts"]] == [
        fact.text for fact in semantic.searched(conn, "harnessed").facts
    ]


def test_the_limit_still_bounds_the_answer_and_a_bad_one_is_still_a_400(
    client: TestClient,
) -> None:
    """The route's other parameter is untouched by the builder."""
    assert len(client.get(FACTS, params={"q": "the", "limit": 1}).json()["facts"]) == 1
    refused = client.get(FACTS, params={"q": "the", "limit": "banana"})
    assert refused.status_code == 400, refused.text
    assert "banana" in refused.text


def test_a_paste_over_the_cap_tells_the_page_it_was_capped(client: TestClient) -> None:
    """Rule D-1: a panel may not show a result for a query it was not told was truncated."""
    pasted = " ".join(f"word{index}" for index in range(semantic.MAX_SEARCH_TERMS + 5))
    payload = client.get(FACTS, params={"q": pasted}).json()
    assert "note" in payload
    assert str(semantic.MAX_SEARCH_TERMS) in payload["note"]


def test_the_consolidation_read_is_an_empty_listing_on_a_fresh_home(client: TestClient) -> None:
    """Ticket 249 (FR-152): no batch is the empty collection plus the two counts, as strings."""
    served = client.get("/v1/memory/consolidation")
    assert served.status_code == 200
    body = served.json()
    assert body["batches"] == []
    assert isinstance(body["consolidate_after"], str)
    assert body["unconsolidated"] == "0"
