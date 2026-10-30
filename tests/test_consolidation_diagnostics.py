"""Consolidation rejection evidence and the general preference contract."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from tests.test_consolidation import new_fact, reply

from harnessed import clock, config
from harnessed.memory import consolidation, db, episodic, retrieval
from harnessed.providers import translate
from harnessed.providers.types import ChatResponse, ConsolidatedFact, Usage
from support.spies import CountingProvider


@pytest.fixture
def conn(clean_env, tmp_harnessed_home):
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def settings(clean_env):
    return replace(config.load(), small_model="fake-small")


def test_rejections_have_specific_reasons(conn: sqlite3.Connection) -> None:
    facts = (
        ConsolidatedFact("A preference", actor="invented"),
        ConsolidatedFact("A preference", path="invented/file.py"),
        ConsolidatedFact("Use the repository convention"),
        ConsolidatedFact("The operator prefers British spelling."),
        ConsolidatedFact("The operator prefers British spelling."),
    )
    rejected: list[dict[str, object]] = []
    kept, skipped = consolidation._fresh(conn, facts, rejected=rejected)
    assert len(kept) == 1 and skipped == 4
    assert [item["reason"] for item in rejected] == [
        "unknown_actor",
        "unsupported_path",
        "missing_path",
        "duplicate",
    ]


def test_diagnostics_survive_reopen_and_are_redacted(conn, settings) -> None:
    episodic.put(
        conn, "session", "loop", "Operator prefers colour. " + "Bearer " + "abcdefghijklmnop"
    )
    provider = CountingProvider([reply(new_fact("Use the repository convention"))])
    result = consolidation.consolidate(
        conn, provider=provider, settings=settings, now=clock.now(), size=5
    )
    assert result is not None and result.skipped == 1
    payload = consolidation.listing_payload(consolidation.listing(conn, 5))
    batch = payload["batches"][0]
    assert batch["diagnostics"]["rejected"][0]["reason"] == "missing_path"
    assert "Operator prefers colour" in batch["diagnostics"]["request"]["user"]
    assert json.loads(batch["diagnostics"]["response"])["facts"]
    assert "abcdefghijklmnop" not in json.dumps(batch["diagnostics"])
    assert "[redacted:bearer_token]" in json.dumps(batch["diagnostics"])
    with db.connect() as reopened:
        assert consolidation.diagnostics(reopened, result.batch_id) == batch["diagnostics"]
    assert conn.execute("SELECT COUNT(*) FROM consolidation_diagnostics").fetchone()[0] == 1


def test_historical_batch_has_no_diagnostics(conn) -> None:
    conn.execute(
        "INSERT INTO consolidation_batches (batch_id,status,episode_count,started_at) "
        "VALUES ('old','done',0,'then')"
    )
    payload = consolidation.listing_payload(consolidation.listing(conn, 5))
    assert payload["batches"][0]["diagnostics"] is None


def test_live_document_labels_remain_rejected_and_general_preference_is_admitted(conn):
    record = json.loads(
        (
            Path(__file__).parent / "fixtures" / "consolidation" / "document-label-paths-live.json"
        ).read_text()
    )
    facts = translate.consolidation_facts(record["response"])
    rejected = []
    kept, skipped = consolidation._fresh(conn, facts, rejected=rejected)
    assert not kept and skipped == len(facts)
    assert {item["reason"] for item in rejected} == {"unsupported_path"}
    general = ConsolidatedFact("The operator prefers British English spelling.")
    assert consolidation._fresh(conn, (general,)) == ((general,), 0)


def test_prompt_explains_the_path_contract_and_generalization(conn, settings):
    episodic.put(conn, "session", "loop", "Operator changed a word in the report.")
    provider = CountingProvider([reply()])
    consolidation.consolidate(conn, provider=provider, settings=settings, now=clock.now(), size=5)
    assert (
        "General operator preferences use actor operator and path null" in consolidation.INSTRUCTION
    )
    assert (
        "Admissible paths (use null when none applies): []" in provider.seen[0].messages[0].content
    )


@pytest.mark.parametrize(
    "text",
    [
        "The operator prefers British spelling (e.g., colour and organise).",
        "The operator prefers British spelling, i.e. colour and organise.",
    ],
)
def test_prose_abbreviations_are_not_file_mentions(conn, text):
    fact = ConsolidatedFact(text)
    assert consolidation._fresh(conn, (fact,)) == ((fact,), 0)


@pytest.mark.parametrize(
    "text", ["Edit config.py", "Use e.g.py", "Follow the repository convention"]
)
def test_actual_file_and_repository_mentions_still_require_paths(conn, text):
    fact = ConsolidatedFact(text)
    assert consolidation._fresh(conn, (fact,)) == ((), 1)


def test_recorded_general_preference_with_abbreviation_is_admitted(conn):
    record = json.loads(
        (
            Path(__file__).parent / "fixtures" / "consolidation" / "spelling-abbreviation-live.json"
        ).read_text()
    )
    facts = translate.consolidation_facts(record["response"])
    assert consolidation._fresh(conn, facts) == (facts, 0)


def test_recorded_preference_reaches_retrieval_and_a_second_batch_does_no_work(conn, settings):
    record = json.loads(
        (
            Path(__file__).parent / "fixtures" / "consolidation" / "spelling-abbreviation-live.json"
        ).read_text()
    )
    episodic.put(conn, "seed", "loop", "Operator changed color to colour.")
    provider = CountingProvider(
        [
            ChatResponse(
                response_id="recorded", model="recorded", content=record["response"], usage=Usage()
            )
        ]
    )
    outcome = consolidation.consolidate(
        conn, provider=provider, settings=settings, now=clock.now(), size=5
    )
    assert outcome is not None and len(outcome.facts) == 1
    context = retrieval.assemble(conn, "What spelling does the operator prefer?", budget=2000)
    assert outcome.facts[0].text in context.text
    assert (
        consolidation.consolidate(
            conn, provider=provider, settings=settings, now=clock.now(), size=5
        )
        is None
    )
    assert provider.calls == 1
    assert conn.execute("SELECT COUNT(*) FROM mem_semantic").fetchone()[0] == 1
