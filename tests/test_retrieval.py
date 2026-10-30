"""FR-11: retrieval is assembled once per run and never exceeds the token budget.

Four claims are load-bearing here and each one is tested against the thing that could break it.

* **The ceiling holds.** A deliberately over-filled store still assembles to a context whose
  token count is less than or equal to the budget, at every budget worth testing including zero.
* **Assembly happens once per run**, not once per iteration, which is measured with a counting
  spy wrapped around ``assemble`` across a multi-turn run rather than asserted about the source.
* **Truncation is deterministic**, so the same store and the same budget assemble byte-identical
  text twice.
* **Counting is offline, stdlib and deterministic**, checked against frozen input/output
  examples and an AST scan proving ``count_tokens`` still has exactly one caller path.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessed import config
from harnessed.errors import ConfigError
from harnessed.events.bus import EventBus
from harnessed.events.names import NAMES
from harnessed.loop.engine import run_loop
from harnessed.loop.prompt import build_system_prompt
from harnessed.memory import budget, db, episodic, procedural, retrieval, semantic
from harnessed.providers.types import ChatRequest, ChatResponse, Message, ToolCall
from harnessed.tools import default_registry
from support.spies import CountingAssembler, CountingProvider, RecordingObserver

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "harnessed"
TOKEN_COUNTS = REPO_ROOT / "tests" / "fixtures" / "token-counts.json"

# The one query every assembly test asks, and the words the seeded store is built from, so a
# search can actually match. Retrieval quality is not what FR-11 measures; the ceiling is.
QUERY = "deploy the harness offline"

# A corpus wide enough that a counting difference shows up: prose, punctuation, code, JSON and
# a non-Latin script, which decision 6 names as the cases the approximation is worst on.
COUNTING_CORPUS = (
    "",
    "   \n\t ",
    "one",
    "The harness runs offline, deterministically, every time.",
    "Punctuation!!! Really? Yes -- all of it; even this.",
    'def f(x): return {"a": [1, 2, 3], "b": None}',
    "こんにちは world",
    "hyphen-separated words and under_scored names",
)


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database file under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


def overfill(connection: sqlite3.Connection, run_id: str = "run_overfilled") -> None:
    """Write far more than any budget can hold, across all three stores.

    Deliberately over-filled is the premise of FR-11's first consequence, so the fixture is the
    premise: the semantic and episodic stores each hold more text than the default 4000-token
    budget on its own. Procedural memory cannot: ``activate`` clears the active set before it
    inserts, so exactly one procedure is ever current, and retrieval offers the active set rather
    than the history (ticket 055). Every version is still written, so the history is over-filled
    even though what retrieval may draw from it is one row.
    """
    for index in range(40):
        procedure = procedural.put(
            connection,
            f"Procedure {index}: to deploy the harness offline, "
            + "check the fixture corpus first. " * 12,
            source="test",
        )
        procedural.activate(connection, procedure.proc_id, cand_id=None)
        semantic.put(
            connection,
            f"Fact {index}: the harness can deploy offline because " + "it replays fixtures. " * 12,
            source="test",
        )
        episodic.put(
            connection,
            run_id,
            "loop",
            f"Episode {index}: the run asked to deploy the harness offline. " * 12,
        )


# ---- counting: deterministic, offline, stdlib, and the formula the architecture fixed ----------


def test_count_tokens_matches_the_frozen_examples() -> None:
    cases = json.loads(TOKEN_COUNTS.read_text(encoding="utf-8"))
    assert [case["text"] for case in cases] == list(COUNTING_CORPUS)
    for case in cases:
        assert budget.count_tokens(case["text"]) == case["tokens"]


def test_count_tokens_is_stable_across_repeated_calls() -> None:
    for text in COUNTING_CORPUS:
        assert budget.count_tokens(text) == budget.count_tokens(text)


def test_count_tokens_returns_the_same_integers_on_every_machine() -> None:
    """Pinned expectations. No download, no dependency, no locale, no hash seed."""
    assert budget.count_tokens("") == 0
    assert budget.count_tokens("   \n\t ") == 0
    assert budget.count_tokens("one") == 2
    assert budget.count_tokens("one two three four five") == 7
    assert budget.count_tokens("Hello, world!") == 6


def test_the_counting_module_imports_nothing_but_the_standard_library() -> None:
    """A download or a dependency would break "identical on any machine with Python 3.12"."""
    tree = ast.parse((SOURCE_ROOT / "memory" / "budget.py").read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "math", "re", "typing"}


def test_count_tokens_is_declared_in_exactly_one_module() -> None:
    """The declaration is not a caller, so it is pinned separately from the call site below."""
    declarers = {
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == "count_tokens"
    }
    assert declarers == {"memory/budget.py"}


def test_count_tokens_has_exactly_these_caller_paths() -> None:
    """Decision 6 makes the v2 swap to a real tokenizer safe by keeping the call sites closed.

    A caller outside this named set is what would make that swap a multi-file change with more
    than one budget to keep in step, so the set is asserted here rather than left as a note in
    the ticket. The module that declares the function is exempt: a declaration is not a use.
    ``router/state.py`` is decision 58's truncation rule, measured against ``STATE_BUDGET``
    (ticket 160).
    """
    callers = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if relative == "memory/budget.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            named = isinstance(node, ast.Attribute | ast.Name) and (
                getattr(node, "attr", getattr(node, "id", None)) == "count_tokens"
            )
            if named:
                callers.add(relative)
    assert callers == {"memory/retrieval.py", "router/state.py"}


# ---- the configured budget ---------------------------------------------------------------------


def test_the_default_retrieval_token_budget_is_4000(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chdir is what makes this a claim about the default rather than about the developer's
    own `.env`: since ticket 040 the file in the working directory supplies every documented
    variable, so a checkout that sets this one would decide the test (learnings, ticket 039)."""
    monkeypatch.chdir(tmp_path)
    assert config.load().retrieval_token_budget == 4000


def test_the_environment_overrides_the_default(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_TOKEN_BUDGET", "512")
    assert config.load().retrieval_token_budget == 512


def test_the_per_run_override_wins_over_the_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--token-budget`` binds to this argument; the flag spelling is owned by config."""
    monkeypatch.setenv("HARNESSED_TOKEN_BUDGET", "512")
    assert config.load(token_budget=77).retrieval_token_budget == 77
    assert config.TOKEN_BUDGET_FLAG == "--token-budget"


def test_a_malformed_budget_is_a_config_error(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_TOKEN_BUDGET", "lots")
    with pytest.raises(ConfigError):
        config.load()


def test_a_negative_budget_is_rejected_at_assembly(conn: sqlite3.Connection) -> None:
    with pytest.raises(ConfigError):
        retrieval.assemble(conn, QUERY, budget=-1)


# ---- the ceiling ------------------------------------------------------------------------------


@pytest.mark.parametrize("ceiling", [0, 1, 37, 400, 4000])
def test_an_overfilled_store_still_assembles_under_the_budget(
    conn: sqlite3.Connection, ceiling: int
) -> None:
    overfill(conn)
    assembled = retrieval.assemble(conn, QUERY, budget=ceiling, run_id="run_overfilled")
    assert budget.count_tokens(assembled.text) <= ceiling
    assert assembled.tokens == budget.count_tokens(assembled.text)
    assert assembled.tokens <= assembled.budget == ceiling


def test_an_overfilled_store_truncates_and_reports_what_it_dropped(
    conn: sqlite3.Connection,
) -> None:
    overfill(conn)
    assembled = retrieval.assemble(conn, QUERY, budget=400, run_id="run_overfilled")
    assert assembled.truncated
    assert assembled.dropped
    assert assembled.included
    assert all(item.text in assembled.text for item in assembled.included)


def test_a_small_store_fits_whole_and_reports_no_truncation(conn: sqlite3.Connection) -> None:
    procedure = procedural.put(
        conn, "Deploy the harness offline before anything else.", source="test"
    )
    procedural.activate(conn, procedure.proc_id, cand_id=None)
    semantic.put(conn, "The harness can deploy offline.", source="test")
    assembled = retrieval.assemble(conn, QUERY, budget=4000)
    assert not assembled.truncated
    assert assembled.dropped == ()
    assert len(assembled.included) == 2


def test_an_empty_store_assembles_to_nothing(conn: sqlite3.Connection) -> None:
    assembled = retrieval.assemble(conn, QUERY, budget=4000)
    assert assembled.text == ""
    assert assembled.tokens == 0
    assert not assembled.truncated


def test_a_query_full_of_punctuation_does_not_reach_the_search_engine_raw(
    conn: sqlite3.Connection,
) -> None:
    """A user task is arbitrary text, and FTS5 MATCH has a syntax. Assembly is not a place a run
    is allowed to die, so the query is rebuilt from its words rather than passed through."""
    overfill(conn)
    assembled = retrieval.assemble(conn, 'deploy AND ("offline OR NEAR/', budget=4000)
    assert assembled.tokens <= 4000


# ---- what matches, and what does not (ticket 048) -----------------------------------------------

ANSWERING_FACT = "the ingest service listens on port 8471"

IRRELEVANT_FACTS = (
    "the billing exporter runs every Sunday at 02:00",
    "the staging cluster has three nodes",
    "invoices are archived after seven years",
)

PORT_QUESTION = "What port does the ingest service listen on?"


def _store_the_needle_in_a_haystack(conn: sqlite3.Connection) -> None:
    """One fact that answers the question, and three that plainly do not."""
    semantic.put(conn, ANSWERING_FACT, source="test")
    for fact in IRRELEVANT_FACTS:
        semantic.put(conn, fact, source="test")


def test_a_word_that_carries_no_meaning_does_not_make_a_fact_match(
    conn: sqlite3.Connection,
) -> None:
    """The defect the judge tier found, as an offline assertion (ticket 048).

    The query was rebuilt from *every* word of the task, ``the`` included, joined with ``OR``. So
    any note containing a stopword matched, and in a store of four sentences that is every note.
    Retrieval returned the answering fact and two unrelated ones; the judge scored the assembled
    context 0.0 and said why.

    Ranking was never the problem - ``semantic.search`` already ends ``ORDER BY rank``, which is
    bm25, and the answering fact already came back first. Being in the right order is no
    consolation when the ceiling is wide enough to admit everything that matched.
    """
    _store_the_needle_in_a_haystack(conn)
    assembled = retrieval.assemble(conn, PORT_QUESTION, budget=4000)

    assert ANSWERING_FACT in assembled.text
    for fact in IRRELEVANT_FACTS:
        assert fact not in assembled.text, (
            f"{fact!r} was retrieved for {PORT_QUESTION!r}. It shares no meaningful word with the "
            f"question, so it is here because a stopword matched."
        )


def test_a_task_of_nothing_but_stopwords_still_searches(conn: sqlite3.Connection) -> None:
    """Dropping every word would retrieve nothing, which is worse than retrieving loosely.

    A question can legitimately be made of small words. Falling back to the task's own words keeps
    such a run's memory attached, at the cost of the looseness this ticket otherwise removes -
    which is the right trade in the rarer case.
    """
    _store_the_needle_in_a_haystack(conn)
    assembled = retrieval.assemble(conn, "what is it on the", budget=4000)
    assert assembled.text, "a task of only stopwords assembled nothing at all"


def test_the_best_match_survives_a_budget_that_cannot_hold_them_all(
    conn: sqlite3.Connection,
) -> None:
    """Truncation cuts a *ranked* list, so the answering fact outranks its near neighbours.

    This is not new behaviour and the criterion is that it stays true: an edit that reached for
    ``ORDER BY created_at`` to make some other case deterministic would silently undo it, and
    nothing else in this file would notice.
    """
    for filler in range(40):
        semantic.put(conn, f"the ingest pipeline note number {filler}", source="test")
    semantic.put(conn, ANSWERING_FACT, source="test")

    assembled = retrieval.assemble(conn, PORT_QUESTION, budget=60)
    assert assembled.truncated, "the budget was not small enough to force a cut"
    assert ANSWERING_FACT in assembled.text


# ---- determinism -------------------------------------------------------------------------------


def test_the_same_store_and_budget_assemble_identically_twice(conn: sqlite3.Connection) -> None:
    overfill(conn)
    first = retrieval.assemble(conn, QUERY, budget=400, run_id="run_overfilled")
    second = retrieval.assemble(conn, QUERY, budget=400, run_id="run_overfilled")
    assert first.text == second.text
    assert first.tokens == second.tokens
    assert [item.ref for item in first.included] == [item.ref for item in second.included]
    assert [item.ref for item in first.dropped] == [item.ref for item in second.dropped]


def test_the_kept_items_are_a_prefix_of_the_ranked_candidates(conn: sqlite3.Connection) -> None:
    """Truncation takes a prefix, which is what makes "deterministic" something a reader can see:
    a lower budget can only ever keep less, never a different selection."""
    overfill(conn)
    wide = retrieval.assemble(conn, QUERY, budget=4000, run_id="run_overfilled")
    narrow = retrieval.assemble(conn, QUERY, budget=400, run_id="run_overfilled")
    wide_refs = [item.ref for item in wide.included]
    narrow_refs = [item.ref for item in narrow.included]
    assert wide_refs[: len(narrow_refs)] == narrow_refs
    assert len(narrow_refs) < len(wide_refs)


# ---- the events ---------------------------------------------------------------------------------


def test_the_published_names_are_in_the_frozen_taxonomy() -> None:
    assert {retrieval.ASSEMBLED, retrieval.BUDGET_TRUNCATED} <= NAMES


def test_truncation_publishes_budget_truncated_then_assembled(conn: sqlite3.Connection) -> None:
    overfill(conn)
    bus = EventBus(run_id="run_overfilled", conn=conn)
    observer = bus.subscribe(RecordingObserver())
    assert isinstance(observer, RecordingObserver)

    assembled = retrieval.assemble(
        conn, QUERY, budget=400, run_id="run_overfilled", publish=bus.publish
    )

    assert observer.names == [retrieval.BUDGET_TRUNCATED, retrieval.ASSEMBLED]
    truncated = observer.named(retrieval.BUDGET_TRUNCATED)[0]
    assert truncated.payload["budget"] == 400
    assert truncated.payload["dropped"] == len(assembled.dropped)
    finished = observer.named(retrieval.ASSEMBLED)[0]
    assert finished.payload["tokens"] == assembled.tokens
    assert finished.payload["kept"] == len(assembled.included)


def test_an_untruncated_assembly_publishes_only_assembled(conn: sqlite3.Connection) -> None:
    semantic.put(conn, "The harness can deploy offline.", source="test")
    bus = EventBus(run_id="run_small", conn=conn)
    observer = bus.subscribe(RecordingObserver())
    assert isinstance(observer, RecordingObserver)

    retrieval.assemble(conn, QUERY, budget=4000, publish=bus.publish)

    assert observer.names == [retrieval.ASSEMBLED]


def test_no_event_payload_carries_the_retrieved_text(conn: sqlite3.Connection) -> None:
    """The events are counters. Text belongs in the prompt, not in a row that outlives the run."""
    overfill(conn)
    bus = EventBus(run_id="run_overfilled", conn=conn)
    observer = bus.subscribe(RecordingObserver())
    assert isinstance(observer, RecordingObserver)

    retrieval.assemble(conn, QUERY, budget=400, run_id="run_overfilled", publish=bus.publish)

    for event in observer.events:
        assert all(isinstance(value, int) for value in event.payload.values())


# ---- once per run --------------------------------------------------------------------------------


def a_run(
    conn: sqlite3.Connection,
    home: Path,
    assembler: CountingAssembler,
    provider: CountingProvider,
) -> str:
    """One run, composed the way the composition root will compose it.

    Retrieval is assembled before the loop starts and never inside it, which is the whole of
    FR-11's second consequence. ``loop/engine.py`` is not this ticket's to edit, so the ordering
    lives here, in the caller, which is where a "once per run" claim is decided anyway.
    """
    assembled = assembler(conn, QUERY, budget=4000, run_id="run_overfilled")
    request = ChatRequest(
        model="fake",
        system=build_system_prompt("You are a harness.", [assembled.text]),
        messages=(Message(role="user", content=QUERY),),
    )
    return run_loop(provider, default_registry(home), request, limit=6).reply


def test_assembly_is_invoked_exactly_once_per_run(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    overfill(conn)
    assembler = CountingAssembler(retrieval.assemble)
    provider = CountingProvider(
        script=[
            ChatResponse(
                response_id="resp_1",
                model="fake",
                content="reading first",
                tool_calls=(
                    ToolCall(id="call_1", name="fs_read", arguments={"path": "notes.txt"}),
                ),
            ),
            ChatResponse(response_id="resp_2", model="fake", content="done"),
        ]
    )
    (tmp_harnessed_home / "notes.txt").write_text("notes", encoding="utf-8")

    reply = a_run(conn, tmp_harnessed_home, assembler, provider)

    assert reply == "done"
    assert provider.calls > 1, "a single-turn run would not distinguish per-run from per-iteration"
    assert assembler.calls == 1


def test_two_runs_assemble_twice_so_the_spy_is_measuring_something(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    overfill(conn)
    assembler = CountingAssembler(retrieval.assemble)
    for _ in range(2):
        provider = CountingProvider(
            script=[ChatResponse(response_id="resp", model="fake", content="done")]
        )
        a_run(conn, tmp_harnessed_home, assembler, provider)
    assert assembler.calls == 2


# ---- status, links and tags reach retrieval (ticket 047) ----------------------------------------

# Shares no meaningful word with PORT_QUESTION - not "port", "ingest", "service" or "listen".
# Deliberate: an earlier draft of this constant contained "ingest", so every link test passed by
# matching the search instead of by following the link, which is the exact shape of a test that is
# green for the wrong reason (ticket 029's rule).
LINKED_FACT = "the platform repository holds the deployment manifest for that component"


def test_an_archived_fact_is_not_a_candidate(conn: sqlite3.Connection) -> None:
    """Since ticket 041 a retrievable fact enters every future prompt at budget cost.

    A fact that has become false therefore costs something forever, and until ticket 047 the only
    way to stop paying was to delete it. The vault answers this by archiving; so does this.
    """
    fact = semantic.put(conn, ANSWERING_FACT, source="test")
    assert ANSWERING_FACT in retrieval.assemble(conn, PORT_QUESTION, budget=4000).text

    semantic.archive(conn, fact.mem_id)
    assert ANSWERING_FACT not in retrieval.assemble(conn, PORT_QUESTION, budget=4000).text


def test_a_linked_fact_arrives_with_the_fact_that_matched(conn: sqlite3.Connection) -> None:
    """Keyword search finds a fact sharing words with the task. A link finds one that shares none.

    ``LINKED_FACT`` has no meaningful word in common with the question, so no query however clean
    would surface it. That is the gap embeddings fill in v2 and an explicit link fills part of now.
    """
    target = semantic.put(conn, LINKED_FACT, source="test")
    semantic.put(conn, ANSWERING_FACT, source="test", links=(target.mem_id,))

    assembled = retrieval.assemble(conn, PORT_QUESTION, budget=4000)
    assert ANSWERING_FACT in assembled.text
    assert LINKED_FACT in assembled.text


def test_a_followed_fact_is_marked_as_followed(conn: sqlite3.Connection) -> None:
    """A reader of the context must be able to tell a searched fact from a followed one."""
    target = semantic.put(conn, LINKED_FACT, source="test")
    semantic.put(conn, ANSWERING_FACT, source="test", links=(target.mem_id,))

    assembled = retrieval.assemble(conn, PORT_QUESTION, budget=4000)
    kinds = {item.kind for item in assembled.included}
    assert "fact" in kinds
    assert "linked" in kinds
    followed = [item for item in assembled.included if item.kind == "linked"]
    assert [item.text for item in followed] == [LINKED_FACT]


def test_expansion_follows_exactly_one_hop(conn: sqlite3.Connection) -> None:
    """Two hops from a well-connected fact is the whole store, which is the problem, not the fix."""
    far = semantic.put(conn, "a fact two hops away that must not be retrieved", source="test")
    near = semantic.put(conn, LINKED_FACT, source="test", links=(far.mem_id,))
    semantic.put(conn, ANSWERING_FACT, source="test", links=(near.mem_id,))

    assembled = retrieval.assemble(conn, PORT_QUESTION, budget=4000)
    assert LINKED_FACT in assembled.text
    assert "two hops away" not in assembled.text


def test_a_followed_fact_never_displaces_a_matched_one(conn: sqlite3.Connection) -> None:
    """The prefix rule is unchanged: matched facts are admitted before followed ones."""
    target = semantic.put(conn, LINKED_FACT, source="test")
    semantic.put(conn, ANSWERING_FACT, source="test", links=(target.mem_id,))

    assembled = retrieval.assemble(conn, PORT_QUESTION, budget=22)
    assert ANSWERING_FACT in assembled.text
    assert LINKED_FACT not in assembled.text


def test_an_archived_fact_is_not_followed_either(conn: sqlite3.Connection) -> None:
    """Archiving retires a fact from retrieval, and a link is retrieval."""
    target = semantic.put(conn, LINKED_FACT, source="test")
    semantic.put(conn, ANSWERING_FACT, source="test", links=(target.mem_id,))
    semantic.archive(conn, target.mem_id)

    assembled = retrieval.assemble(conn, PORT_QUESTION, budget=4000)
    assert ANSWERING_FACT in assembled.text
    assert LINKED_FACT not in assembled.text


def test_a_tag_narrows_the_candidates(conn: sqlite3.Connection) -> None:
    semantic.put(conn, ANSWERING_FACT, source="test", tags=("infra",))
    semantic.put(conn, "the ingest report is filed monthly", source="test", tags=("admin",))

    assembled = retrieval.assemble(conn, PORT_QUESTION, budget=4000, tag="infra")
    assert ANSWERING_FACT in assembled.text
    assert "filed monthly" not in assembled.text


# ---- retired procedures are not offered (ticket 055) -------------------------------------------

# The retired procedure shares no meaningful word with QUERY, with PORT_QUESTION, or with any
# fact in this file. Procedures are not searched, so its absence can only mean deactivation was
# honoured - there is no second route by which it could have been dropped.
RETIRED_PROCEDURE = "Escalate every ambiguity to the duty pager before proceeding."
REPLACEMENT_PROCEDURE = "Answer from the fixture corpus and cite the revision you used."


def _superseded_by(conn: sqlite3.Connection, first: str, second: str) -> tuple[str, str]:
    """Write and activate ``first``, then write and activate ``second`` over it.

    ``activate`` clears the active set before inserting, so activating a replacement *is* the
    deactivation of what it replaced: there is no separate retirement verb to call.
    """
    retired = procedural.put(conn, first, source="test")
    procedural.activate(conn, retired.proc_id, cand_id=None)
    current = procedural.put(conn, second, source="test")
    procedural.activate(conn, current.proc_id, cand_id=None)
    return retired.proc_id, current.proc_id


def test_a_deactivated_procedure_is_not_offered_under_default_kinds(
    conn: sqlite3.Connection,
) -> None:
    """A procedure the improve loop retired is knowledge that was tried and rejected (FR-12).

    Asserted through ``assemble`` with **default** kinds, because the default is where the bug
    lived: ``run`` narrows to facts and episodes, so naming ``procedure`` explicitly would test a
    path no caller takes and would keep passing if ``ALL_KINDS`` changed underneath it.
    """
    retired_id, current_id = _superseded_by(conn, RETIRED_PROCEDURE, REPLACEMENT_PROCEDURE)

    assembled = retrieval.assemble(conn, QUERY, budget=4000)

    offered = [item.ref for item in assembled.included if item.kind == "procedure"]
    assert offered == [current_id]
    assert retired_id not in offered
    assert REPLACEMENT_PROCEDURE in assembled.text
    assert RETIRED_PROCEDURE not in assembled.text


def test_superseded_versions_never_outrank_the_one_that_replaced_them(
    conn: sqlite3.Connection,
) -> None:
    """More versions on record than ``limit``, and the newest is the only candidate.

    ``procedural.all`` is ``ORDER BY rowid`` ascending, so a head slice of the full history
    prefers the *earliest* versions ever written and drops the replacement entirely. The active
    set has no such ordering hazard: it holds what is current and nothing else.
    """
    for index in range(retrieval.DEFAULT_LIMIT + 1):
        stale = procedural.put(conn, f"Revision {index}: {RETIRED_PROCEDURE}", source="test")
        procedural.activate(conn, stale.proc_id, cand_id=None)
    current = procedural.put(conn, REPLACEMENT_PROCEDURE, source="test")
    procedural.activate(conn, current.proc_id, cand_id=None)

    assembled = retrieval.assemble(conn, QUERY, budget=4000)

    offered = [item.ref for item in assembled.included if item.kind == "procedure"]
    assert offered == [current.proc_id]
    assert "Revision " not in assembled.text
    assert len(procedural.all(conn)) == retrieval.DEFAULT_LIMIT + 2


def test_an_active_procedure_is_retrieved_exactly_as_before(conn: sqlite3.Connection) -> None:
    """This ticket removes retired procedures; it does not change which active ones win."""
    stored = procedural.put(conn, REPLACEMENT_PROCEDURE, source="test")
    procedural.activate(conn, stored.proc_id, cand_id=None)

    assembled = retrieval.assemble(conn, QUERY, budget=4000)

    assert [item.ref for item in assembled.included if item.kind == "procedure"] == [stored.proc_id]
    assert REPLACEMENT_PROCEDURE in assembled.text


def test_retirement_holds_for_procedures_as_it_does_for_facts(conn: sqlite3.Connection) -> None:
    """The module's claim that retired knowledge stays retired, asserted over both stores at once.

    An archived fact and a deactivated procedure are retired by different verbs and the same
    promise. Pairing them in one assembly is what stops the sentence being true of one store and
    false of the other, which is exactly how it read before this ticket.
    """
    retired_fact = semantic.put(conn, LINKED_FACT, source="test")
    semantic.put(conn, ANSWERING_FACT, source="test", links=(retired_fact.mem_id,))
    semantic.archive(conn, retired_fact.mem_id)
    _superseded_by(conn, RETIRED_PROCEDURE, REPLACEMENT_PROCEDURE)

    assembled = retrieval.assemble(conn, PORT_QUESTION, budget=4000)

    assert ANSWERING_FACT in assembled.text
    assert REPLACEMENT_PROCEDURE in assembled.text
    assert LINKED_FACT not in assembled.text
    assert RETIRED_PROCEDURE not in assembled.text


def write_evidence(name: str, text: str) -> Path:
    """One artefact under `evidence/055/`. Regenerated on every verify run, never committed."""
    directory = REPO_ROOT / "evidence" / "055"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_evidence_shows_the_two_readers_disagreeing(conn: sqlite3.Connection) -> None:
    """The artefact states what each reader offers, because agreeing readers prove nothing.

    Backfilled after 055 shipped. The build session wrote the fix and its four tests and recorded
    no evidence file, which every ticket since 019 carries; its loop-state closed with
    "next: none - ticket done", so the omission was an oversight rather than a deferral.

    The seeded store is the one shape that separates the two readers: more versions on record than
    ``DEFAULT_LIMIT``, every one of them retired by the activation of the next, so the history is
    long, the active set is a singleton, and the head of the history is the oldest row in it.
    """
    for index in range(retrieval.DEFAULT_LIMIT + 1):
        stale = procedural.put(conn, f"Revision {index}: {RETIRED_PROCEDURE}", source="test")
        procedural.activate(conn, stale.proc_id, cand_id=None)
    current = procedural.put(conn, REPLACEMENT_PROCEDURE, source="test")
    procedural.activate(conn, current.proc_id, cand_id=None)

    shipped = [row.text for row in procedural.all(conn)[: retrieval.DEFAULT_LIMIT]]
    assembled = retrieval.assemble(conn, QUERY, budget=4000)
    offered = [item.text for item in assembled.included if item.kind == "procedure"]

    assert offered == [REPLACEMENT_PROCEDURE]
    assert all(RETIRED_PROCEDURE in text for text in shipped)
    assert REPLACEMENT_PROCEDURE not in shipped

    write_evidence(
        "active-not-retired.txt",
        "\n".join(
            (
                f"{retrieval.DEFAULT_LIMIT + 2} procedure versions on record. Each activation",
                "clears the active set, so only the last is current and the other",
                f"{retrieval.DEFAULT_LIMIT + 1} are retired.",
                "",
                f"procedural.all(conn)[:{retrieval.DEFAULT_LIMIT}] - what shipped before this",
                "ticket, offered to the model under the label the assembler presents as",
                "promoted, durable knowledge:",
                *(f"  - {text}" for text in shipped[:3]),
                f"  ... {len(shipped) - 4} more, every one a retired revision ...",
                f"  - {shipped[-1]}",
                "",
                "assemble(conn, query) under default kinds - what it offers now:",
                *(f"  - {text}" for text in offered),
                "",
                "The two lists share nothing. The first is the oldest retired revisions, taken",
                "from the head of an ascending history; the second is the active set. Every",
                "retired revision was a candidate, and the version that replaced them all was",
                "not among them.",
            )
        ),
    )
