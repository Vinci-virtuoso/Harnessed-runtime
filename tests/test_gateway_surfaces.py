"""FR-40: every read surface is one call into a store that exists (ticket 070).

Decision 24's route table is the whole contract, and this file holds it as data: ``app.routes``
must match it in both directions, so a route the table never named and a table row nothing
answers both fail. Each handler under ``gateway/routes/`` is read by AST and must hand the
request's connection to exactly one callable: that is what "one call into a store" means when a
machine checks it rather than a reviewer, and it is the structural half of "a second view, not a
second source of truth" (the byte comparison against the CLI is ticket 072's).

The glossary is load-bearing here too. ``{run_key}`` accepts a session id from ``harnessed run``
and a case run key from a scored eval case, because ``turns.run_id`` holds both shapes and the
listing labels every row with its ``key_kind`` (FR-41). Episodes are keyed by the session id the
bus carried, so that parameter is spelled ``{session_id}``; decision 24's row spells it
``{run_id}``, which MUST rule 15 forbids, and this ticket's acceptance criteria name zero such
parameters. Nothing here unifies the ids.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harnessed import clock, config
from harnessed.evals import runner, trace
from harnessed.evals.suite import Case, GraderSpec, Suite, load_named_suite
from harnessed.events.bus import EventBus
from harnessed.gateway import create_app
from harnessed.gateway.serialize import CanonicalJSONResponse
from harnessed.improve import steps
from harnessed.loop import runs
from harnessed.loop.engine import LoopResult, run_loop
from harnessed.loop.journal import Journal
from harnessed.loop.prompt import build_system_prompt
from harnessed.memory import db, episodic, procedural, redact, semantic
from harnessed.providers import jev, quirks, registry
from harnessed.providers.fake import FakeProvider
from harnessed.providers.translate import tool_call_payload
from harnessed.providers.types import ChatRequest, ChatResponse, Message
from harnessed.router import decisions as decisions_store
from harnessed.serialize import canonical
from harnessed.tools import default_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
ROUTES_ROOT = PACKAGE_ROOT / "gateway" / "routes"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "070"

#: Decision 24, as data. Path beside the one callable it renders. Every route is ``GET``.
ROUTE_TABLE: tuple[tuple[str, str], ...] = (
    ("/v1/health", "gateway/app.py::health"),
    ("/v1/runs/totals", "loop/runs.py::totals then totals_payload"),
    ("/v1/runs/turn_seconds", "loop/runs.py::turn_seconds then turn_seconds_payload"),
    ("/v1/spend", "loop/runs.py::spend then spend_payload"),
    ("/v1/runs", "loop/runs.py::listing"),
    ("/v1/runs/{run_key}", "loop/runs.py::run_payload"),
    ("/v1/runs/{run_key}/trace", "evals/trace.py::assemble"),
    ("/v1/eval/runs", "evals/runner.py::recent then summary_payload"),
    ("/v1/eval/runs/{eval_run_id}", "evals/runner.py::scored_run then json_payload"),
    ("/v1/eval/compare", "evals/runner.py::comparison then comparison_payload"),
    ("/v1/eval/suites", "evals/suite.py::listing then listing_payload"),
    ("/v1/improve/cycles/{improvement_run_id}", "improve/steps.py::json_payload"),
    ("/v1/promotions", "memory/procedural.py::promotions"),
    ("/v1/memory/facts", "memory/semantic.py::searched then json_payload"),
    ("/v1/memory/facts/{mem_id}", "memory/semantic.py::get"),
    ("/v1/memory/episodes/{session_id}", "memory/episodic.py::for_session"),
    ("/v1/memory/procedures", "memory/procedural.py::active"),
    ("/v1/memory/totals", "memory/totals.py::totals then totals_payload"),
    ("/v1/memory/consolidation", "memory/consolidation.py::listing then listing_payload"),
    ("/v1/sessions", "events/read.py::sessions then sessions_payload"),
    ("/v1/sessions/{session_id}/events", "events/read.py::json_payload"),
    ("/v1/sessions/{session_id}/stream", "gateway/sse.py::stream"),
    ("/v1/events/gate", "events/read.py::gate_split then gate_split_payload"),
    ("/v1/events/totals", "events/read.py::totals then totals_payload"),
    ("/v1/prices", "providers/pricing.py::stored then json_payload"),
    ("/v1/providers", "providers/registry.py::states then json_payload"),
    ("/v1/agents", "acp/probes.py::states then json_payload"),
    (
        "/v1/sessions/{session_id}/decisions",
        "router/decisions.py::for_session then json_payload",
    ),
    ("/v1/router", "router/decisions.py::probe_state then router_json_payload"),
    ("/v1/outputs/{output_id}", "acp/outputs.py::body"),
    (
        "/v1/sessions/{session_id}/messages",
        "loop/messages.py::session_payload (for_session and task_for, then messages_payload)",
    ),
    ("/v1/messages", "loop/messages.py::recent then recent_payload"),
    ("/v1/permissions", "acp/pending.py::listing then listing_payload"),
    ("/v1/tasks", "tasks/store.py::listing then listing_payload"),
    ("/v1/tasks/totals", "tasks/store.py::totals then totals_payload"),
    ("/v1/tasks/{task_id}", "tasks/store.py::get then json_payload"),
    ("/v1/schedules", "tasks/schedule.py::listing then listing_payload"),
)

#: The query a surface needs before it can answer at all: its required parameters, filled with
#: ids no ledger holds so the answer is the same 404 every other row gives. A row here is a
#: route whose question is incomplete without a query string, never a filter (ticket 098).
REQUIRED_QUERY: dict[str, str] = {
    "/v1/permissions": "?state=pending",
    "/v1/memory/facts": "?q=anything",
    "/v1/eval/compare": (
        "?before=eval_00000000000000000000000000&after=eval_00000000000000000000000000"
    ),
}

#: The glossary terms a path parameter may carry (MUST rule 15). ``run_id`` is not one of them.
GLOSSARY_PARAMETERS = frozenset(
    {"run_key", "eval_run_id", "improvement_run_id", "mem_id", "session_id", "output_id", "task_id"}
)

#: The store callables the handlers must reach, module alias dot name, as the AST sees them.
#: The ticket also names ``steps.latest`` and ``steps.get``; neither can be the one call a
#: handler makes (``latest`` goes from a suite to an id, ``get`` needs a step key), so the
#: cycle surface is pinned on the projection it renders and the one call is whatever rebuilds a
#: ``Cycle`` by id.
#:
#: The facts surface is pinned on ``semantic.searched`` rather than ``semantic.search`` since
#: ticket 121. ``search`` takes an FTS5 expression and ``?q=`` carries text an operator typed;
#: a handler reaching the strict one again is the 500 that ticket closed, so this row is what
#: would catch it.
REQUIRED_CALLS = frozenset(
    {
        "semantic.get",
        "semantic.searched",
        "episodic.for_session",
        "procedural.active",
        "procedural.promotions",
        "runner.recent",
        "runner.scored_run",
        "runner.comparison",
        "suite.listing",
        "runner.json_payload",
        "steps.json_payload",
        "trace.assemble",
        "runs.listing",
        "runs.run_payload",
        "runs.totals",
        "runs.turn_seconds",
        "runs.spend",
        "read.sessions",
        "read.gate_split",
        "read.totals",
        "totals.totals",
        "pricing.stored",
        "registry.states",
        "probes.states",
        "decisions.for_session",
        "decisions.probe_state",
        "outputs.body",
        "pending.listing",
        "store.listing_wire",
        "store.totals",
        "store.get",
        "messages.session_payload",
        "messages.recent",
    }
)

#: The handlers that read no database, beside the one subsystem callable each renders. FR-89's
#: provider state is the environment as ``config.load`` resolves it (decision 55): there is no
#: ledger row behind it, so the handler takes no ``conn`` and its one read is named here instead.
CONNECTIONLESS: dict[str, str] = {"providers": "registry.states"}

#: The subsystem areas a route module may call into (the gateway's import-table row).
SUBSYSTEMS = ("evals", "improve", "memory", "loop", "events", "providers", "acp", "router", "tasks")

#: The collection surfaces without a path parameter and the key each wraps its rows under.
#: Episodes are not here: its ``{session_id}`` under an empty store is an id no ledger holds,
#: which is a 404 since ticket 076 (decision 24), so its empty-collection case needs a held
#: session and has its own test below.
COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("/v1/runs", "runs"),
    ("/v1/sessions", "sessions"),
    ("/v1/eval/runs", "runs"),
    ("/v1/promotions", "promotions"),
    ("/v1/memory/facts?q=anything", "facts"),
    ("/v1/memory/procedures", "procedures"),
    ("/v1/messages", "messages"),
)

#: The single-entity surfaces, each with an id the ledgers do not hold.
SINGLE_ENTITY_MISSES: tuple[str, ...] = (
    "/v1/runs/run_00000000000000000000000000",
    "/v1/runs/run_00000000000000000000000000/trace",
    "/v1/eval/runs/eval_00000000000000000000000000",
    "/v1/improve/cycles/imp_00000000000000000000000000",
    "/v1/memory/facts/mem_00000000000000000000000000",
    "/v1/memory/episodes/run_00000000000000000000000000",
    "/v1/sessions/run_00000000000000000000000000/decisions",
    "/v1/sessions/run_00000000000000000000000000/messages",
)

#: A key the redaction module recognises, never stored raw (FR-13). Built from parts so the
#: source of this file does not itself hold a key-shaped string.
SECRET = "sk-ant-" + "api03-seeded-for-ticket-070"

MODEL = "fake-model"
WORDS = ("alpha", "beta", "gamma", "delta", "epsilon")

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- benches -------------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_harnessed_home: Path) -> Iterator[TestClient]:
    """The app over the migrated database ``conn`` writes to."""
    app = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def tick(seconds: int) -> None:
    clock.freeze(clock.now() + timedelta(seconds=seconds))


def record_turns(conn: sqlite3.Connection, run_key: str, *replies: str) -> None:
    """Journal one turn per reply under ``run_key``, a second apart, the last one terminal."""
    journal = Journal(conn, run_key, serialize_call=tool_call_payload, provider="fake")
    request = ChatRequest(model=MODEL, system="", messages=(Message(role="user", content="hi"),))
    for iteration, reply in enumerate(replies, start=1):
        tick(1)
        last = iteration == len(replies)
        response = ChatResponse(
            response_id=f"resp_{iteration}",
            model=MODEL,
            content=reply,
            stop_reason="end_turn" if last else "tool_use",
        )
        journal.record(iteration, request, response, ())


def seed_session(conn: sqlite3.Connection, *replies: str) -> str:
    """One ``harnessed run``: a fresh session id, its start published on the bus (which is
    where a session id is minted and recorded, ticket 076) and its turns journaled under it."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    record_turns(conn, bus.run_id, *replies)
    return bus.run_id


@dataclasses.dataclass
class JournalingSolver:
    """A ``Solve`` that journals under the case run key the runner hands it."""

    conn: sqlite3.Connection
    run_keys: list[str] = dataclasses.field(default_factory=list)

    def __call__(self, request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        assert run_key is not None
        self.run_keys.append(run_key)
        word = request.messages[-1].content.removeprefix("say ")
        record_turns(self.conn, run_key, word)
        return LoopResult(word, (), 1)


def five_case_suite() -> Suite:
    return Suite(
        name="five",
        path=Path("five.yaml"),
        cases=tuple(
            Case(
                id=f"five-{index}",
                prompt=f"say {word}",
                graders=(GraderSpec(name="exact_match", expect={"value": word}),),
            )
            for index, word in enumerate(WORDS, start=1)
        ),
    )


def seed_eval(conn: sqlite3.Connection) -> tuple[str, tuple[str, ...]]:
    """A five-case eval through the real runner: its eval run id and the five case run keys."""
    solver = JournalingSolver(conn)
    result = runner.run_suite(five_case_suite(), solver, conn, model=MODEL, provider="fake")
    assert len(solver.run_keys) == 5
    return result.eval_run_id, tuple(solver.run_keys)


def seed_candidates(conn: sqlite3.Connection, *cand_ids: str) -> None:
    """``promotions.cand_id`` is a foreign key onto ``candidates``, so the rows exist first."""
    db.write(
        conn,
        tuple(
            (
                "INSERT INTO candidates (cand_id, text, source_case_ids, provenance, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    cand_id,
                    f"procedure {cand_id}",
                    '["b"]',
                    "ticket 070",
                    clock.isoformat(clock.now()),
                ),
            )
            for cand_id in cand_ids
        ),
    )


def seed_promotions(conn: sqlite3.Connection) -> list[procedural.Promotion]:
    """Two decisions: one refused, one accepted, which also makes one procedure active."""
    seed_candidates(conn, "cand_alpha", "cand_beta")
    decided = []
    for cand_id, promoted in (("cand_alpha", False), ("cand_beta", True)):
        tick(1)
        decided.append(
            procedural.record_promotion(
                conn,
                cand_id=cand_id,
                text=f"always {cand_id}",
                promoted=promoted,
                reason="score_gain" if promoted else "no_gain",
                score_before=0.6,
                score_after=0.8 if promoted else 0.6,
                passes_before=3,
                passes_after=4 if promoted else 3,
                regressions=0,
                soft_drops=0,
            )
        )
    return decided


def seed_cycle(conn: sqlite3.Connection, home: Path) -> steps.Cycle:
    """One fresh, full improvement cycle over the shipped smoke suite, against the fixture corpus.

    Full, not dry: ``dry_run``, ``resumed`` and each step's ``replayed`` describe a pass and have
    no ledger column, so ``cycle_for`` reads every cycle back as a fresh full pass (ticket 074).
    Only a fresh full pass has a read-back that is byte-identical to what it returned.
    """
    provider = FakeProvider(config.load())
    registry = default_registry(home)

    def solve(request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        return run_loop(provider, registry, request, limit=6)

    return steps.run_cycle(
        conn,
        suite=load_named_suite("smoke"),
        holdout=load_named_suite("holdout"),
        solve=solve,
        reflector=provider,
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
    )


def fields_of(cls: type[Any]) -> set[str]:
    return {field.name for field in dataclasses.fields(cls)}


def projected(rows: tuple[Any, ...]) -> list[dict[str, object]]:
    return [dataclasses.asdict(row) for row in rows]


def assert_canonical(response: Any) -> Any:
    """The bytes are ``serialize.canonical`` of the object they decode to, one line, no space."""
    payload = response.json()
    assert response.content == canonical(payload).encode("utf-8")
    assert response.headers["content-type"].startswith("application/json")
    return payload


def assert_not_found(response: Any) -> None:
    assert response.status_code == 404, response.text
    payload = assert_canonical(response)
    assert set(payload) == {"error", "message"}
    assert payload["error"] == "SurfaceNotFound"
    assert payload["message"]


# ---- AC 1 and 7: app.routes is decision 24's table, and no parameter is named run_id ------------


def test_app_routes_match_decision_24_in_both_directions(client: TestClient) -> None:
    registered = {route.path: route for route in client.app.routes}  # type: ignore[attr-defined]
    table = {path for path, _ in ROUTE_TABLE}
    unnamed = set(registered) - table
    unserved = table - set(registered)
    assert unnamed == set(), f"registered but absent from decision 24: {sorted(unnamed)}"
    assert unserved == set(), f"in decision 24 but nothing answers: {sorted(unserved)}"
    assert len(registered) == len(ROUTE_TABLE), "a path is registered twice"
    for path, route in registered.items():
        assert set(getattr(route, "methods", ())) == {"GET"}, path
    assert client.get("/v1/health").json()["route_count"] == len(ROUTE_TABLE)
    lines = [f"{path:48} {callable_}" for path, callable_ in ROUTE_TABLE]
    capture(
        "route-table.txt",
        "ticket 070 - app.routes beside decision 24's table (both directions match)\n"
        + "\n".join(lines)
        + "\n",
    )


def test_zero_path_parameters_are_named_run_id(client: TestClient) -> None:
    names = {
        name
        for route in client.app.routes  # type: ignore[attr-defined]
        for name in re.findall(r"\{(\w+)\}", route.path)
    }
    assert "run_id" not in names
    assert names == GLOSSARY_PARAMETERS
    assert [name for name in names if name == "run_id"] == []


# ---- AC 2 and 3: each handler hands the connection to exactly one callable ----------------------


def route_modules() -> list[Path]:
    found = sorted(path for path in ROUTES_ROOT.glob("*.py") if path.name != "__init__.py")
    assert {path.stem for path in found} >= {"runs", "evals", "improve", "memory", "events"}
    return found


def subsystem_aliases(tree: ast.Module) -> set[str]:
    """The local names bound to modules imported from a subsystem area (``runner``, ``read``)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        parts = node.module.split(".")
        if len(parts) >= 2 and parts[0] == "harnessed" and parts[1] in SUBSYSTEMS:
            aliases.update(alias.asname or alias.name for alias in node.names)
    return aliases


def handlers_in(tree: ast.Module) -> list[ast.FunctionDef]:
    """Every function decorated with ``@app.get(...)``, wherever ``register`` nests it."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "get"
            ):
                found.append(node)
    return found


def passes_conn(call: ast.Call) -> bool:
    positional = any(isinstance(arg, ast.Name) and arg.id == "conn" for arg in call.args)
    keyword = any(isinstance(kw.value, ast.Name) and kw.value.id == "conn" for kw in call.keywords)
    return positional or keyword


def called_name(call: ast.Call) -> str:
    return ast.unparse(call.func)


@dataclasses.dataclass(frozen=True)
class HandlerCalls:
    """What one handler body calls: the store calls (given ``conn``) and every other call."""

    name: str
    store: tuple[str, ...]
    other: tuple[str, ...]


def calls_of(handler: ast.FunctionDef) -> HandlerCalls:
    store: list[str] = []
    other: list[str] = []
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            (store if passes_conn(node) else other).append(called_name(node))
    return HandlerCalls(handler.name, tuple(store), tuple(other))


def scan(tree: ast.Module) -> list[HandlerCalls]:
    return [calls_of(handler) for handler in handlers_in(tree)]


def test_each_handler_hands_the_connection_to_exactly_one_callable() -> None:
    offenders = []
    seen = 0
    for path in route_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for handler in scan(tree):
            seen += 1
            if handler.name in CONNECTIONLESS:
                if handler.store or CONNECTIONLESS[handler.name] not in handler.other:
                    offenders.append(f"{path.name}::{handler.name} calls {list(handler.other)}")
                continue
            if len(handler.store) != 1:
                offenders.append(f"{path.name}::{handler.name} calls {list(handler.store)}")
    assert offenders == []
    assert seen == len(ROUTE_TABLE) - 1, "every route but health is a handler under routes/"


#: The emitters a handler may reach a subsystem for beyond its one store call: the entity
#: serializer, the summary-row serializer ticket 075 added so ``eval list --json`` and
#: ``GET /v1/eval/runs`` come from one function (decision 19), and the session-listing emitter
#: decision 38 names beside ``read.sessions`` (ticket 097), and the comparison emitter FR-62
#: pairs with ``runner.comparison`` (decision 37, ticket 098), and the totals emitter decision
#: 41 pairs with ``runs.totals`` (FR-67, ticket 126), and the turn-seconds emitter decision
#: 54 pairs with ``runs.turn_seconds`` (FR-68, ticket 127), and the gate-split emitter
#: decision 48 pairs with ``read.gate_split`` (FR-69, ticket 128). The two totals emitters of
#: FR-70 (ticket 129) share the name ``totals_payload``, and the spend emitter decision 53 pairs
#: with ``runs.spend`` (FR-74, ticket 133), and the router emitter pairs with ``decisions.
#: probe_state`` (FR-121, decision 67, ticket 186). Nothing else.
SERIALIZERS = frozenset(
    {
        "comparison_payload",
        "gate_split_payload",
        "json_payload",
        "listing_payload",
        "messages_payload",
        "recent_payload",
        "router_json_payload",
        "sessions_payload",
        "spend_payload",
        "summary_payload",
        "totals_payload",
        "turn_seconds_payload",
    }
)


def test_a_handler_calls_no_second_subsystem_callable_beside_its_serializer() -> None:
    """Beyond the one store call, a subsystem module may be reached only for a ``SERIALIZERS``
    name."""
    offenders = []
    for path in route_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = subsystem_aliases(tree)
        for handler in scan(tree):
            for name in handler.other:
                head, _, attr = name.rpartition(".")
                if name == CONNECTIONLESS.get(handler.name):
                    continue
                if head in aliases and attr not in SERIALIZERS:
                    offenders.append(f"{path.name}::{handler.name} also calls {name}")
    assert offenders == []


TWO_CALL_HANDLER = """
from harnessed.evals import runner
from harnessed.memory import semantic

def register(app, prefix):
    @app.get(f"{prefix}/assembled")
    def assembled(conn):
        return {"runs": runner.recent(conn), "facts": semantic.search(conn, "x")}
"""


def test_the_scan_fails_a_handler_that_assembles_two_subsystems() -> None:
    """Not vacuous: a synthetic two-store handler is caught by the same predicate."""
    (handler,) = scan(ast.parse(TWO_CALL_HANDLER))
    assert handler.name == "assembled"
    assert len(handler.store) == 2
    assert set(handler.store) == {"runner.recent", "semantic.search"}


def test_the_registered_surfaces_are_backed_by_the_named_store_callables() -> None:
    reached: set[str] = set()
    for path in route_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = subsystem_aliases(tree)
        for handler in scan(tree):
            for name in (*handler.store, *handler.other):
                if name.rpartition(".")[0] in aliases:
                    reached.add(name)
    missing = REQUIRED_CALLS - reached
    assert missing == set(), f"no handler reaches {sorted(missing)}"
    # The ticket's minimum also names these; they exist in the store even though neither can
    # be the one call a handler makes.
    assert callable(steps.latest) and callable(steps.get)


def test_route_modules_add_no_redaction_pass_and_no_serializer_of_their_own() -> None:
    """The gateway weakens no redaction and re-serializes nothing (Security, decision 19)."""
    for path in route_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            (node.module or "") + "." + alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not any("redact" in name for name in imported), path.name
        assert "json" not in imported, path.name
        assert not any(name.endswith("JSONResponse") for name in imported), path.name


# ---- AC 4, 5, 11: dataclass field names, empty collections at 200, canonical bytes --------------


@pytest.mark.parametrize(("path", "key"), COLLECTIONS)
def test_an_empty_store_answers_an_empty_collection_with_200(
    client: TestClient, path: str, key: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200, response.text
    payload = assert_canonical(response)
    assert payload == {key: []}
    with (EVIDENCE_DIR / "empty-store-200.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"GET {path}\n{response.status_code} {response.text}\n")


def test_a_held_session_with_no_episodes_answers_an_empty_collection_with_200(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    """The episodes half of the empty-store case: the session exists, the store is empty."""
    session_id = seed_session(conn, "hello")
    path = f"/v1/memory/episodes/{session_id}"
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert assert_canonical(response) == {"episodes": []}
    with (EVIDENCE_DIR / "empty-store-200.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"GET {path}\n{response.status_code} {response.text}\n")


@pytest.fixture(autouse=True, scope="module")
def _fresh_evidence() -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "empty-store-200.txt").write_text(
        "ticket 070 - each collection surface over an empty store\n", encoding="utf-8"
    )


def test_collection_rows_carry_the_dataclass_field_names_and_no_nulls(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    session_id = seed_session(conn, "hello")
    seed_eval(conn)
    seed_promotions(conn)
    fact = semantic.put(conn, "the sky is blue", "ticket 070")
    episode = episodic.put(conn, session_id, "transcript", "hello there")
    expectations = {
        "/v1/runs": ("runs", runs.LoopRun),
        "/v1/eval/runs": ("runs", runner.RunSummary),
        "/v1/promotions": ("promotions", procedural.Promotion),
        "/v1/memory/facts?q=sky": ("facts", semantic.Fact),
        f"/v1/memory/episodes/{session_id}": ("episodes", episodic.Episode),
        "/v1/memory/procedures": ("procedures", procedural.Procedure),
    }
    for path, (key, cls) in expectations.items():
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        payload = assert_canonical(response)
        assert set(payload) == {key}, path
        assert payload[key], f"{path} listed nothing after seeding"
        for row in payload[key]:
            assert set(row) == fields_of(cls), path
            assert None not in row.values(), (path, row)
    single = client.get(f"/v1/memory/facts/{fact.mem_id}")
    assert set(assert_canonical(single)) == fields_of(semantic.Fact)
    assert client.get(f"/v1/memory/episodes/{session_id}").json()["episodes"] == projected(
        (episode,)
    )


def test_every_collection_is_the_store_projection_the_cli_prints(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    """``{"<noun>": [asdict(row) ...]}``: the CLI's ``--json`` shape for the memory group."""
    session_id = seed_session(conn, "hello")
    seed_eval(conn)
    seed_promotions(conn)
    semantic.put(conn, "offline first", "ticket 070")
    episodic.put(conn, session_id, "transcript", "hello there")
    expected = {
        "/v1/runs": {"runs": projected(runs.listing(conn, 20))},
        "/v1/eval/runs": {"runs": projected(runner.recent(conn, 20))},
        "/v1/promotions": {"promotions": projected(procedural.promotions(conn, 20))},
        "/v1/memory/facts?q=offline": {"facts": projected(semantic.search(conn, "offline", 10))},
        f"/v1/memory/episodes/{session_id}": {
            "episodes": projected(episodic.for_session(conn, session_id) or ())
        },
        "/v1/memory/procedures": {"procedures": projected(procedural.active(conn))},
    }
    for path, payload in expected.items():
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.content == canonical(payload).encode("utf-8"), path
    assert len(client.get("/v1/memory/procedures").json()["procedures"]) == 1


def test_every_surface_is_served_by_canonical_json_response(
    conn: sqlite3.Connection,
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `/v1/agents` reads `agents.toml` from the working directory: serve a registry this test
    # owns, one whose `use_for` carries a colon-space inside a string value, never the operator's.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents.toml").write_text(
        '[agents.scout]\ncommand = "scout"\nuse_for = "Live facts: today\'s news, prices"\n',
        encoding="utf-8",
    )
    # Priced, so /v1/spend's fake-model handler row carries a cost rather than a `reason` whose
    # prose ("no prices fetched: run `harnessed prices refresh`") reads as a false positive to
    # the colon-space scan below - the scan proves the encoder's own formatting, not the reason
    # vocabulary, which tests/test_spend.py already covers.
    conn.execute(
        "INSERT INTO prices (provider, model, input, output, cached_input, currency, "
        "source_url, fetched_at) VALUES ('fake', ?, '0.01', '0.01', NULL, 'USD', "
        "'https://example.test/models', ?)",
        (MODEL, clock.isoformat(clock.now())),
    )
    conn.commit()
    session_id = seed_session(conn, "hello")
    eval_run_id, case_keys = seed_eval(conn)
    fact = semantic.put(conn, "the sky is blue", "ticket 070")
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    assert client.app.router.default_response_class is CanonicalJSONResponse  # type: ignore[attr-defined]
    paths = [
        "/v1/health",
        "/v1/runs/totals",
        "/v1/runs/turn_seconds",
        "/v1/runs?limit=3",
        f"/v1/runs/{session_id}",
        f"/v1/runs/{case_keys[0]}/trace",
        "/v1/eval/runs",
        f"/v1/eval/runs/{eval_run_id}",
        "/v1/promotions",
        "/v1/memory/facts?q=sky",
        f"/v1/memory/facts/{fact.mem_id}",
        f"/v1/memory/episodes/{session_id}",
        "/v1/memory/procedures",
        f"/v1/sessions/{bus.run_id}/events",
        "/v1/events/gate",
        "/v1/events/totals",
        "/v1/memory/totals",
        "/v1/memory/consolidation",
        "/v1/prices",
        "/v1/providers",
        "/v1/agents",
        "/v1/spend",
        f"/v1/spend?session={session_id}",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        # Parse and re-encode: a colon-space inside a string value is data, a separator is not.
        assert_canonical(response)
        assert b"\n" not in response.content, path
    agents_body = client.get("/v1/agents").json()
    assert "Live facts: today's news" in agents_body["agents"][0]["use_for"]


def test_the_canonical_check_still_rejects_a_spaced_separator() -> None:
    headers = {"content-type": "application/json"}
    spaced = SimpleNamespace(content=b'{"a": 1}', headers=headers, json=lambda: {"a": 1})
    with pytest.raises(AssertionError):
        assert_canonical(spaced)
    value = {"a": "x: y"}
    inside = SimpleNamespace(
        content=canonical(value).encode("utf-8"), headers=headers, json=lambda: value
    )
    assert assert_canonical(inside) == value


# ---- AC 6: an unknown id on a single-entity surface is a 404 in the one body -------------------


@pytest.mark.parametrize("path", SINGLE_ENTITY_MISSES)
def test_an_unknown_id_is_404_in_the_decision_24_body(client: TestClient, path: str) -> None:
    assert_not_found(client.get(path))


# ---- ticket 076: an unheld session id is absent, not empty ------------------------------------

EVIDENCE_076 = REPO_ROOT / "evidence" / "076"


def test_episodes_tell_an_unheld_session_from_a_held_one_with_no_episodes(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    """One test, three answers: unheld 404, held-and-empty 200 ``[]``, held-with-rows 200 rows.

    A client must never have to guess whether ``[]`` means "recorded nothing" or "never
    existed" (decision 24, ticket 076). ``seed_session`` publishes under the id, so a session
    that journaled turns but wrote no episode is the held-and-empty case.
    """
    unheld = "run_00000000000000000000000000"
    absent = client.get(f"/v1/memory/episodes/{unheld}")
    assert_not_found(absent)
    assert unheld in absent.json()["message"]

    empty_session = seed_session(conn, "hello")
    empty = client.get(f"/v1/memory/episodes/{empty_session}")
    assert empty.status_code == 200, empty.text
    assert empty.content == canonical({"episodes": []}).encode("utf-8")

    full_session = seed_session(conn, "hello")
    episode = episodic.put(conn, full_session, "transcript", "hello there")
    full = client.get(f"/v1/memory/episodes/{full_session}")
    assert full.status_code == 200, full.text
    assert full.content == canonical({"episodes": projected((episode,))}).encode("utf-8")
    assert full.json()["episodes"] == projected(episodic.for_run(conn, full_session))

    EVIDENCE_076.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_076 / "absent-vs-empty.txt").write_text(
        "ticket 076 - GET /v1/memory/episodes/{session_id}: absent versus empty\n\n"
        f"unheld id           {absent.status_code} {absent.text}\n"
        f"held, no episodes   {empty.status_code} {empty.text}\n"
        f"held, with episodes {full.status_code} {full.text}\n",
        encoding="utf-8",
    )


# ---- ticket 180: a session's decisions, options nested (FR-120, decision 67) ------------------

EVIDENCE_180 = REPO_ROOT / "evidence" / "180"


def test_a_sessions_decisions_serve_with_options_nested_and_an_unheld_id_is_404(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    """The same three-way proof ``test_episodes_tell_...`` makes for episodes, for decisions:
    an unheld session is 404, a held session that never routed is 200 ``[]``, and a held session
    with a routed decision is 200 with its options nested, every figure the string
    :func:`~harnessed.router.decisions.record_decision` stored."""
    unheld = "run_00000000000000000000000000"
    absent = client.get(f"/v1/sessions/{unheld}/decisions")
    assert_not_found(absent)
    assert unheld in absent.json()["message"]

    empty_session = seed_session(conn, "hello")
    empty = client.get(f"/v1/sessions/{empty_session}/decisions")
    assert empty.status_code == 200, empty.text
    assert empty.content == canonical({"decisions": []}).encode("utf-8")

    full_session = seed_session(conn, "hello")
    answered = jev.Decisions(
        answers={
            "route": jev.Choice(
                value="groq_chat",
                confidence=Decimal("0.93"),
                probabilities={"groq_chat": Decimal("0.93"), "needs_human": Decimal("0.07")},
            ),
            "needs_memory": jev.Noul(probability=Decimal("0.2")),
        },
        id="gen-dec-gw-1",
        model="jev/router-1",
        usage={"input_tokens": 420, "output_tokens": 22},
        cost="0.0000158",
    )
    decisions_store.record_decision(
        conn,
        run_id=full_session,
        message_seq=1,
        task="ping",
        model="jev/router-1",
        latency_seconds="0.4",
        outcome="routed",
        route="groq_chat",
        handler="groq",
        confidence=Decimal("0.93"),
        floor=Decimal("0.5"),
        needs_memory=Decimal("0.2"),
        memory_kind=None,
        stop_reason=None,
        fallback_cause=None,
        state_truncated=False,
        answered=answered,
    )
    conn.commit()
    full = client.get(f"/v1/sessions/{full_session}/decisions")
    assert full.status_code == 200, full.text
    body = full.json()
    (row,) = body["decisions"]
    assert (row["outcome"], row["route"], row["handler"], row["hop"]) == (
        "routed",
        "groq_chat",
        "groq",
        0,
    )
    assert (row["confidence"], row["confidence_percent"]) == ("0.9300", "93%")
    assert (row["needs_memory"], row["needs_memory_percent"]) == ("0.2000", "20%")
    assert row["cost"] == "0.0000158"
    options = {(o["question"], o["option"]): o for o in row["options"]}
    assert set(options) == {("route", "groq_chat"), ("route", "needs_human")}
    assert options[("route", "groq_chat")]["probability"] == "0.9300"
    assert options[("route", "groq_chat")]["probability_percent"] == "93%"
    assert options[("route", "groq_chat")]["description"] is None
    assert full.content == canonical(
        decisions_store.json_payload(decisions_store.for_session(conn, full_session))
    ).encode("utf-8")

    EVIDENCE_180.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_180 / "decisions-body.txt").write_text(
        "ticket 180 - GET /v1/sessions/{session_id}/decisions\n\n"
        f"unheld id           {absent.status_code} {absent.text}\n"
        f"held, no decisions  {empty.status_code} {empty.text}\n"
        f"held, one routed    {full.status_code} {full.text}\n",
        encoding="utf-8",
    )


def test_no_2xx_response_carries_an_error_field(client: TestClient) -> None:
    for path, _ in ROUTE_TABLE:
        filled = re.sub(r"\{\w+\}", "run_00000000000000000000000000", path)
        filled += REQUIRED_QUERY.get(filled, "")
        response = client.get(filled)
        assert response.status_code in (200, 404), (filled, response.status_code)
        if response.status_code == 200:
            assert "error" not in response.json(), filled
        else:
            assert response.json()["error"] == "SurfaceNotFound", filled


# ---- AC 8 and 9: key_kind on the listing, and {run_key} resolves both shapes -------------------


def test_the_listing_labels_one_session_and_five_case_runs_never_six_runs(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    session_id = seed_session(conn, "hello")
    _, case_keys = seed_eval(conn)
    response = client.get("/v1/runs?limit=20")
    assert response.status_code == 200, response.text
    rows = response.json()["runs"]
    assert len(rows) == 6
    assert all("key_kind" in row for row in rows)
    kinds = [row["key_kind"] for row in rows]
    assert kinds.count("session") == 1
    assert kinds.count("case_run") == 5
    assert {row["run_key"] for row in rows if row["key_kind"] == "session"} == {session_id}
    assert {row["run_key"] for row in rows if row["key_kind"] == "case_run"} == set(case_keys)
    assert len(client.get("/v1/runs?limit=2").json()["runs"]) == 2
    assert len(client.get("/v1/runs").json()["runs"]) == 6


def test_a_malformed_limit_is_refused_in_the_one_body_never_fastapi_detail(
    client: TestClient,
) -> None:
    for path in ("/v1/runs?limit=abc", "/v1/eval/runs?limit=-1", "/v1/promotions?limit=0"):
        response = client.get(path)
        assert response.status_code >= 400, path
        payload = assert_canonical(response)
        assert set(payload) == {"error", "message"}, path
        assert "detail" not in payload


# ---- ticket 080: a bad limit is the client's mistake, 400 and never 500 -------------------------

BAD_LIMITS = ("abc", "0", "-1")


def limit_routes(app: Any) -> list[str]:
    """Every registered path whose handler takes ``limit``, read off the app's route table."""
    return sorted(
        route.path
        for route in app.routes
        if "limit" in inspect.signature(route.endpoint).parameters
    )


def test_the_limit_routes_are_discovered_not_listed(client: TestClient) -> None:
    """The discovery must find the four decision 24 names, or the sweep below proves nothing."""
    found_paths = limit_routes(client.app)
    assert {"/v1/runs", "/v1/eval/runs", "/v1/promotions", "/v1/memory/facts"} <= set(found_paths)


def test_every_limit_route_answers_a_bad_limit_with_400_naming_the_value(
    client: TestClient,
) -> None:
    lines: list[str] = []
    for path in limit_routes(client.app):
        assert "{" not in path, path
        for bad in BAD_LIMITS:
            response = client.get(path, params={"limit": bad})
            lines.append(f"GET {path}?limit={bad} -> {response.status_code} {response.text}")
            assert response.status_code == 400, (path, bad, response.text)
            payload = assert_canonical(response)
            assert payload["error"] == "LimitError", (path, bad)
            assert repr(bad) in payload["message"], (path, bad)
    capture_to("080", "bad-limit.txt", "\n".join(lines) + "\n")


def capture_to(ticket: str, name: str, text: str) -> Path:
    """Write one evidence artefact under another ticket's directory; never committed."""
    directory = REPO_ROOT / "evidence" / ticket
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_run_key_resolves_a_session_id_and_a_case_run_key(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    session_id = seed_session(conn, "first", "second")
    _, case_keys = seed_eval(conn)
    assert ":" in case_keys[0] and ":" not in session_id
    for key, kind in ((session_id, "session"), (case_keys[0], "case_run")):
        response = client.get(f"/v1/runs/{key}")
        assert response.status_code == 200, (key, response.text)
        payload = assert_canonical(response)
        assert payload["run_key"] == key
        assert payload["key_kind"] == kind
        assert payload == runs.run_payload(conn, key)
        traced = client.get(f"/v1/runs/{key}/trace")
        assert traced.status_code == 200, (key, traced.text)
        objects = assert_canonical(traced)
        assert objects == list(trace.assemble(conn, key))
        assert len(objects) == payload["iterations"]
        assert [item["run_id"] for item in objects] == [key] * len(objects)


def test_the_eval_run_surface_is_scored_run_through_json_payload(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    eval_run_id, _ = seed_eval(conn)
    response = client.get(f"/v1/eval/runs/{eval_run_id}")
    assert response.status_code == 200, response.text
    scored = runner.scored_run(conn, eval_run_id)
    assert scored is not None
    assert response.content == canonical(runner.json_payload(scored)).encode("utf-8")
    assert response.json()["case_count"] == 5


def test_the_improve_cycle_surface_is_the_cycle_through_json_payload(
    conn: sqlite3.Connection, client: TestClient, tmp_harnessed_home: Path
) -> None:
    cycle = seed_cycle(conn, tmp_harnessed_home)
    response = client.get(f"/v1/improve/cycles/{cycle.improvement_run_id}")
    assert response.status_code == 200, response.text
    assert response.content == canonical(steps.json_payload(cycle)).encode("utf-8")


# ---- AC 10: a seeded secret is served in its redacted form --------------------------------------


def test_a_stored_secret_is_served_redacted_and_the_gateway_adds_no_pass(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    fact = semantic.put(conn, f"the key is {SECRET}", "ticket 070")
    token = redact.token("anthropic_key")
    single = client.get(f"/v1/memory/facts/{fact.mem_id}")
    assert single.status_code == 200, single.text
    assert token in single.json()["text"]
    assert SECRET not in single.text
    searched = client.get("/v1/memory/facts?q=key")
    assert searched.status_code == 200, searched.text
    assert [row["text"] for row in searched.json()["facts"]] == [fact.text]
    assert SECRET not in searched.text
    capture(
        "redacted-payload.txt",
        f"seeded: the key is {SECRET[:7]}...\n"
        f"GET /v1/memory/facts/{fact.mem_id}\n{single.text}\n",
    )


# ---- ticket 147: provider state, by variable name and never by value (FR-89) --------------------

EVIDENCE_147 = REPO_ROOT / "evidence" / "147"

#: A key only the environment holds. Built from parts, like :data:`SECRET`.
PROVIDER_SECRET = "gsk_" + "seeded-for-ticket-147-never-on-the-wire"


def test_providers_serves_one_row_per_registered_provider_and_no_credential(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each registered provider once, ``configured`` by the variable's name, 0 key values."""
    monkeypatch.chdir(tmp_path)  # no `.env` of the operator's can configure a provider here
    for var in config.SECRET_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", PROVIDER_SECRET)
    response = client.get("/v1/providers")
    assert response.status_code == 200, response.text
    assert_canonical(response)
    rows = response.json()["providers"]
    assert [row["name"] for row in rows] == list(config.PROVIDER_KEY_VARS)
    for row in rows:
        assert set(row) == {"name", "configured", "key_var", "default_model"}, row
        assert row["key_var"] == config.PROVIDER_KEY_VARS[row["name"]]
        assert row["default_model"] == quirks.default_model_for(row["name"])
    configured = {row["name"]: row["configured"] for row in rows}
    assert configured["groq"] is True
    assert configured["fake"] is True and configured["ollama"] is True, "they need no key"
    assert [name for name, flag in configured.items() if not flag] == [
        "deepseek",
        "openrouter",
        "openai",
        "anthropic",
    ]
    assert PROVIDER_SECRET not in response.text
    assert PROVIDER_SECRET[4:] not in response.text, "not even the key without its prefix"
    EVIDENCE_147.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_147 / "providers-payload.txt").write_text(
        f"seeded: GROQ_API_KEY={PROVIDER_SECRET[:4]}... in the environment\n"
        f"GET /v1/providers\n{response.text}\n"
        f"grep for the seeded key in the body: {response.text.count(PROVIDER_SECRET)} matches\n",
        encoding="utf-8",
    )


def test_states_reads_the_environment_through_config_and_names_no_value() -> None:
    """``registry.states`` takes resolved settings, and its rows hold no field a key could fill."""
    settings = dataclasses.replace(
        config.load(), _keys={"OPENAI_API_KEY": PROVIDER_SECRET, "GROQ_API_KEY": ""}
    )
    states = registry.states(settings)
    by_name = {state.name: state for state in states}
    assert by_name["openai"].configured is True
    assert by_name["groq"].configured is False, "an empty assignment is not a key"
    assert PROVIDER_SECRET not in repr(states)
    assert PROVIDER_SECRET not in canonical(registry.json_payload(states))
    source = inspect.getsource(registry)
    assert "os.environ" not in source and "getenv" not in source
