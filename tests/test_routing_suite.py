"""FR-118 (UJ-21, ticket 178): the routing suite scores the router by handler and by exact route.

``corpus/routing/tasks.yaml`` holds the spike's 28 tasks with their accepted routes (research.md,
Appendix A), and ``harnessed eval run routing`` scores them: offline through FakeDecider against
the committed per-task fixtures, live through Jev with the operator's own registry. Every claim
the command prints is checked here by running the command (MUST rule 12): the offline pass is a
real CLI run over the committed corpus, and the seeded wrong fixture is a real corpus copy with one
answer changed, served through the same door.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessed import config, shipped
from harnessed.acp.config import AgentSpec
from harnessed.cli import eval_cmd
from harnessed.cli.app import app, main
from harnessed.evals import runner
from harnessed.memory import db, redact
from harnessed.providers import fake_decider
from harnessed.providers.fake_decider import DEFAULT_CORPUS_ROOT, FakeDecider
from harnessed.providers.jev import Choice, Decisions, Noul, Question
from harnessed.router import suite as routing

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = shipped.routing_tasks()
EVIDENCE = REPO_ROOT / "evidence" / "178"
SPIKE_TASKS = 28
CLAUDE = "claude" "-code"

split = CliRunner(mix_stderr=False)


def evidence(name: str, text: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(text, encoding="utf-8")


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    try:
        yield connection
    finally:
        connection.close()


def scope_for(task: str) -> str:
    """The directory FakeDecider looks in for ``task``: the state's scrubbed ``Task:`` line."""
    return FakeDecider.scope_of(f"Task: {redact.scrub(task)}")


# ---- the committed tasks -----------------------------------------------------------------------


def test_the_committed_suite_holds_the_spikes_28_tasks() -> None:
    loaded = routing.load_routing_suite()

    assert routing.DEFAULT_TASKS_PATH == TASKS_PATH
    assert loaded.name == "routing"
    assert len(loaded.tasks) == SPIKE_TASKS
    assert len({task.id for task in loaded.tasks}) == SPIKE_TASKS
    assert [task.source for task in loaded.tasks].count("operator") == 20
    assert [task.source for task in loaded.tasks].count("written") == 8
    vocabulary = {"groq_chat", "groq_memory", "needs_human", *loaded.routes}
    assert all(task.accepted and set(task.accepted) <= vocabulary for task in loaded.tasks)
    # The spike's draft registry: nine routes, the fixed three plus six entries (research.md).
    assert len(loaded.routes) == 6
    assert {"email_writer", "wireframe_designer", "motion_designer"} <= set(loaded.routes)


def test_every_task_has_a_committed_fixture_that_names_it() -> None:
    loaded = routing.load_routing_suite()
    for task in loaded.tasks:
        base = DEFAULT_CORPUS_ROOT / scope_for(task.task) / "base.json"
        assert base.is_file(), f"{task.id}: no fixture at {base}"
        assert json.loads(base.read_text(encoding="utf-8"))["task"] == task.task, task.id


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("suite: routing\nroutes: {}\ntasks: []\n", "tasks"),
        (
            "suite: routing\nroutes: {}\ntasks:\n  - id: a\n    task: t\n    source: written\n",
            "accepted",
        ),
        (
            "suite: routing\nroutes: {}\ntasks:\n  - id: a\n    task: t\n    accepted: [nowhere]\n"
            "    source: written\n",
            "nowhere",
        ),
        (
            "suite: routing\nroutes: {}\ntasks:\n  - id: a\n    task: t\n"
            "    accepted: [groq_chat]\n    source: guessed\n",
            "source",
        ),
    ],
)
def test_a_malformed_tasks_file_names_the_file_and_the_field(
    tmp_path: Path, body: str, field: str
) -> None:
    path = tmp_path / "tasks.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(routing.SuiteLoadError) as caught:
        routing.load_routing_suite(path)
    assert str(path) in str(caught.value)
    assert field in str(caught.value)


# ---- scoring, pure ------------------------------------------------------------------------------


def test_a_route_maps_to_its_handler() -> None:
    assert routing.handler_of("groq_chat") == "groq"
    assert routing.handler_of("groq_memory") == "groq"
    assert routing.handler_of("needs_human") == "human"
    assert routing.handler_of("wireframe_designer") == "agent"
    assert routing.handler_of("email_writer") == "agent"


class _Scripted:
    """A Decider answering a fixed route per task at a fixed cost, recording what it was asked."""

    def __init__(self, routes: dict[str, str], cost: str) -> None:
        self.routes = routes
        self.cost = cost
        self.asked: list[tuple[Question, ...]] = []

    def decide(self, state: str, questions: tuple[Question, ...]) -> Decisions:
        self.asked.append(questions)
        task = state.splitlines()[0].removeprefix("Task: ")
        route = self.routes[task]
        return Decisions(
            answers={
                "route": Choice(value=route, confidence=Decimal("0.9"), probabilities={}),
                "needs_memory": Noul(probability=Decimal("0.2")),
                "memory_kind": Choice(
                    value="semantic", confidence=Decimal("0.4"), probabilities={}
                ),
            },
            id="gen-dec-scripted",
            model="typesafe/jev-1.13-20260917",
            usage={"cost": self.cost},
            cost=self.cost,
        )


def _two_task_suite() -> routing.RoutingSuite:
    return routing.RoutingSuite(
        name="routing",
        routes={"researcher": "research", "coder": "code"},
        tasks=(
            routing.RoutingTask(
                id="t-1", task="refactor it", accepted=("coder",), source="written"
            ),
            routing.RoutingTask(
                id="t-2", task="what is x", accepted=("groq_chat",), source="written"
            ),
        ),
    )


def test_exact_and_by_handler_are_scored_separately(conn: sqlite3.Connection) -> None:
    decider = _Scripted({"refactor it": "researcher", "what is x": "groq_chat"}, "0.000030")
    ticks = iter([0.0, 0.5, 1.0, 3.0])

    scored = routing.run_routing(_two_task_suite(), decider, conn, timer=lambda: next(ticks))

    assert (scored.exact, scored.by_handler, scored.total) == (1, 2, 2)
    assert [r.exact for r in scored.results] == [False, True]
    assert [r.by_handler for r in scored.results] == [True, True]
    assert scored.failed == ()
    assert scored.cost_total == Decimal("0.000060")
    assert scored.cost_per_decision == Decimal("0.000030")
    assert scored.p50_seconds == 0.5
    assert scored.p95_seconds == 2.0
    # the draft registry is what the route question offers when no operator registry is given
    assert decider.asked[0][0].options == (
        "groq_chat",
        "groq_memory",
        "researcher",
        "coder",
        "needs_human",
    )


def test_a_wrong_handler_is_a_failure_naming_the_task(conn: sqlite3.Connection) -> None:
    decider = _Scripted({"refactor it": "groq_chat", "what is x": "groq_chat"}, "0.00001")

    scored = routing.run_routing(_two_task_suite(), decider, conn)

    assert scored.by_handler == 1
    assert [r.id for r in scored.failed] == ["t-1"]
    message = routing.failure_message(scored)
    assert "t-1" in message and "refactor it" in message and "groq_chat" in message


def test_percentiles_are_nearest_rank() -> None:
    assert routing.percentile([0.3, 0.1, 0.2], 0.5) == 0.2
    assert routing.percentile([float(n) for n in range(1, 21)], 0.95) == 19.0
    assert routing.percentile([], 0.5) is None


# ---- ticket 196: a task whose agent is not installed also accepts groq_chat ----------------------


def _agent_spec(name: str, command: str) -> AgentSpec:
    return AgentSpec(name=name, command=command, use_for="x", not_for="")


def _email_suite() -> routing.RoutingSuite:
    return routing.RoutingSuite(
        name="routing",
        routes={"email_writer": "draft emails"},
        tasks=(
            routing.RoutingTask(
                id="t-1", task="draft an email", accepted=("email_writer",), source="written"
            ),
        ),
    )


def test_an_agent_only_accepted_task_with_no_offered_entry_also_accepts_groq_chat(
    conn: sqlite3.Connection,
) -> None:
    """AC2: the operator's registry may have neither `email_writer` nor `wireframe_designer`
    (ticket 178's finding), so the task also accepts `groq_chat`."""
    decider = _Scripted({"draft an email": "groq_chat"}, "0.00001")

    scored = routing.run_routing(_email_suite(), decider, conn, agents={})

    assert scored.results[0].accepted == ("email_writer", "groq_chat")
    assert scored.results[0].exact is True
    assert scored.results[0].by_handler is True
    assert scored.by_handler == 1
    assert scored.failed == ()


def test_an_installed_entry_keeps_its_own_accepted_route_only(conn: sqlite3.Connection) -> None:
    """AC3: a registry that does install `email_writer` leaves `routing-021` accepting it alone,
    as today."""
    decider = _Scripted({"draft an email": "email_writer"}, "0.00001")
    agents = {"email_writer": _agent_spec("email_writer", sys.executable)}

    scored = routing.run_routing(_email_suite(), decider, conn, agents=agents)

    assert scored.results[0].accepted == ("email_writer",)
    assert scored.by_handler == 1


def test_a_task_accepting_the_shipped_entry_name_is_not_widened(conn: sqlite3.Connection) -> None:
    """Ticket 223: the corpus spells the shipped entry name, so a registry holding that one entry
    offers it and the accepted tuple comes back unchanged; a name it lacks still widens."""
    suite = routing.RoutingSuite(
        name="routing",
        routes={CLAUDE: "research"},
        tasks=(routing.RoutingTask(id="t-1", task="dig", accepted=(CLAUDE,), source="written"),),
    )
    decider = _Scripted({"dig": CLAUDE}, "0.00001")
    agents = {CLAUDE: _agent_spec(CLAUDE, sys.executable)}

    scored = routing.run_routing(suite, decider, conn, agents=agents)
    assert scored.results[0].accepted == (CLAUDE,)

    absent = routing.run_routing(suite, decider, conn, agents={})
    assert absent.results[0].accepted == (CLAUDE, "groq_chat")


def test_a_task_already_accepting_a_fixed_route_is_never_widened(
    conn: sqlite3.Connection,
) -> None:
    """`needs_human` (or either Groq route) is always offered, so a task naming it beside an
    uninstalled agent entry has nothing missing to fall back from."""
    suite = routing.RoutingSuite(
        name="routing",
        routes={CLAUDE: "research"},
        tasks=(
            routing.RoutingTask(
                id="t-1",
                task="ambiguous",
                accepted=(CLAUDE, "needs_human"),
                source="written",
            ),
        ),
    )
    decider = _Scripted({"ambiguous": "needs_human"}, "0.00001")

    scored = routing.run_routing(suite, decider, conn, agents={})

    assert scored.results[0].accepted == (CLAUDE, "needs_human")


def test_the_row_marks_an_accepted_entry_the_registry_does_not_offer() -> None:
    """The example in the ticket's own AC2: `accepted: email_writer (not installed), groq_chat`."""
    assert (
        eval_cmd._accepted_row(("email_writer", "groq_chat"), {})
        == "email_writer (not installed), groq_chat"
    )


def test_the_row_names_an_installed_entry_plainly() -> None:
    agents = {"email_writer": _agent_spec("email_writer", sys.executable)}
    assert eval_cmd._accepted_row(("email_writer",), agents) == "email_writer"


def test_the_row_is_unmarked_offline_with_the_draft_registry() -> None:
    assert eval_cmd._accepted_row(("email_writer", "groq_chat"), None) == "email_writer, groq_chat"


EVIDENCE_196 = REPO_ROOT / "evidence" / "196"


def evidence_196(name: str, text: str) -> None:
    EVIDENCE_196.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_196 / name).write_text(text, encoding="utf-8")


def test_the_021_and_022_rows_are_captured_under_both_registries(
    conn: sqlite3.Connection,
) -> None:
    """AC2/AC3 over the suite's own spike tasks: `routing-021` and `routing-022`, whose only
    accepted route is an agent entry the operator's registry may not have (ticket 178)."""
    loaded = routing.load_routing_suite()
    named = {
        "routing-021-investor-email": "email_writer",
        "routing-022-onboarding-wireframe": "wireframe_designer",
    }
    subset = routing.RoutingSuite(
        name=loaded.name,
        routes=loaded.routes,
        tasks=tuple(task for task in loaded.tasks if task.id in named),
    )
    answers = {task.task: named[task.id] for task in subset.tasks}
    lacking: dict[str, AgentSpec] = {}
    installed = {name: _agent_spec(name, sys.executable) for name in named.values()}

    lines = []
    scores = {}
    for label, agents in (("neither installed", lacking), ("both installed", installed)):
        decider = _Scripted(answers, "0.00003")
        scored = routing.run_routing(subset, decider, conn, agents=agents)
        scores[label] = scored
        lines.append(f"-- {label} --")
        for result in scored.results:
            lines.append(f"{result.id}: accepted {eval_cmd._accepted_row(result.accepted, agents)}")
    evidence_196("registries.txt", "\n".join(lines) + "\n")

    lacking_rows = {result.id: result.accepted for result in scores["neither installed"].results}
    assert lacking_rows["routing-021-investor-email"] == ("email_writer", "groq_chat")
    assert lacking_rows["routing-022-onboarding-wireframe"] == ("wireframe_designer", "groq_chat")
    installed_rows = {result.id: result.accepted for result in scores["both installed"].results}
    assert installed_rows["routing-021-investor-email"] == ("email_writer",)
    assert installed_rows["routing-022-onboarding-wireframe"] == ("wireframe_designer",)


# ---- the command, offline, over the committed corpus ---------------------------------------------


def test_eval_run_routing_offline_scores_28_of_28_by_handler(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    result = split.invoke(app, ["eval", "run", "routing", "--json"])
    evidence("offline.json", result.stdout)

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["suite"] == "routing"
    assert payload["registry"] == "draft"
    assert payload["by_handler"] == {"passed": SPIKE_TASKS, "total": SPIKE_TASKS}
    # Exact is reported apart from handler: the digest run's one soft miss stays a Groq route.
    assert payload["exact"] == {"passed": SPIKE_TASKS - 1, "total": SPIKE_TASKS}
    misses = [row for row in payload["results"] if not row["exact"]]
    assert [(row["id"], row["route"]) for row in misses] == [
        ("routing-028-linkedin-post", "groq_memory")
    ]
    assert payload["failed"] == []
    assert payload["cost_per_decision"] is not None
    assert payload["latency_p50_seconds"] is not None
    assert payload["latency_p95_seconds"] is not None


def test_eval_run_routing_prints_exact_and_handler_apart_in_text(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    result = split.invoke(app, ["eval", "run", "routing", "--fresh"])
    evidence("offline.txt", "$ harnessed eval run routing --fresh\n" + result.stdout)

    assert result.exit_code == 0, result.stderr
    assert f"by handler {SPIKE_TASKS}/{SPIKE_TASKS}" in result.stdout
    assert f"exact route {SPIKE_TASKS - 1}/{SPIKE_TASKS}" in result.stdout
    assert "routing-028-linkedin-post" in result.stdout


def test_a_seeded_wrong_fixture_fails_the_suite_naming_the_task(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """MUST rule 12: the failure is shown by serving a wrong answer, not by reading a message.

    Run through ``main``, argv and all, as ``tests/test_cli.py`` runs every failure: that is the
    door an operator uses, and the one whose stderr and exit code are the error contract."""
    corpus = tmp_path / "jev"
    shutil.copytree(DEFAULT_CORPUS_ROOT, corpus)
    seeded = "Draft a warm follow-up email to the investor I met on Tuesday about the seed round"
    target = corpus / scope_for(seeded) / "base.json"
    body = json.loads(target.read_text(encoding="utf-8"))
    body["answers"]["route"]["choice"] = "groq_chat"
    target.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(fake_decider, "DEFAULT_CORPUS_ROOT", corpus)
    monkeypatch.setattr(sys, "argv", ["harnessed", "eval", "run", "routing"])

    with pytest.raises(SystemExit) as exit_info:
        main()
    captured = capsys.readouterr()
    evidence(
        "seeded-wrong.txt",
        f"$ harnessed eval run routing   (seeded wrong fixture, exit {exit_info.value.code})\n"
        + captured.out
        + f"stderr:\n{captured.err}",
    )

    assert exit_info.value.code == 1
    assert f"by handler {SPIKE_TASKS - 1}/{SPIKE_TASKS}" in captured.out
    assert "routing-021-investor-email" in captured.err
    assert seeded in captured.err
    assert "Traceback" not in captured.err


def test_the_routing_suite_writes_nothing_the_promoter_reads(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    """The suite never reaches the promoter: a routing run files no eval run in the ledger."""
    result = split.invoke(app, ["eval", "run", "routing", "--json"])
    assert result.exit_code == 0, result.stderr
    with closing(db.connect()) as conn:
        assert runner.recent(conn, limit=10) == ()


def test_improve_never_names_the_routing_suite() -> None:
    improve = REPO_ROOT / "src" / "harnessed" / "improve"
    named = [
        path.name
        for path in improve.rglob("*.py")
        if "routing" in (text := path.read_text(encoding="utf-8")) or "harnessed.router" in text
    ]
    assert named == []


# ---- the live tier, operator-run (never part of verify) ------------------------------------------


@pytest.mark.live
def test_live_routing_suite_records_scores_latency_and_cost() -> None:
    """Through Jev with the operator's own registry and memory, deselected by default. Records the
    evidence ticket 179 reads; the 28 / 28 by handler it needs is that ticket's gate, not this."""
    settings = config.load()
    if settings.api_key_for("openrouter") is None:
        pytest.skip("no live key for openrouter")
    if not Path("agents.toml").is_file():
        pytest.skip("no agents.toml in the working directory: the live tier asks with it")

    result = split.invoke(app, ["eval", "run", "routing", "--fresh", "--json"])
    evidence(
        "routing-live.txt",
        f"$ harnessed eval run routing --fresh --json   (live, exit {result.exit_code})\n"
        + result.stdout
        + (f"stderr:\n{result.stderr}" if result.stderr else ""),
    )

    assert result.exit_code in (0, 1), result.stderr
    payload = json.loads(result.stdout)
    assert payload["registry"] == "operator"
    assert payload["by_handler"]["total"] == SPIKE_TASKS
    assert payload["latency_p50_seconds"] is not None
    assert payload["latency_p95_seconds"] is not None
    assert payload["cost_per_decision"] is not None
