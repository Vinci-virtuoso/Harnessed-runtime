"""FR-148, FR-149 (decision 81): consolidation has a ledger, a trigger and one call through the
gate's seam.

A batch of unconsolidated episodes is distilled into facts with one small-model call, outside
``loop/``. Every claim is measured: the batch id by recomputing the hash, the trigger by counting
provider calls on both sides of the threshold, the request by reading the one ``ChatRequest`` the
provider was handed, the three doors by what the world looked like at the instant the trigger
ran, and "no eval or improve path calls it" by a counter across both commands as well as a scan.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.support.task_world import CannedProvider, ScriptedDecider, answer, install
from typer.testing import CliRunner

from harnessed import clock, config, control, ids, proc, shipped
from harnessed.cli import run_cmd, task_run
from harnessed.cli.app import app
from harnessed.errors import ProviderError, TranslationError
from harnessed.events import read
from harnessed.events.bus import EventBus
from harnessed.memory import consolidation, db, episodic, procedural, semantic
from harnessed.providers import translate
from harnessed.providers.fake import FakeProvider
from harnessed.providers.types import ChatRequest, ChatResponse, ConsolidatedFact, Usage
from harnessed.tasks import lease, store
from support.raising_provider import RaisingProvider
from support.spies import CountingProvider

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "harnessed"
FIXTURES = shipped.fake_wire() / "_consolidate"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "248"

runner = CliRunner()

#: Episodes the threshold tests seed. Small, so a batch is cheap to read.
AFTER = 3

SMALL = "fake-small"


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def conn(clean_env: None, tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def settings(clean_env: None) -> config.Settings:
    return dataclasses.replace(config.load(), consolidate_after=AFTER, small_model=SMALL)


def seed(
    conn: sqlite3.Connection, count: int, *, text: str = "the operator prefers tea"
) -> list[str]:
    """``count`` episodes, oldest first, under one run id each."""
    return [
        episodic.put(conn, f"run_{number}", "loop", f"{text} ({number})").episode_id
        for number in range(count)
    ]


def reply(*facts: dict[str, object]) -> ChatResponse:
    return ChatResponse(
        response_id="scripted",
        model="scripted",
        content=json.dumps({"facts": list(facts)}),
        usage=Usage(input_tokens=11, output_tokens=7),
    )


def new_fact(text: str, path: str | None = None) -> dict[str, object]:
    return {
        "text": text,
        "actor": "operator",
        "path": path,
        "kind": "preference",
        "relation": "new",
        "replaces": None,
    }


def batches(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT batch_id, status, failure, attempts, episode_count, facts_written, "
        "facts_archived, model, input_tokens, output_tokens FROM consolidation_batches"
    ).fetchall()


def consolidated(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT episode_id FROM mem_consolidated")}


# ---- the ledger: migration 023 ------------------------------------------------------------------


def test_migration_023_creates_both_tables_and_the_source_index(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"consolidation_batches", "mem_consolidated"} <= tables
    indexes = {row[1]: row[0] for row in conn.execute("PRAGMA index_list(mem_semantic)")}
    assert "mem_semantic_source_idx" in indexes
    assert [row[2] for row in conn.execute("PRAGMA index_info(mem_semantic_source_idx)")] == [
        "source"
    ]
    shipped = db.load_migrations()
    assert (shipped[22].version, shipped[22].slug) == (23, "consolidation")
    assert (shipped[23].version, shipped[23].slug) == (24, "fact_link_relation")
    assert (shipped[24].version, shipped[24].slug) == (25, "permission_exchange")
    assert (shipped[25].version, shipped[25].slug) == (26, "v6_debts")
    assert (shipped[26].version, shipped[26].slug) == (27, "schedule_backends")
    assert db.schema_version(conn) == len(shipped) == 28
    assert [m.version for m in shipped if m.version >= 17] == list(range(17, 29))


def test_the_batch_row_carries_the_columns_decision_81_names(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(consolidation_batches)")}
    assert columns == {
        "batch_id", "session_id", "status", "failure", "attempts", "episode_count",
        "facts_written", "facts_archived", "model", "input_tokens", "output_tokens", "cost",
        "cost_nanos", "started_at", "finished_at", "holder_pid", "holder_started_at",
            "facts_skipped",
    }  # fmt: skip
    keyed = [row[1] for row in conn.execute("PRAGMA table_info(mem_consolidated)") if row[5]]
    assert keyed == ["episode_id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO consolidation_batches (batch_id, status, episode_count, started_at) "
            "VALUES ('bat_x', 'stuck', 1, 'now')"
        )


# ---- the batch id -------------------------------------------------------------------------------


def test_the_batch_id_is_bat_plus_sixteen_hex_of_the_sorted_ids_and_stable() -> None:
    episode_ids = ["epi_b", "epi_a", "epi_c"]
    canonical = json.dumps(sorted(episode_ids), sort_keys=True, separators=(",", ":"))
    expected = "bat_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    assert consolidation.batch_id(episode_ids) == expected
    assert consolidation.batch_id(list(reversed(episode_ids))) == expected
    assert consolidation.batch_id([*episode_ids, "epi_d"]) != expected
    assert ids.content_hash("bat", sorted(episode_ids)) == expected


# ---- the trigger --------------------------------------------------------------------------------


def test_below_the_threshold_nothing_runs_and_the_provider_is_never_asked(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER - 1)
    provider = CountingProvider([reply()])
    outcome = consolidation.maybe_consolidate(
        conn,
        provider=provider,
        settings=settings,
        bus_factory=lambda: EventBus(conn=conn),
        now=clock.now(),
    )
    assert outcome is None
    assert provider.calls == 0
    assert batches(conn) == []


def test_at_the_threshold_one_batch_runs_over_the_oldest_episodes(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    oldest = seed(conn, AFTER + 2)
    provider = CountingProvider([reply(new_fact("The operator prefers tea."))])
    outcome = consolidation.maybe_consolidate(
        conn,
        provider=provider,
        settings=settings,
        bus_factory=lambda: EventBus(conn=conn),
        now=clock.now(),
    )
    assert outcome is not None and outcome.status == "done"
    assert provider.calls == 1
    assert consolidated(conn) == set(oldest[:AFTER])
    assert outcome.batch_id == consolidation.batch_id(oldest[:AFTER])
    [row] = batches(conn)
    assert row[:6] == (outcome.batch_id, "done", None, 1, AFTER, 1)
    assert row[7:] == (SMALL, 11, 7)
    # Two episodes are still waiting: below the threshold, so a second trigger runs nothing.
    again = consolidation.maybe_consolidate(
        conn,
        provider=provider,
        settings=settings,
        bus_factory=lambda: EventBus(conn=conn),
        now=clock.now(),
    )
    assert again is None and provider.calls == 1


def test_the_default_threshold_is_twenty_and_the_variable_moves_it(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert config.DEFAULT_CONSOLIDATE_AFTER == 20
    assert config.load().consolidate_after == 20
    monkeypatch.setenv("HARNESSED_CONSOLIDATE_AFTER", "5")
    assert config.load().consolidate_after == 5


def test_a_done_batch_is_the_facts_written_by_source_and_tag(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    outcome = consolidation.consolidate(
        conn,
        provider=CountingProvider([reply(new_fact("The operator prefers tea."))]),
        settings=settings,
        now=clock.now(),
        size=AFTER,
    )
    assert outcome is not None
    source = f"{consolidation.SOURCE_PREFIX}{outcome.batch_id}"
    rows = conn.execute(
        "SELECT mem_id, text FROM mem_semantic WHERE source = ?", (source,)
    ).fetchall()
    assert [row[1] for row in rows] == ["The operator prefers tea."]
    fact = semantic.get(conn, rows[0][0])
    assert fact is not None and "consolidation" in fact.tags
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT mem_id FROM mem_semantic WHERE source = ?", (source,)
    ).fetchall()
    assert any("mem_semantic_source_idx" in str(step) for step in plan)


# ---- the one call -------------------------------------------------------------------------------


def test_the_call_is_one_chat_request_on_the_consolidate_scope_and_the_small_model(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    provider = CountingProvider([reply()])
    consolidation.consolidate(
        conn, provider=provider, settings=settings, now=clock.now(), size=AFTER
    )
    [request] = provider.seen
    assert isinstance(request, ChatRequest)
    assert request.metadata == {"scope": "_consolidate"}
    assert request.model == SMALL
    assert request.system == consolidation.INSTRUCTION
    assert request.tools == ()
    assert len(request.messages) == 1 and request.messages[0].role == "user"
    assert consolidation.TIMEOUT_S == 60


def test_the_user_turn_carries_scrubbed_cut_episodes_oldest_first_and_the_hits_with_ids(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    secret = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDDEEEEFFFF"
    first = episodic.put(conn, "run_a", "loop", "alpha opening " + "x" * 3000 + " ZZEND")
    second = episodic.put(conn, "run_b", "loop", f"beta uses {secret}")
    third = episodic.put(conn, "run_c", "loop", "gamma closing")
    hits = [semantic.put(conn, f"alpha fact number {n}", "operator") for n in range(12)]
    provider = CountingProvider([reply()])
    consolidation.consolidate(conn, provider=provider, settings=settings, now=clock.now(), size=3)
    content = provider.seen[0].messages[0].content

    assert (
        content.index("alpha opening") < content.index("beta uses") < content.index("gamma closing")
    )
    assert "ZZEND" not in content, "an episode is cut to 2,000 characters"
    assert "x" * (consolidation.EPISODE_CHARS + 1) not in content
    assert secret not in content, "an episode is scrubbed before it leaves"
    shown = [fact.mem_id for fact in hits if fact.mem_id in content]
    assert len(shown) == consolidation.TOP_K == 10
    assert {first.episode_id, second.episode_id, third.episode_id}.isdisjoint(set(content.split()))


def test_an_agent_hop_episode_names_the_entry_that_did_the_work_and_its_directory(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    episodic.put(conn, "run_hop", "delegation", '{"agent":"scripted-agent","status":"succeeded"}')
    episodic.put(conn, "run_plain", "loop", "a plain loop episode")
    db.write(
        conn,
        (
            (
                "INSERT INTO delegations (task_hash, agent, cwd, task, acceptance, status, "
                "started_at) VALUES ('th1', 'scripted-agent', '/tmp/scratch-repo', 't', 'a', "
                "'succeeded', '2026-10-10T00:00:00Z')",
                (),
            ),
            (
                "INSERT INTO agent_turns (run_id, idx, hop, task_hash, entry, source, response, "
                "complete, created_at) VALUES ('run_hop', 0, 0, 'th1', 'scripted-agent', 'acp', "
                "'r', 1, '2026-10-10T00:00:00Z')",
                (),
            ),
        ),
    )
    provider = CountingProvider([reply()])
    consolidation.consolidate(conn, provider=provider, settings=settings, now=clock.now(), size=2)
    content = provider.seen[0].messages[0].content

    assert "(done by agent scripted-agent, cwd /tmp/scratch-repo) " in content
    assert content.count("done by") == 1, "only the agent hop carries the prefix"
    assert "done the work" in consolidation.INSTRUCTION


def test_no_procedure_enters_the_request(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    procedural.put(conn, "ALWAYS-ANSWER-IN-FRENCH-PROCEDURE", "seed")
    seed(conn, AFTER)
    provider = CountingProvider([reply()])
    consolidation.consolidate(
        conn, provider=provider, settings=settings, now=clock.now(), size=AFTER
    )
    request = provider.seen[0]
    assert "FRENCH-PROCEDURE" not in request.system + request.messages[0].content


# ---- the reply ----------------------------------------------------------------------------------


def test_consolidation_facts_parses_the_documented_schema() -> None:
    text = json.dumps(
        {
            "facts": [
                new_fact("A"),
                {**new_fact(" B "), "relation": "contradicts", "replaces": "mem_1"},
            ]
        }
    )
    assert translate.consolidation_facts(text, known={"mem_1"}) == (
        ConsolidatedFact("A", "operator", None, "preference", "new", None),
        ConsolidatedFact("B", "operator", None, "preference", "contradicts", "mem_1"),
    )
    assert translate.consolidation_facts('{"facts": []}') == ()


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[]",
        '{"facts": "no"}',
        '{"fact": []}',
        '{"facts": [1]}',
        json.dumps({"facts": [{**new_fact("x"), "text": ""}]}),
        json.dumps({"facts": [{**new_fact("x"), "text": "   "}]}),
        json.dumps({"facts": [{**new_fact("x"), "actor": ""}]}),
        json.dumps({"facts": [{**new_fact("x"), "path": 3}]}),
        json.dumps({"facts": [{k: v for k, v in new_fact("x").items() if k != "actor"}]}),
        json.dumps({"facts": [{**new_fact("x"), "extra": 1}]}),
        json.dumps({"facts": [{**new_fact("x"), "kind": "maybe"}]}),
        json.dumps({"facts": [{**new_fact("x"), "relation": "maybe"}]}),
        json.dumps({"facts": [{**new_fact("x"), "relation": "contradicts"}]}),
        json.dumps({"facts": [{**new_fact("x"), "replaces": "mem_1"}]}),
        json.dumps({"facts": [{**new_fact("x"), "relation": "contradicts", "replaces": 7}]}),
        json.dumps(
            {"facts": [{**new_fact("x"), "relation": "contradicts", "replaces": "mem_unseen"}]}
        ),
    ],
)
def test_any_other_shape_is_refused(bad: str) -> None:
    with pytest.raises(TranslationError):
        translate.consolidation_facts(bad, known={"mem_1"})


def test_a_replaces_naming_no_hit_in_the_prompt_settles_the_batch_unparseable(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    bad = {**new_fact("x"), "relation": "contradicts", "replaces": "mem_nowhere"}
    outcome = consolidation.consolidate(
        conn,
        provider=CountingProvider([reply(bad)]),
        settings=settings,
        now=clock.now(),
        size=AFTER,
    )
    assert outcome is not None and (outcome.status, outcome.failure) == ("failed", "unparseable")
    assert batches(conn)[0][1:3] == ("failed", "unparseable")
    assert consolidated(conn) == set()
    assert conn.execute("SELECT COUNT(*) FROM mem_semantic").fetchone()[0] == 0


def test_a_contradiction_archives_the_old_fact_and_links_the_new_one(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER, text="the operator prefers coffee")
    old = semantic.put(conn, "the operator prefers coffee", "operator")
    contradiction = {
        **new_fact("The operator now prefers tea."),
        "relation": "contradicts",
        "replaces": old.mem_id,
    }
    outcome = consolidation.consolidate(
        conn, provider=CountingProvider([reply(contradiction)]), settings=settings,
        now=clock.now(), size=AFTER,
    )  # fmt: skip
    assert outcome is not None and outcome.archived == 1
    kept = semantic.get(conn, old.mem_id)
    assert kept is not None and kept.status == semantic.ARCHIVED, "archived, never deleted"
    [new] = outcome.facts
    assert new.links == (old.mem_id,)
    assert batches(conn)[0][5:7] == (1, 1)
    link = conn.execute(
        "SELECT mem_id, to_id, relation, reason FROM mem_fact_links WHERE mem_id = ?", (new.mem_id,)
    ).fetchone()
    assert tuple(link) == (new.mem_id, old.mem_id, "contradicts", outcome.batch_id)
    capture(
        "contradiction.txt",
        f"archived {old.mem_id} status={kept.status}\nlink {tuple(link)}\n",
    )


@pytest.mark.parametrize("error", [ProviderError])
def test_a_provider_error_settles_the_batch_failed_and_writes_nothing(
    conn: sqlite3.Connection, settings: config.Settings, error: type[ProviderError]
) -> None:
    seed(conn, AFTER)
    provider = RaisingProvider(error=error)
    outcome = consolidation.consolidate(
        conn, provider=provider, settings=settings, now=clock.now(), size=AFTER
    )
    assert outcome is not None and outcome.failure == "provider_error" and provider.calls == 1
    assert consolidated(conn) == set()


def test_a_call_past_the_timeout_settles_the_batch_timeout(
    conn: sqlite3.Connection, settings: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(conn, AFTER)
    release = threading.Event()

    class Slow:
        def complete(self, request: ChatRequest) -> ChatResponse:
            release.wait(5)
            return reply()

    monkeypatch.setattr(consolidation, "TIMEOUT_S", 0.05)
    try:
        outcome = consolidation.consolidate(
            conn, provider=Slow(), settings=settings, now=clock.now(), size=AFTER
        )
    finally:
        release.set()
    assert outcome is not None and outcome.failure == "timeout"
    assert batches(conn)[0][1:3] == ("failed", "timeout")


# ---- ticket 248: fail closed, resume, in flight, facts only -------------------------------------


def _fact_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM mem_semantic").fetchone()[0])


@pytest.mark.parametrize("failure", ["provider_error", "timeout", "unparseable"])
def test_each_failure_fails_closed_and_the_episodes_stay_for_the_next_batch(
    conn: sqlite3.Connection,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    ids_ = seed(conn, AFTER)
    release = threading.Event()

    class Provider:
        def complete(self, request: ChatRequest) -> ChatResponse:
            if failure == "provider_error":
                raise ProviderError("boom")
            if failure == "timeout":
                release.wait(5)
            return ChatResponse(
                response_id="x", model="x", content="not json", usage=Usage(input_tokens=1)
            )

    monkeypatch.setattr(consolidation, "TIMEOUT_S", 0.05)
    try:
        outcome = consolidation.consolidate(
            conn, provider=Provider(), settings=settings, now=clock.now(), size=AFTER
        )
    finally:
        release.set()
    assert outcome is not None and outcome.status == "failed" and outcome.failure == failure
    assert _fact_count(conn) == 0 and consolidated(conn) == set()
    assert batches(conn)[0][1:3] == ("failed", failure)
    assert [e.episode_id for e in episodic.unconsolidated(conn, AFTER)] == ids_
    capture(f"closed-{failure}.txt", f"{failure}: facts 0, marked 0, batch {batches(conn)[0]}\n")


def test_a_failed_batch_retries_with_the_same_id_and_succeeds(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    first = consolidation.consolidate(
        conn, provider=RaisingProvider(error=ProviderError), settings=settings,
        now=clock.now(), size=AFTER,
    )  # fmt: skip
    second = consolidation.consolidate(
        conn, provider=CountingProvider([reply(new_fact("The operator likes tea."))]),
        settings=settings, now=clock.now(), size=AFTER,
    )  # fmt: skip
    assert first is not None and second is not None and first.batch_id == second.batch_id
    assert second.status == "done" and batches(conn)[0][3] == 2


def test_facts_are_written_before_the_episodes_are_marked_and_with_source_and_tag(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    seen: dict[str, object] = {}

    def between() -> None:
        seen["facts"] = _fact_count(conn)
        seen["marked"] = len(consolidated(conn))
        seen["status"] = batches(conn)[0][1]

    outcome = consolidation.consolidate(
        conn, provider=CountingProvider([reply(new_fact("a"), new_fact("b"))]),
        settings=settings, now=clock.now(), size=AFTER, after_facts=between,
    )  # fmt: skip
    assert seen == {"facts": 2, "marked": 0, "status": "running"}
    assert outcome is not None and outcome.status == "done"
    assert all(
        f.source == f"consolidation:{outcome.batch_id}" and "consolidation" in f.tags
        for f in outcome.facts
    )
    assert batches(conn)[0][5] == 2 and len(consolidated(conn)) == AFTER


class Crash(RuntimeError):
    pass


def _crash() -> None:
    raise Crash


def test_a_crash_between_the_facts_and_the_marks_resumes_with_0_calls_and_0_duplicates(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    first = CountingProvider([reply(new_fact("The operator likes tea."), new_fact("Uses vim."))])
    with pytest.raises(Crash):
        consolidation.consolidate(
            conn, provider=first, settings=settings, now=clock.now(), size=AFTER,
            after_facts=_crash,
        )  # fmt: skip
    assert _fact_count(conn) == 2 and consolidated(conn) == set()
    # The crashed pass was this process; a real crash is a dead holder.
    conn.execute("UPDATE consolidation_batches SET holder_pid = 2147483646")
    conn.commit()
    second = CountingProvider([reply()])
    outcome = consolidation.consolidate(
        conn, provider=second, settings=settings, now=clock.now(), size=AFTER
    )
    assert outcome is not None and outcome.status == "done" and outcome.calls == 0
    assert len(second.seen) == 0 and len(first.seen) == 1
    assert _fact_count(conn) == 2, "0 duplicate facts"
    assert len(consolidated(conn)) == AFTER
    assert batches(conn)[0][1] == "done" and batches(conn)[0][5] == 2
    assert len(outcome.facts) == 2
    capture(
        "resume.txt",
        f"model calls first pass {len(first.seen)}, resume {len(second.seen)}; "
        f"facts {_fact_count(conn)}\n",
    )


def test_resume_finds_the_facts_by_the_source_index(conn: sqlite3.Connection) -> None:
    semantic.put(conn, "x", "consolidation:bat_1")
    plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + semantic._BY_SOURCE, ("a",))]
    assert any("mem_semantic_source_idx" in step for step in plan)
    assert [f.text for f in semantic.by_source(conn, "consolidation:bat_1")] == ["x"]


def test_a_running_batch_under_a_live_process_is_skipped(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    chosen = consolidation.plan(conn, AFTER)
    assert chosen is not None
    mine = proc.identity(os.getpid())
    assert mine is not None
    conn.execute(
        "INSERT INTO consolidation_batches (batch_id, status, attempts, episode_count, started_at,"
        " holder_pid, holder_started_at) VALUES (?, 'running', 1, ?, 'now', ?, ?)",
        (chosen.batch_id, AFTER, mine.pid, mine.started_at),
    )
    conn.commit()
    provider = CountingProvider([reply()])
    outcome = consolidation.consolidate(
        conn, provider=provider, settings=settings, now=clock.now(), size=AFTER
    )
    assert outcome is not None and outcome.status == "skipped" and provider.seen == []
    assert batches(conn)[0][1] == "running"


def test_a_running_batch_whose_holder_died_is_taken_over(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    chosen = consolidation.plan(conn, AFTER)
    assert chosen is not None
    conn.execute(
        "INSERT INTO consolidation_batches (batch_id, status, attempts, episode_count, started_at,"
        " holder_pid) VALUES (?, 'running', 1, ?, 'now', 2147483646)",
        (chosen.batch_id, AFTER),
    )
    conn.commit()
    outcome = consolidation.consolidate(
        conn, provider=CountingProvider([reply(new_fact("a"))]), settings=settings,
        now=clock.now(), size=AFTER,
    )  # fmt: skip
    assert outcome is not None and outcome.status == "done"


def test_a_done_batch_has_nothing_to_do(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    consolidation.consolidate(
        conn, provider=CountingProvider([reply()]), settings=settings, now=clock.now(), size=AFTER
    )
    assert consolidation.plan(conn, AFTER) is None
    provider = CountingProvider([reply()])
    assert (
        consolidation.consolidate(
            conn, provider=provider, settings=settings, now=clock.now(), size=AFTER
        )
        is None
    )
    assert provider.seen == []


def test_a_restatement_is_absent_from_the_reply_and_writes_nothing(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    semantic.put(conn, "the operator prefers tea", "operator")
    outcome = consolidation.consolidate(
        conn, provider=CountingProvider([reply()]), settings=settings, now=clock.now(), size=AFTER
    )
    assert outcome is not None and outcome.status == "done" and outcome.facts == ()
    assert _fact_count(conn) == 1 and len(consolidated(conn)) == AFTER


def test_a_new_fact_is_written_and_a_contradiction_never_deletes(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER, text="the operator prefers coffee")
    old = semantic.put(conn, "the operator prefers coffee", "operator")
    outcome = consolidation.consolidate(
        conn,
        provider=CountingProvider(
            [
                reply(
                    new_fact("Works in Lisbon."),
                    {
                        **new_fact("Now prefers tea."),
                        "relation": "contradicts",
                        "replaces": old.mem_id,
                    },
                )
            ]
        ),
        settings=settings, now=clock.now(), size=AFTER,
    )  # fmt: skip
    assert outcome is not None and len(outcome.facts) == 2 and outcome.archived == 1
    assert _fact_count(conn) == 3
    assert [f.mem_id for f in semantic.search(conn, "coffee", include_archived=True)] == [
        old.mem_id
    ]


# ---- offline: FakeProvider and the socket guard -------------------------------------------------


def test_the_consolidate_scope_is_committed_and_served_by_fakeprovider(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    assert (FIXTURES / "_index.yaml").is_file() and (FIXTURES / "base.json").is_file()
    assert "_consolidate" in FakeProvider(settings).scopes
    seed(conn, AFTER)
    opened: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        opened.append(args)
        raise AssertionError("a consolidation batch opened a socket")

    real = socket.socket.connect
    socket.socket.connect = refuse  # type: ignore[method-assign,assignment]
    try:
        outcome = consolidation.consolidate(
            conn, provider=FakeProvider(settings), settings=settings, now=clock.now(), size=AFTER
        )
    finally:
        socket.socket.connect = real  # type: ignore[method-assign]
    assert opened == []
    assert outcome is not None and outcome.status == "done"
    assert [fact.text for fact in outcome.facts] == ["The operator prefers British spelling."]
    capture(
        "batch.txt",
        f"batch {outcome.batch_id} over {len(outcome.episode_ids)} episodes\n"
        + "".join(f"fact {fact.mem_id} {fact.text}\n" for fact in outcome.facts),
    )


def test_twenty_seeded_episodes_make_one_batch_offline(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    conn = db.connect()
    try:
        seed(conn, 20)
        settings = config.load()
        assert settings.consolidate_after == 20
        outcome = consolidation.maybe_consolidate(
            conn,
            provider=FakeProvider(settings),
            settings=dataclasses.replace(settings, small_model=SMALL),
            bus_factory=lambda: EventBus(conn=conn),
            now=clock.now(),
        )
        assert outcome is not None and outcome.status == "done" and len(outcome.episode_ids) == 20
        assert len(consolidated(conn)) == 20
        capture("batch-of-twenty.txt", f"{outcome.batch_id}: {len(outcome.facts)} fact(s)\n")
    finally:
        conn.close()


# ---- the three doors ----------------------------------------------------------------------------


class Spy:
    """Wrap ``consolidation.maybe_consolidate``: record what the world looked like at the call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        self.home = home
        self.seen: list[dict[str, Any]] = []
        self.real = consolidation.maybe_consolidate
        monkeypatch.setattr(consolidation, "maybe_consolidate", self)

    def __call__(self, conn: sqlite3.Connection, **kwargs: Any) -> Any:
        finished = conn.execute(
            "SELECT COUNT(*) FROM events WHERE name = 'run.finished'"
        ).fetchone()
        self.seen.append(
            {
                "finished": finished[0],
                "lease": lease.holder(conn),
                "thread": threading.current_thread().name,
            }
        )
        return self.real(conn, **kwargs)

    def wait(self, count: int = 1) -> None:
        deadline = time.monotonic() + 15
        while len(self.seen) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(self.seen) >= count, "the trigger was never called"


@pytest.fixture
def door(clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch) -> Spy:
    monkeypatch.setenv("HARNESSED_CONSOLIDATE_AFTER", "1")
    db.connect().close()
    return Spy(monkeypatch, tmp_harnessed_home)


def test_run_calls_the_trigger_after_the_reply_is_printed_and_the_lease_is_released(
    door: Spy,
) -> None:
    printed: list[int] = []
    real_reply = run_cmd.render.reply

    def reply_then_note(*args: Any, **kwargs: Any) -> None:
        real_reply(*args, **kwargs)
        printed.append(len(door.seen))

    run_cmd.render.reply = reply_then_note  # type: ignore[assignment]
    try:
        result = runner.invoke(app, ["run", "ping"])
    finally:
        run_cmd.render.reply = real_reply  # type: ignore[assignment]
    assert result.exit_code == 0, result.output
    assert printed == [0], "the reply was printed before the trigger was called"
    [seen] = door.seen
    assert seen["finished"] >= 1, "run.finished was already published"
    assert seen["lease"] is None, "the run lease was already released"
    conn = db.connect()
    try:
        assert [row[1] for row in batches(conn)] == ["done"], "and the batch really ran"
    finally:
        conn.close()


def test_the_control_worker_runs_the_trigger_as_its_own_job_after_the_run_future(
    door: Spy,
) -> None:
    with TestClient(control.create_app(door.home), raise_server_exceptions=False) as client:
        response = client.post("/control/v1/runs", json={"prompt": "ping"})
        assert response.status_code == 202, response.text
        in_flight = client.app.state.worker.run  # type: ignore[attr-defined]
        in_flight.result(timeout=15)
        door.wait()
        [seen] = door.seen
        assert in_flight.done()
        assert seen["finished"] >= 1 and seen["lease"] is None
        assert seen["thread"].startswith("harnessed-control")


def test_task_run_calls_the_trigger_after_each_checkpoint(
    door: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, ScriptedDecider([answer("groq_chat"), answer("done")]), CannedProvider())
    conn = db.connect()
    try:
        row = store.queue(conn, "ping", via="cli")
    finally:
        conn.close()
    task_run.run_task(str(row["task_id"]))
    assert len(door.seen) >= 1
    assert all(seen["finished"] >= 1 and seen["lease"] is None for seen in door.seen)


# ---- 0 calls from eval run and improve run ------------------------------------------------------


def test_eval_run_and_improve_run_make_0_consolidation_calls(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    for name in ("maybe_consolidate", "consolidate"):
        real = getattr(consolidation, name)

        def spy(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(consolidation, name, spy)
    real_complete = FakeProvider.complete

    def watched(self: FakeProvider, request: ChatRequest) -> ChatResponse:
        if request.metadata.get("scope") == "_consolidate":
            calls.append("request")
        return real_complete(self, request)

    monkeypatch.setattr(FakeProvider, "complete", watched)
    ran = runner.invoke(app, ["eval", "run", "smoke", "--fresh"])
    improved = runner.invoke(app, ["improve", "run", "--dry-run"])
    assert ran.exit_code in (0, 1), ran.output
    assert improved.exit_code in (0, 1), improved.output
    assert calls == []
    capture(
        "zero-calls.txt",
        f"eval run exit {ran.exit_code}, improve run exit {improved.exit_code}\ncalls: {calls}\n",
    )


def _names_in(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
    return names


def test_nothing_in_loop_evals_or_improve_names_consolidation() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for area in ("loop", "evals", "improve")
        for path in sorted((SRC / area).rglob("*.py"))
        if {"consolidation", "maybe_consolidate", "consolidate"} & _names_in(path)
    ]
    assert offenders == []


def test_consolidation_is_facts_only_and_reaches_no_procedure_or_improvement() -> None:
    tree = ast.parse((SRC / "memory" / "consolidation.py").read_text(encoding="utf-8"))
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            reached.add(node.module)
            reached.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            reached.update(alias.name for alias in node.names)
    assert not {name for name in reached if "procedural" in name or "improve" in name}
    assert {"harnessed.memory.semantic", "harnessed.memory.episodic"} <= reached


# ---- the hand verb ------------------------------------------------------------------------------


def test_memory_consolidate_dry_run_prints_the_selection_and_makes_0_calls(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect()
    oldest = seed(conn, 5)
    conn.close()
    calls: list[object] = []
    monkeypatch.setattr(FakeProvider, "complete", lambda self, request: calls.append(request))
    built: list[object] = []
    monkeypatch.setattr("harnessed.cli.memory_cmd.build_provider", lambda s: built.append(s))
    result = runner.invoke(
        app, ["memory", "consolidate", "--batch-size", "3", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "batch_id": consolidation.batch_id(oldest[:3]),
        "status": "dry_run",
        "episodes": oldest[:3],
    }
    assert calls == [] and built == []
    conn = db.connect()
    try:
        assert batches(conn) == []
    finally:
        conn.close()
    text = runner.invoke(app, ["memory", "consolidate", "--batch-size", "3", "--dry-run"])
    assert consolidation.batch_id(oldest[:3]) in text.output
    capture("dry-run.txt", text.output)


def test_memory_consolidate_runs_the_same_pass_by_hand(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    conn = db.connect()
    oldest = seed(conn, 5)
    conn.close()
    result = runner.invoke(app, ["memory", "consolidate", "--batch-size", "4", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "done"
    assert payload["batch_id"] == consolidation.batch_id(oldest[:4])
    assert payload["facts_archived"] == "0" and payload["facts_skipped"] == "0"
    assert [fact["text"] for fact in payload["facts"]] == ["The operator prefers British spelling."]
    conn = db.connect()
    try:
        assert consolidated(conn) == set(oldest[:4])
    finally:
        conn.close()
    plain = runner.invoke(app, ["memory", "consolidate", "--batch-size", "4"])
    assert plain.exit_code == 0 and "episode" in plain.output


def test_memory_consolidate_with_nothing_waiting_says_so(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    db.connect().close()
    result = runner.invoke(app, ["memory", "consolidate", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"batch_id": None, "status": "empty", "episodes": []}


# ---- ticket 249: the batch says when it ran -----------------------------------------------------


def events_of(conn: sqlite3.Connection) -> list[tuple[str, int, str, dict[str, Any]]]:
    return [
        (row[0], row[1], row[2], json.loads(row[3]))
        for row in conn.execute(
            "SELECT run_id, seq, name, payload FROM events WHERE name LIKE 'memory.consolidation.%'"
        )
    ]


def test_a_settled_batch_publishes_one_event_as_the_only_event_of_its_own_session(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    trigger = "run_00000000000000000TRIGGERED"
    run = EventBus(run_id=trigger, conn=conn)
    run.publish("run.finished", {"stop_reason": "end_turn"})
    outcome = consolidation.maybe_consolidate(
        conn,
        provider=CountingProvider([reply(new_fact("The operator prefers tea."))]),
        settings=settings,
        bus_factory=lambda: EventBus(conn=conn),
        now=clock.now(),
        session_id=trigger,
    )
    assert outcome is not None and outcome.status == "done"
    [(session, seq, name, payload)] = events_of(conn)
    assert (name, seq) == ("memory.consolidation.finished", 1)
    assert session != trigger and session.startswith("run_")
    assert payload == {
        "batch_id": outcome.batch_id,
        "status": "done",
        "episode_count": AFTER,
        "facts_written": 1,
        "facts_archived": "0",
        "facts_skipped": "0",
        "cost": None,
        "failure": None,
        "session_id": trigger,
    }
    # The triggering session gained nothing after its own run.finished.
    assert [
        row[0] for row in conn.execute("SELECT name FROM events WHERE run_id = ?", (trigger,))
    ] == ["run.finished"]
    # Its one event is the session's start and finish both: the pair is self-paired.
    assert read.terminal_for(conn, session) == "memory.consolidation.finished"
    assert read.TERMINAL_PAIRS["memory.consolidation.finished"] == "memory.consolidation.finished"


def test_a_failed_batch_publishes_its_event_too_and_a_skipped_one_does_not(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    failing = RaisingProvider()
    outcome = consolidation.consolidate(
        conn,
        provider=failing,
        settings=settings,
        now=clock.now(),
        size=AFTER,
        bus_factory=lambda: EventBus(conn=conn),
    )
    assert outcome is not None and outcome.status == "failed"
    [(_, _, _, payload)] = events_of(conn)
    assert (payload["status"], payload["failure"], payload["facts_written"]) == (
        "failed",
        "provider_error",
        0,
    )
    assert payload["session_id"] is None
    # A pass that finds its batch already done is skipped, and says nothing a second time.
    consolidation.consolidate(
        conn,
        provider=CountingProvider([reply(new_fact("The operator prefers tea."))]),
        settings=settings,
        now=clock.now(),
        size=AFTER,
        bus_factory=lambda: EventBus(conn=conn),
    )
    assert len(events_of(conn)) == 2  # the retry settled done: one more, not a third
    third = consolidation.consolidate(
        conn,
        provider=failing,
        settings=settings,
        now=clock.now(),
        size=AFTER,
        bus_factory=lambda: EventBus(conn=conn),
    )
    assert third is None  # nothing waits; the batch is consumed
    assert len(events_of(conn)) == 2


def test_the_read_serves_every_batch_newest_first_with_its_facts_by_source(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER * 2)
    consolidation.consolidate(
        conn,
        provider=CountingProvider([reply(new_fact("The operator prefers tea."))]),
        settings=settings,
        now=clock.now(),
        size=AFTER,
        session_id="run_x",
    )
    consolidation.consolidate(
        conn, provider=RaisingProvider(), settings=settings, now=clock.now(), size=AFTER
    )
    seed(conn, 1)
    payload = consolidation.listing_payload(consolidation.listing(conn, AFTER))
    assert payload["consolidate_after"] == str(AFTER) and payload["unconsolidated"] == "4"
    batches_served = payload["batches"]
    assert isinstance(batches_served, list)
    assert [row["status"] for row in batches_served] == ["failed", "done"]
    failed, done = batches_served
    assert failed["facts"] == [] and failed["failure"] == "provider_error"
    assert failed["failure_reason"] is None and failed["session_id"] is None
    assert failed["session_id_reason"] == "run by hand, not triggered by a session"
    assert [fact["text"] for fact in done["facts"]] == ["The operator prefers tea."]
    assert done["failure"] is None and done["failure_reason"] == "the batch did not fail"
    for row in batches_served:
        for name, value in row.items():
            assert name in {"facts", "diagnostics"} or value is None or isinstance(value, str), name


def test_the_listing_route_and_the_cli_serve_the_same_bytes(
    conn: sqlite3.Connection, settings: config.Settings, tmp_harnessed_home: Path
) -> None:
    seed(conn, AFTER)
    consolidation.consolidate(
        conn,
        provider=CountingProvider([reply(new_fact("The operator prefers tea."))]),
        settings=settings,
        now=clock.now(),
        size=AFTER,
    )
    from harnessed.gateway import create_app

    with TestClient(create_app(tmp_harnessed_home / db.DATABASE_FILENAME)) as client:
        served = client.get("/v1/memory/consolidation")
    assert served.status_code == 200
    printed = runner.invoke(app, ["memory", "consolidate", "--list", "--json"])
    assert printed.exit_code == 0, printed.output
    assert json.loads(printed.output) == served.json()
    [batch] = served.json()["batches"]
    assert batch["facts_skipped"] == "0" and batch["facts_archived"] == "0"
    plain = runner.invoke(app, ["memory", "consolidate", "--list"])
    assert plain.exit_code == 0 and "batch(es)" in plain.output


# ---- ticket 274: only the operator and the work, once -------------------------------------------


def test_the_instruction_is_the_job_and_the_fields_meanings() -> None:
    text = consolidation.INSTRUCTION
    assert "about the operator" in text and "projects and setup" in text and "decision" in text
    for field in ("text", "actor", "path", "kind", "relation", "replaces"):
        assert f"{field} " in text
    assert len(text) < 1100, "the instruction shrank to the job and the fields' meanings"


def _run_reply(
    conn: sqlite3.Connection, settings: config.Settings, *facts: dict[str, object]
) -> consolidation.Outcome:
    seed(conn, AFTER)
    outcome = consolidation.consolidate(
        conn,
        provider=CountingProvider([reply(*facts)]),
        settings=settings,
        now=clock.now(),
        size=AFTER,
        bus_factory=lambda: EventBus(conn=conn),
    )
    assert outcome is not None
    return outcome


def test_a_repeat_of_an_active_fact_or_of_the_same_reply_is_skipped_and_counted(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    semantic.put(conn, "Unit tests help catch bugs early.", "seed")
    outcome = _run_reply(
        conn,
        settings,
        new_fact("  unit TESTS help   catch bugs early. "),
        new_fact("The operator prefers tea."),
        new_fact("the operator prefers  TEA."),
    )
    assert [fact.text for fact in outcome.facts] == ["The operator prefers tea."]
    assert outcome.skipped == 2
    [(_, _, _, payload)] = events_of(conn)
    assert payload["facts_skipped"] == "2" and payload["facts_written"] == 1


def test_a_fact_equal_to_a_procedure_is_skipped_and_counted(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    procedural.put(conn, "Respond with a single JSON object.", "seed")
    outcome = _run_reply(
        conn,
        settings,
        new_fact("respond with a SINGLE json object."),
        new_fact("The operator prefers tea."),
    )
    assert [fact.text for fact in outcome.facts] == ["The operator prefers tea."]
    assert outcome.skipped == 1
    assert events_of(conn)[0][3]["facts_skipped"] == "1"


# ---- ticket 283: every fact about a repository names it, and stands on its own ------------------

PLACE = "/tmp/hqa6-repo"


def _run_place(
    conn: sqlite3.Connection, settings: config.Settings, *facts: dict[str, object]
) -> consolidation.Outcome:
    seed(conn, AFTER, text=f"the operator works in {PLACE}")
    outcome = consolidation.consolidate(
        conn,
        provider=CountingProvider([reply(*facts)]),
        settings=settings,
        now=clock.now(),
        size=AFTER,
        bus_factory=lambda: EventBus(conn=conn),
    )
    assert outcome is not None
    return outcome


def test_the_instruction_says_each_fact_is_read_alone() -> None:
    assert "read alone" in consolidation.INSTRUCTION


def test_a_fact_with_the_path_is_kept_and_one_without_it_is_skipped_and_counted(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    outcome = _run_place(
        conn,
        settings,
        new_fact(f"The operator keeps the project in the repository at {PLACE}.", PLACE),
        new_fact("The operator commits to README.md in the same repository."),
        new_fact("The operator appends a line to README.md.", "README.md"),
        new_fact("The operator prefers tea."),
    )
    assert [fact.text for fact in outcome.facts] == [
        f"The operator keeps the project in the repository at {PLACE}.",
        "The operator prefers tea.",
    ]
    assert outcome.skipped == 2
    assert events_of(conn)[0][3]["facts_skipped"] == "2"


def test_a_bare_name_from_text_is_not_a_place_but_a_folded_path_matches(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    outcome = _run_place(
        conn,
        settings,
        new_fact("README.md in hqa6-repo lists the setup.", "README.md"),
        new_fact("The repository at C:\\tmp\\hqa6-repo uses uv.", PLACE.upper()),
    )
    assert outcome.skipped == 1
    assert [fact.text for fact in outcome.facts] == [
        "The repository at C:\\tmp\\hqa6-repo uses uv."
    ]


def test_a_fact_with_no_file_or_repository_is_not_touched_by_the_check(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    outcome = _run_place(
        conn, settings, new_fact("The operator prefers Python 3.12 and short replies.")
    )
    assert outcome.skipped == 0 and len(outcome.facts) == 1


def test_the_directory_of_an_agent_hop_counts_as_a_place_of_the_batch(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    episodic.put(conn, "run_hop", "delegation", '{"agent":"scripted-agent"}')
    db.write(
        conn,
        (
            (
                "INSERT INTO delegations (task_hash, agent, cwd, task, acceptance, status, "
                "started_at) VALUES ('th9', 'scripted-agent', '/tmp/scratch-repo', 't', 'a', "
                "'succeeded', '2026-10-10T00:00:00Z')",
                (),
            ),
            (
                "INSERT INTO agent_turns (run_id, idx, hop, task_hash, entry, source, response, "
                "complete, created_at) VALUES ('run_hop', 0, 0, 'th9', 'scripted-agent', 'acp', "
                "'r', 1, '2026-10-10T00:00:00Z')",
                (),
            ),
        ),
    )
    provider = CountingProvider(
        [
            reply(
                new_fact("The operator runs scripted-agent in /tmp/scratch-repo."),
                new_fact("The operator edits notes.txt."),
            )
        ]
    )
    outcome = consolidation.consolidate(
        conn, provider=provider, settings=settings, now=clock.now(), size=1
    )
    assert outcome is not None and outcome.skipped == 1 and len(outcome.facts) == 1


# ---- ticket 289: a place is a real path or a hop's directory, and the skip count is served -------

PLACES_FIXTURE = Path(__file__).parent / "fixtures" / "consolidation" / "places-qa-round4.json"


def test_the_quoted_patterns_are_decision_99s_exactly() -> None:
    assert consolidation._ABSOLUTE.pattern == (
        r"(?<![\w./\\-])(?:[A-Za-z]:[\\/]|/(?=[^\s/]+/))[^\s\"'`<>|,;()]+"
    )
    assert consolidation._RELATIVE_FILE.pattern == (
        r"(?<![\w./\\:-])(?:[\w.-]+[\\/])+[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7}\b"
    )


def test_qa_round_4_text_yields_no_fake_places_and_keeps_the_real_ones(
    conn: sqlite3.Connection,
) -> None:
    fixture = json.loads(PLACES_FIXTURE.read_text(encoding="utf-8"))
    episodes = []
    for index, item in enumerate(fixture["episodes"]):
        run_id = f"run_qa{index}"
        episodes.append(episodic.put(conn, run_id, item["kind"], item["text"]))
        if item["kind"] == "delegation":
            db.write(
                conn,
                (
                    (
                        "INSERT INTO delegations (task_hash, agent, cwd, task, acceptance, "
                        "status, started_at) VALUES (?, 'scripted-agent', ?, 't', 'a', "
                        "'succeeded', "
                        "'2026-10-06T00:00:00Z')",
                        (f"th{index}", fixture["cwd"]),
                    ),
                    (
                        "INSERT INTO agent_turns (run_id, idx, hop, task_hash, entry, source, "
                        "response, complete, created_at) VALUES (?, 0, 0, ?, 'scripted-agent', "
                        "'acp', 'r', 1, '2026-10-06T00:00:00Z')",
                        (run_id, f"th{index}"),
                    ),
                ),
            )
    places = set(consolidation._places(conn, tuple(episodes)))
    for fake in ("design", "png", "svg", "status_update", "/design", "/png", "/svg"):
        assert fake not in places
    assert "hqa6-repo" in places
    assert "src/harnessed/x.py" in places
    assert "x.py" not in places


def test_a_token_from_text_adds_only_itself_never_its_last_segment(
    conn: sqlite3.Connection,
) -> None:
    text = r"see C:\work\proj\notes and /opt/app/bin/tool and docs/guide.md but image/png is not"
    episode = episodic.put(conn, "run_t", "message", text)
    places = set(consolidation._places(conn, (episode,)))
    assert places == {"c:/work/proj/notes", "/opt/app/bin/tool", "docs/guide.md"}


def _settled(conn: sqlite3.Connection, settings: config.Settings) -> consolidation.Outcome:
    outcome = _run_place(
        conn,
        settings,
        new_fact(f"The operator keeps the project at {PLACE}."),
        new_fact("The operator appends a line to README.md."),
    )
    assert outcome.skipped == 1
    return outcome


def test_the_batch_row_stores_facts_skipped_and_the_listing_serves_it_as_a_string(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    outcome = _settled(conn, settings)
    stored = conn.execute(
        "SELECT facts_skipped FROM consolidation_batches WHERE batch_id = ?", (outcome.batch_id,)
    ).fetchone()
    assert stored[0] == 1
    [batch] = consolidation.listing_payload(consolidation.listing(conn, 5))["batches"]
    assert batch["facts_skipped"] == "1" and batch["facts_archived"] == "0"
    assert batch["facts_skipped_reason"] is None


def test_a_pre_026_batch_serves_facts_skipped_null_with_its_reason(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO consolidation_batches (batch_id, status, episode_count, started_at) "
        "VALUES ('bat_old', 'done', 1, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    [batch] = consolidation.listing_payload(consolidation.listing(conn, 5))["batches"]
    assert batch["facts_skipped"] is None
    assert batch["facts_skipped_reason"] == "counted from v7 on"


def test_the_event_serves_facts_archived_as_a_string(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    _settled(conn, settings)
    assert events_of(conn)[0][3]["facts_archived"] == "0"


# ---- ticket 296: the reply schema the provider enforces, and a batch's cost ----------------------

DECISION_98_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "actor", "path", "kind", "relation", "replaces"],
                "properties": {
                    "text": {"type": "string"},
                    "actor": {"type": "string"},
                    "path": {"type": ["string", "null"]},
                    "kind": {
                        "type": "string",
                        "enum": ["preference", "convention", "setup", "decision"],
                    },
                    "relation": {"type": "string", "enum": ["new", "contradicts"]},
                    "replaces": {"type": ["string", "null"]},
                },
            },
        }
    },
}


def fact_of(
    text: str,
    *,
    actor: str = "operator",
    path: str | None = None,
    kind: str = "preference",
    relation: str = "new",
    replaces: str | None = None,
) -> dict[str, object]:
    return {
        "text": text,
        "actor": actor,
        "path": path,
        "kind": kind,
        "relation": relation,
        "replaces": replaces,
    }


def test_reply_schema_is_decision_98_exactly_and_rides_the_request(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    assert consolidation.REPLY_SCHEMA == DECISION_98_SCHEMA
    seed(conn, AFTER)
    provider = CountingProvider([reply()])
    consolidation.consolidate(
        conn, provider=provider, settings=settings, now=clock.now(), size=AFTER
    )
    sent = provider.seen[0].response_schema
    assert sent is not None and sent.schema == DECISION_98_SCHEMA and sent.name
    assert "attribut" not in consolidation.INSTRUCTION.casefold()


def test_a_reply_with_the_old_kind_vocabulary_is_unparseable() -> None:
    old = json.dumps({"facts": [{"text": "A", "kind": "new", "replaces": None}]})
    with pytest.raises(TranslationError):
        translate.consolidation_facts(old)


def test_facts_with_a_bad_actor_a_non_place_path_or_a_mention_without_a_path_are_skipped(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    outcome = _run_place(
        conn,
        settings,
        fact_of("The operator prefers tea."),
        fact_of("The model made a decision.", actor="nobody"),
        fact_of("Uses uv.", path="design"),
        fact_of("The operator edits README.md often."),
        fact_of(f"The repository at {PLACE} uses uv.", kind="setup", path=PLACE),
    )
    assert [fact.text for fact in outcome.facts] == [
        "The operator prefers tea.",
        f"The repository at {PLACE} uses uv.",
    ]
    assert outcome.skipped == 3
    assert batches(conn)[0][1] == "done"
    assert conn.execute("SELECT facts_skipped FROM consolidation_batches").fetchone()[0] == 3


def test_stored_facts_carry_kind_actor_and_path_tags(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    outcome = _run_place(
        conn,
        settings,
        fact_of("The operator prefers tea."),
        fact_of(f"The repository at {PLACE} uses uv.", kind="setup", path=PLACE),
    )
    tea, setup = outcome.facts
    assert set(semantic.get(conn, tea.mem_id).tags) == {
        "consolidation",
        "kind:preference",
        "actor:operator",
    }
    assert set(semantic.get(conn, setup.mem_id).tags) == {
        "consolidation",
        "kind:setup",
        "actor:operator",
        f"path:{PLACE}",
    }


def _price(conn: sqlite3.Connection, provider: str, model: str) -> None:
    db.write(
        conn,
        (
            (
                "INSERT INTO prices (provider, model, input, output, cached_input, currency, "
                "source_url, fetched_at) VALUES (?, ?, '0.10', '0.50', NULL, 'USD', 'u', "
                "'2026-10-01T00:00:00Z')",
                (provider, model),
            ),
        ),
    )


def test_a_settled_batch_writes_its_cost_from_the_price_row(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    settings = dataclasses.replace(settings, provider="groq")
    _price(conn, "groq", SMALL)
    _run_place(conn, settings, fact_of("The operator prefers tea."))
    cost, nanos = conn.execute("SELECT cost, cost_nanos FROM consolidation_batches").fetchone()
    # 11 input tokens at 0.10 and 7 output tokens at 0.50 per million
    assert cost == "0.000005" and nanos == 4600


def test_an_unpriced_batch_keeps_a_null_cost(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    _run_place(conn, settings, fact_of("The operator prefers tea."))
    assert tuple(conn.execute("SELECT cost, cost_nanos FROM consolidation_batches").fetchone()) == (
        None,
        None,
    )


# ---- ticket 297: the request scope is a parameter (decision 98) ----------------------------------


def test_the_request_scope_defaults_to_consolidate_and_takes_a_case_scope(
    conn: sqlite3.Connection, settings: config.Settings
) -> None:
    seed(conn, AFTER)
    spy = CountingProvider([reply(new_fact("The operator prefers tea."))])
    consolidation.consolidate(
        conn, provider=spy, settings=settings, now=clock.now(), size=AFTER, scope="_consolidate-x"
    )
    assert spy.seen[0].metadata == {"scope": "_consolidate-x"}
