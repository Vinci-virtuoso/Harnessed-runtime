"""FR-16: a resumable eval runner over the ``eval_results`` ledger.

Four claims carry this ticket, and each is measured against the thing that could break it.

* **Resuming re-scores nothing.** A run interrupted mid-suite and re-issued without ``fresh``
  keeps its ``eval_run_id`` and issues *zero* solver calls for the cases already on disk -
  counted with a spy rather than asserted about the source - and completes the remainder.
* **A row is on disk before the next case begins.** Observed from inside the run: the spy is
  handed each case as it starts and reads the ledger at that instant, so "persisted before the
  next case" is measured rather than inferred from the order of two statements.
* **Reproducibility is what makes the score a measurement.** Two ``fresh`` runs against the real
  ``FakeProvider``, the real ``run_loop`` and the real registry produce an identical aggregate
  score and identical per-case pass or fail. If they could disagree, the promoter's arithmetic
  in ticket 020 would prove nothing.
* **The runner never re-derives an aggregate or a comparison.** Rule 1: the mean and the epsilon
  live in ``evals/scoring.py``. An AST scan over ``runner.py`` proves it, and the same scan run
  against a synthetic offender proves the scan can fail.

The integration tests deliberately inject the real collaborators. ``metadata["scope"]`` reaching
FakeProvider as the case id is not a claim about a dictionary; it is the claim that the corpus
answers the case, so it is checked by scoring the committed ``smoke`` suite and reading the
numbers the fixtures imply.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from dataclasses import asdict as dataclasses_asdict
from dataclasses import dataclass, field
from dataclasses import fields as dataclasses_fields
from pathlib import Path

import pytest

from harnessed import clock, config
from harnessed.errors import EvalError, SuiteMismatch, SurfaceNotFound
from harnessed.evals import runner, scoring
from harnessed.evals.suite import Case, GraderSpec, Suite, load_named_suite
from harnessed.events.bus import EventBus
from harnessed.events.names import NAMES
from harnessed.improve import promoter
from harnessed.loop.engine import LoopResult, run_loop
from harnessed.memory import db
from harnessed.providers.fake import FakeProvider
from harnessed.providers.types import ChatRequest, ToolCall
from harnessed.tools import default_registry
from support.spies import RecordingObserver, is_subsequence

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
RUNNER_PATH = PACKAGE_ROOT / "evals" / "runner.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "019"

MODEL = "fake-1"
#: The provider every bench run is recorded under (FR-72). ``run_suite`` has no default.
PROVIDER = "fake"

#: The eval run id every zero-case bench below plants under (ticket 096).
ZERO_CASE_RUN_ID = "eval_empty"

#: The four words the ``tiny`` suite below asks for, one per case.
TINY_WORDS = ("alpha", "beta", "gamma", "delta")

#: What the committed ``smoke`` corpus implies at baseline: every case answers as its ``base``
#: fixture does, and ``smoke-003-json-only`` fails because that fixture wraps the object in
#: prose. That failure is the observed one the flywheel is gated on, so it is asserted here
#: rather than treated as a corpus bug.
BASELINE_PASSES = {
    "smoke-001-capital": True,
    "smoke-002-offline-summary": True,
    "smoke-003-json-only": False,
    "smoke-004-version-shape": True,
    "smoke-005-replay": True,
}

#: The traits ``smoke-003-json-only``'s ``with_procedure`` variant is selected on. Appending them
#: to the system prompt is the only difference between the two integration runs below.
PROCEDURE = (
    "When asked for JSON, respond with a single JSON object and no prose before or after it."
)


# ---- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def smoke() -> Suite:
    """The committed primary suite, loaded from disk exactly as the CLI will load it."""
    return load_named_suite("smoke")


def real_solver(home: Path, bus: EventBus | None = None) -> Callable[[ChatRequest], LoopResult]:
    """The real loop, the real FakeProvider and the real registry behind one callable."""
    provider = FakeProvider(config.load())
    registry = default_registry(home, bus=bus)

    def solve(request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        return run_loop(provider, registry, request, limit=6, bus=bus)

    return solve


# ---- the spy the resume and ordering claims are measured with ----------------------------------


@dataclass
class CountingSolver:
    """Answer each case from a table, recording the calls and probing the world at each one.

    ``scopes`` records ``metadata["scope"]`` for every request the runner issued, which is what
    the resume claim counts. ``probe`` is handed the scope at the top of the call, so a test can
    interrupt the run there or read the ledger as it stood a moment earlier.
    """

    replies: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    scopes: list[str] = field(default_factory=list)
    run_keys: list[str | None] = field(default_factory=list)
    probe: Callable[[str], None] | None = None

    def __call__(self, request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        # ``run_key`` is the run journal's key (ticket 045). Recorded rather than ignored:
        # a double that silently swallowed it could not show that each case gets its own.
        self.run_keys.append(run_key)
        scope = request.metadata["scope"]
        self.scopes.append(scope)
        if self.probe is not None:
            self.probe(scope)
        tool = self.tools.get(scope)
        return LoopResult(
            self.replies.get(scope, ""),
            () if tool is None else (ToolCall(id=f"call_{scope}", name=tool),),
            1,
        )


class Boom(RuntimeError):
    """The interruption. Deliberately not a ``HarnessError``: nothing may dress it up."""


def tiny_suite() -> Suite:
    """Four cases, one grader each, scored purely from the reply text."""
    return Suite(
        name="tiny",
        path=Path("tiny.yaml"),
        cases=tuple(
            Case(
                id=f"tiny-{index}",
                prompt=f"say {word}",
                graders=(GraderSpec(name="exact_match", expect={"value": word}),),
            )
            for index, word in enumerate(TINY_WORDS, start=1)
        ),
    )


def answering_all() -> CountingSolver:
    """A solver that answers every ``tiny`` case correctly."""
    return CountingSolver(replies={f"tiny-{i}": w for i, w in enumerate(TINY_WORDS, start=1)})


def scored_rows(connection: sqlite3.Connection, eval_run_id: str) -> tuple[str, ...]:
    """The case ids on disk for one eval run, in insertion order."""
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT case_id FROM eval_results WHERE eval_run_id = ? ORDER BY rowid",
            (eval_run_id,),
        )
    )


# ---- the event vocabulary ---------------------------------------------------------------------


def test_the_runner_publishes_only_frozen_names() -> None:
    assert {
        runner.RUN_STARTED,
        runner.CASE_STARTED,
        runner.CASE_SCORED,
        runner.RUN_FINISHED,
    } <= NAMES


def test_the_public_surface_is_exported() -> None:
    for name in (
        "CaseResult",
        "EvalComparison",
        "EvalRun",
        "Solve",
        "comparison",
        "comparison_payload",
        "json_payload",
        "render_json",
        "run_suite",
    ):
        assert name in runner.__all__


# ---- scoring one suite end to end ---------------------------------------------------------------


def test_a_full_run_scores_every_case_and_persists_one_row_each(conn: sqlite3.Connection) -> None:
    solver = answering_all()
    result = runner.run_suite(tiny_suite(), solver, conn, model=MODEL, provider=PROVIDER)

    assert tuple(case.case_id for case in result.cases) == tiny_suite().case_ids
    assert all(case.passed for case in result.cases)
    assert result.score == 1.0
    assert result.passes == 4
    assert result.case_count == 4
    assert scored_rows(conn, result.eval_run_id) == tiny_suite().case_ids
    assert solver.scopes == list(tiny_suite().case_ids)


def test_the_eval_run_row_is_written_and_finished(conn: sqlite3.Connection) -> None:
    result = runner.run_suite(
        tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER, label="baseline"
    )

    row = conn.execute(
        "SELECT suite, label, started_at, finished_at FROM eval_runs WHERE eval_run_id = ?",
        (result.eval_run_id,),
    ).fetchone()
    assert row is not None
    assert (row[0], row[1]) == ("tiny", "baseline")
    assert row[2] and row[3]


def test_a_failing_case_is_recorded_as_a_failure_rather_than_dropped(
    conn: sqlite3.Connection,
) -> None:
    solver = CountingSolver(replies={"tiny-1": "alpha", "tiny-2": "wrong"})
    result = runner.run_suite(tiny_suite(), solver, conn, model=MODEL, provider=PROVIDER)

    assert {case.case_id: case.passed for case in result.cases} == {
        "tiny-1": True,
        "tiny-2": False,
        "tiny-3": False,
        "tiny-4": False,
    }
    assert result.passes == 1
    assert len(scored_rows(conn, result.eval_run_id)) == 4


def test_every_grader_a_case_declares_must_hold_for_the_case_to_pass(
    conn: sqlite3.Connection,
) -> None:
    """Two graders, one answer: a lucky sentence must not carry a case about tool use."""
    suite = Suite(
        name="two-graders",
        path=Path("two-graders.yaml"),
        cases=(
            Case(
                id="pair-1",
                prompt="read and answer",
                graders=(
                    GraderSpec(name="contains_all", expect={"values": ["offline"]}),
                    GraderSpec(name="tool_called", expect={"name": "read_file"}),
                ),
            ),
        ),
    )
    result = runner.run_suite(
        suite, CountingSolver(replies={"pair-1": "offline"}), conn, model=MODEL, provider=PROVIDER
    )

    assert result.cases[0].passed is False
    assert result.cases[0].score == 0.5


def test_the_eval_run_id_carries_the_documented_prefix(conn: sqlite3.Connection) -> None:
    result = runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    assert result.eval_run_id.startswith("eval_")


# ---- a row is persisted before the next case begins ---------------------------------------------


def test_each_row_is_on_disk_before_the_next_case_begins(conn: sqlite3.Connection) -> None:
    """Observed from inside the run: at the top of case N, cases 1..N-1 are already rows."""
    suite = tiny_suite()
    seen: list[tuple[str, tuple[str, ...]]] = []
    holder: dict[str, str] = {}

    def probe(scope: str) -> None:
        seen.append((scope, scored_rows(conn, holder["id"])))

    solver = CountingSolver(
        replies={f"tiny-{i}": w for i, w in enumerate(TINY_WORDS, start=1)}, probe=probe
    )
    result = runner.run_suite(
        suite,
        solver,
        conn,
        model=MODEL,
        provider=PROVIDER,
        on_start=lambda eval_run_id: holder.update(id=eval_run_id),
    )

    assert [scope for scope, _ in seen] == list(suite.case_ids)
    for index, (scope, rows) in enumerate(seen):
        assert rows == suite.case_ids[:index], f"{scope} began with {rows} on disk"
    assert result.passes == 4


# ---- resuming an interrupted run ---------------------------------------------------------------


def interrupted(connection: sqlite3.Connection, at: str) -> str:
    """Score the ``tiny`` suite until case ``at`` starts, then die. Returns the run id."""
    holder: dict[str, str] = {}

    def probe(scope: str) -> None:
        if scope == at:
            raise Boom(f"interrupted at {scope}")

    solver = CountingSolver(
        replies={f"tiny-{i}": w for i, w in enumerate(TINY_WORDS, start=1)}, probe=probe
    )
    with pytest.raises(Boom):
        runner.run_suite(
            tiny_suite(),
            solver,
            connection,
            model=MODEL,
            provider=PROVIDER,
            on_start=lambda eval_run_id: holder.update(id=eval_run_id),
        )
    return holder["id"]


def test_resuming_re_scores_zero_recorded_cases_and_completes_the_remainder(
    conn: sqlite3.Connection,
) -> None:
    first = interrupted(conn, at="tiny-3")
    assert scored_rows(conn, first) == ("tiny-1", "tiny-2")

    solver = answering_all()
    result = runner.run_suite(tiny_suite(), solver, conn, model=MODEL, provider=PROVIDER)

    assert result.eval_run_id == first, "resuming keeps the interrupted run's id"
    assert result.resumed is True
    assert solver.scopes == ["tiny-3", "tiny-4"], "a recorded case costs zero solver calls"
    assert scored_rows(conn, first) == tiny_suite().case_ids
    assert result.passes == 4
    assert [case.replayed for case in result.cases] == [True, True, False, False]


def test_a_recorded_case_is_replayed_with_its_stored_score(conn: sqlite3.Connection) -> None:
    """The replayed row is the row that was written, not a fresh derivation of it."""
    solver = CountingSolver(replies={"tiny-1": "alpha", "tiny-2": "wrong"})
    first = runner.run_suite(tiny_suite(), solver, conn, model=MODEL, provider=PROVIDER)

    again = runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)

    assert again.eval_run_id == first.eval_run_id
    assert [case.score for case in again.cases] == [case.score for case in first.cases]
    assert [case.passed for case in again.cases] == [case.passed for case in first.cases]
    assert again.score == first.score


def test_re_issuing_a_completed_run_does_no_work_at_all(conn: sqlite3.Connection) -> None:
    first = runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    solver = answering_all()

    again = runner.run_suite(tiny_suite(), solver, conn, model=MODEL, provider=PROVIDER)

    assert again.eval_run_id == first.eval_run_id
    assert solver.scopes == []
    assert all(case.replayed for case in again.cases)


def test_a_case_added_to_a_recorded_suite_is_the_only_one_scored(
    conn: sqlite3.Connection,
) -> None:
    """Done-state is per case, so a grown suite resumes into the same run and scores the gap."""
    runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    grown = Suite(
        name="tiny",
        path=Path("tiny.yaml"),
        cases=(
            *tiny_suite().cases,
            Case(
                id="tiny-5",
                prompt="say epsilon",
                graders=(GraderSpec(name="exact_match", expect={"value": "epsilon"}),),
            ),
        ),
    )
    solver = CountingSolver(replies={"tiny-5": "epsilon"})

    result = runner.run_suite(grown, solver, conn, model=MODEL, provider=PROVIDER)

    assert solver.scopes == ["tiny-5"]
    assert result.case_count == 5
    assert result.passes == 5


def test_a_different_label_is_a_different_run(conn: sqlite3.Connection) -> None:
    """``before`` and ``after`` are two runs of one suite; the label is what separates them."""
    before = runner.run_suite(
        tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER, label="before"
    )
    after = runner.run_suite(
        tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER, label="after"
    )

    assert before.eval_run_id != after.eval_run_id
    assert after.resumed is False


# ---- fresh ------------------------------------------------------------------------------------


def test_fresh_produces_a_new_eval_run_id_and_scores_every_case(conn: sqlite3.Connection) -> None:
    first = runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    solver = answering_all()

    second = runner.run_suite(
        tiny_suite(), solver, conn, model=MODEL, provider=PROVIDER, fresh=True
    )

    assert second.eval_run_id != first.eval_run_id
    assert second.fresh is True
    assert second.resumed is False
    assert solver.scopes == list(tiny_suite().case_ids), "fresh scores every case"
    assert [case.replayed for case in second.cases] == [False] * 4


def test_fresh_after_an_interruption_leaves_the_partial_run_intact(
    conn: sqlite3.Connection,
) -> None:
    first = interrupted(conn, at="tiny-3")
    solver = answering_all()

    second = runner.run_suite(
        tiny_suite(), solver, conn, model=MODEL, provider=PROVIDER, fresh=True
    )

    assert second.eval_run_id != first
    assert solver.scopes == list(tiny_suite().case_ids)
    assert scored_rows(conn, first) == ("tiny-1", "tiny-2"), "the partial run is not rewritten"


# ---- reproducibility, against the real corpus ---------------------------------------------------


def test_two_fresh_runs_against_fakeprovider_agree_exactly(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite
) -> None:
    first = runner.run_suite(
        smoke, real_solver(tmp_harnessed_home), conn, model=MODEL, provider=PROVIDER, fresh=True
    )
    second = runner.run_suite(
        smoke, real_solver(tmp_harnessed_home), conn, model=MODEL, provider=PROVIDER, fresh=True
    )

    assert first.eval_run_id != second.eval_run_id
    assert first.score == second.score
    assert [(case.case_id, case.passed) for case in first.cases] == [
        (case.case_id, case.passed) for case in second.cases
    ]
    assert [case.score for case in first.cases] == [case.score for case in second.cases]


def test_the_scope_of_every_request_is_the_case_id_so_the_corpus_answers_it(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite
) -> None:
    """Not a claim about a dictionary: the committed fixtures are what produce these numbers."""
    result = runner.run_suite(
        smoke, real_solver(tmp_harnessed_home), conn, model=MODEL, provider=PROVIDER
    )

    assert {case.case_id: case.passed for case in result.cases} == BASELINE_PASSES
    assert result.passes == 4


def test_the_system_prompt_reaches_fakeprovider_and_moves_the_json_case(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite
) -> None:
    """The variant predicate reads the system prompt, so the runner must hand it over intact."""
    base = runner.run_suite(
        smoke, real_solver(tmp_harnessed_home), conn, model=MODEL, provider=PROVIDER, fresh=True
    )
    improved = runner.run_suite(
        smoke,
        real_solver(tmp_harnessed_home),
        conn,
        model=MODEL,
        provider=PROVIDER,
        system=PROCEDURE,
        fresh=True,
    )

    assert {case.case_id: case.passed for case in improved.cases}["smoke-003-json-only"] is True
    assert improved.passes == 5
    assert scoring.strictly_greater(improved.score, base.score)


# ---- the four events, in order ---------------------------------------------------------------


def test_the_four_events_are_published_in_order(conn: sqlite3.Connection) -> None:
    observer = RecordingObserver()
    bus = EventBus(run_id="run_eval_events", conn=conn)
    bus.subscribe(observer)

    result = runner.run_suite(
        tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER, bus=bus
    )

    expected = [runner.RUN_STARTED]
    for _ in tiny_suite().case_ids:
        expected += [runner.CASE_STARTED, runner.CASE_SCORED]
    expected.append(runner.RUN_FINISHED)
    assert is_subsequence(expected, observer.names)

    scored = observer.named(runner.CASE_SCORED)
    assert [event.payload["case_id"] for event in scored] == list(tiny_suite().case_ids)

    finished = observer.named(runner.RUN_FINISHED)
    assert len(finished) == 1
    assert finished[0].payload["score"] == result.score
    assert finished[0].payload["eval_run_id"] == result.eval_run_id

    started = observer.named(runner.RUN_STARTED)
    assert started[0].payload["suite"] == "tiny"
    assert started[0].payload["case_count"] == 4


def test_a_replayed_case_still_announces_itself(conn: sqlite3.Connection) -> None:
    """A resumed run stays observable: silence would look like a run that never happened."""
    runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    observer = RecordingObserver()
    bus = EventBus(run_id="run_eval_replayed", conn=conn)
    bus.subscribe(observer)

    runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER, bus=bus)

    scored = observer.named(runner.CASE_SCORED)
    assert len(scored) == 4
    assert all(event.payload["replayed"] is True for event in scored)


def test_a_run_without_a_bus_still_completes(conn: sqlite3.Connection) -> None:
    result = runner.run_suite(
        tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER, bus=None
    )
    assert result.passes == 4


# ---- the machine-readable projection ------------------------------------------------------------


def test_json_is_a_single_object_on_stdout_and_nothing_else(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    result = runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    sys.stdout.write(runner.render_json(result))

    captured = capsys.readouterr()
    assert captured.err == ""
    parsed = json.loads(captured.out)
    assert isinstance(parsed, dict)
    assert json.dumps(parsed, sort_keys=True, separators=(",", ":")) == captured.out


def test_the_json_payload_carries_the_numbers_a_caller_needs(conn: sqlite3.Connection) -> None:
    result = runner.run_suite(
        tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER, label="before"
    )
    payload = runner.json_payload(result)

    assert payload["eval_run_id"] == result.eval_run_id
    assert payload["suite"] == "tiny"
    assert payload["label"] == "before"
    assert payload["score"] == result.score
    assert payload["pass_count"] == 4
    assert payload["case_count"] == 4
    assert payload["resumed"] is False
    assert payload["fresh"] is False
    cases = payload["cases"]
    assert isinstance(cases, list)
    assert [case["case_id"] for case in cases] == list(tiny_suite().case_ids)


def test_two_fresh_runs_render_the_same_json_but_for_the_run_id(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite
) -> None:
    """The evidence artefact: two independent runs, one identical aggregate."""
    first = runner.run_suite(
        smoke, real_solver(tmp_harnessed_home), conn, model=MODEL, provider=PROVIDER, fresh=True
    )
    second = runner.run_suite(
        smoke, real_solver(tmp_harnessed_home), conn, model=MODEL, provider=PROVIDER, fresh=True
    )

    one = runner.json_payload(first)
    two = runner.json_payload(second)
    assert one.pop("eval_run_id") != two.pop("eval_run_id")
    assert one == two

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "eval-run-json.txt").write_text(
        f"{runner.render_json(first)}\n{runner.render_json(second)}\n", encoding="utf-8"
    )


# ---- the aggregate is scoring.py's, and the runner never rebuilds it ---------------------------


def test_the_aggregate_comes_from_the_scoring_module(conn: sqlite3.Connection) -> None:
    solver = CountingSolver(replies={"tiny-1": "alpha", "tiny-2": "beta"})
    result = runner.run_suite(tiny_suite(), solver, conn, model=MODEL, provider=PROVIDER)

    expected = scoring.RunResult.from_cases(
        scoring.CaseOutcome(case.case_id, case.score, case.passed) for case in result.cases
    )
    assert result.result == expected
    assert result.score == expected.score
    assert result.passes == expected.pass_count


def _score_like(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr in {"score", "score_before", "score_after"}
    if isinstance(node, ast.Name):
        return node.id in {"score", "before", "after"}
    return False


def _compares_two_scores(node: ast.Compare) -> bool:
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Gt | ast.Lt | ast.GtE | ast.LtE):
        return False
    return _score_like(node.left) and _score_like(node.comparators[0])


def _rebuilds_a_mean(node: ast.Call) -> bool:
    name = node.func.id if isinstance(node.func, ast.Name) else ""
    attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
    called = name or attr
    return called in {"mean", "fmean", "average"}


def scan_for_score_arithmetic(source: str) -> list[str]:
    """Any mean rebuilt inline, or any two scores compared with an operator, in ``source``."""
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _rebuilds_a_mean(node):
            offenders.append(f"{node.lineno}: rebuilt mean")
        if isinstance(node, ast.Compare) and _compares_two_scores(node):
            offenders.append(f"{node.lineno}: two scores compared inline")
    return offenders


def test_the_runner_neither_rebuilds_a_mean_nor_compares_two_scores() -> None:
    assert scan_for_score_arithmetic(RUNNER_PATH.read_text(encoding="utf-8")) == []


def test_the_scan_catches_a_module_that_does() -> None:
    """Without this half the scan above is a row of assertions that would pass against anything."""
    offender = (
        "from statistics import mean\n"
        "def better(before, after):\n"
        "    total = mean([1.0, 0.0])\n"
        "    return after.score > before.score\n"
    )
    found = scan_for_score_arithmetic(offender)

    assert len(found) == 2
    assert "rebuilt mean" in found[0]
    assert "compared inline" in found[1]


# ---- structure -----------------------------------------------------------------------------------


def imported_modules(path: Path) -> set[str]:
    """Every ``harnessed`` module ``path`` imports, however it spells the import.

    ``from harnessed import clock`` is expanded to ``harnessed.clock`` rather than
    recorded as the bare package: admitting ``harnessed`` into the permitted list would
    admit every module under it by prefix and the check would prove nothing.
    """
    seen: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("harnessed"):
            module = node.module or ""
            if module == "harnessed":
                seen.update(f"harnessed.{alias.name}" for alias in node.names)
            else:
                seen.add(module)
        elif isinstance(node, ast.Import):
            seen.update(alias.name for alias in node.names if alias.name.startswith("harnessed"))
    return seen


def test_the_runner_imports_only_what_the_import_table_permits() -> None:
    """``evals -> loop, memory, providers(types only), events``, plus the leaves."""
    permitted = (
        "harnessed.clock",
        "harnessed.errors",
        "harnessed.evals",
        "harnessed.events",
        "harnessed.ids",
        "harnessed.loop",
        "harnessed.memory",
        "harnessed.providers.types",
    )
    seen = imported_modules(RUNNER_PATH)

    assert seen
    for module in sorted(seen):
        assert any(
            module == allowed or module.startswith(f"{allowed}.") for allowed in permitted
        ), f"{module} is outside the import table for evals/"


def test_the_runner_never_reaches_the_tool_registry() -> None:
    """The wall the injected solver exists for. ``tools`` is absent from the evals import table."""
    seen = imported_modules(RUNNER_PATH)
    assert not any(module.startswith("harnessed.tools") for module in seen)


def test_the_runner_reads_no_process_variable_of_its_own() -> None:
    """Rule 3: ``config.py`` is the only reader; the runner takes what it needs as arguments."""
    source = RUNNER_PATH.read_text(encoding="utf-8")
    assert "environ" not in source
    assert "getenv" not in source


def test_the_runner_generates_no_id_and_reads_no_wall_clock_inline() -> None:
    """Rule 3: both come from their owning module, never from ``uuid`` or ``datetime``."""
    forbidden = {"uuid1", "uuid4", "utcnow", "monotonic", "fromtimestamp"}
    offenders = []
    for node in ast.walk(ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            called = node.func.id if isinstance(node.func, ast.Name) else ""
            attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if called in forbidden or attr in forbidden:
                offenders.append(f"{node.lineno}: {called or attr}")
    assert offenders == []


def test_the_runner_never_prints() -> None:
    """A ``print()`` outside ``cli/render.py`` is a bug; the runner renders a string instead."""
    for node in ast.walk(ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


# ---- named errors and the second attempt -------------------------------------------------------


def test_a_second_attempt_is_its_own_key_rather_than_a_replay(conn: sqlite3.Connection) -> None:
    runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    solver = answering_all()

    second = runner.run_suite(tiny_suite(), solver, conn, model=MODEL, provider=PROVIDER, attempt=2)

    assert solver.scopes == list(tiny_suite().case_ids)
    assert [case.attempt for case in second.cases] == [2] * 4
    assert len(scored_rows(conn, second.eval_run_id)) == 8


def test_a_non_positive_attempt_is_a_named_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(EvalError) as excinfo:
        runner.run_suite(
            tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER, attempt=0
        )
    assert "attempt" in str(excinfo.value)


def test_a_stored_detail_is_scrubbed_before_it_reaches_the_ledger(
    conn: sqlite3.Connection,
) -> None:
    """FR-13: ``tool_called`` reports the tools the model asked for, and a tool name is text the
    model produced. Anything a model produced that reaches a row is scrubbed on the way in.
    """
    secret = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDDEEEEFFFF"
    suite = Suite(
        name="leaky",
        path=Path("leaky.yaml"),
        cases=(
            Case(
                id="leaky-1",
                prompt="use a tool",
                graders=(GraderSpec(name="tool_called", expect={"name": "read_file"}),),
            ),
        ),
    )

    result = runner.run_suite(
        suite, CountingSolver(tools={"leaky-1": secret}), conn, model=MODEL, provider=PROVIDER
    )

    stored = conn.execute(
        "SELECT detail FROM eval_results WHERE eval_run_id = ?", (result.eval_run_id,)
    ).fetchone()
    assert secret not in str(stored[0])
    assert "redacted" in str(stored[0])


def test_a_case_result_is_frozen(conn: sqlite3.Connection) -> None:
    result = runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    with pytest.raises(AttributeError):
        result.cases[0].score = 0.0  # type: ignore[misc]


def test_the_runner_needs_no_socket(conn: sqlite3.Connection, tmp_harnessed_home: Path) -> None:
    """The socket guard is autouse, so a run that touched the network would already have failed."""
    result = runner.run_suite(
        load_named_suite("smoke"),
        real_solver(tmp_harnessed_home),
        conn,
        model=MODEL,
        provider=PROVIDER,
    )
    assert result.case_count == 5


# Ticket 050. The runner can draw more than once, through the attempt seam that already existed.


def test_samples_scores_every_case_n_times_under_its_own_attempt(
    conn: sqlite3.Connection,
) -> None:
    """``eval_results.attempt`` has been in the ledger since ticket 019. This is the first 2."""
    suite = tiny_suite()
    solver = answering_all()
    result = runner.run_suite(suite, solver, conn, model=MODEL, provider=PROVIDER, samples=3)

    first = suite.cases[0].id
    attempts = [
        int(a)
        for (a,) in conn.execute(
            "SELECT attempt FROM eval_results "
            "WHERE eval_run_id = ? AND case_id = ? ORDER BY attempt",
            (result.eval_run_id, first),
        )
    ]
    assert attempts == [1, 2, 3]
    assert result.samples == 3
    assert len(solver.scopes) == 3 * len(suite.cases)


def test_a_case_score_under_samples_is_the_mean_across_attempts(
    conn: sqlite3.Connection,
) -> None:
    """A solver that is right on odd attempts and wrong on even ones scores two thirds."""
    suite = tiny_suite()
    only = Suite(
        name=suite.name,
        description=suite.description,
        path=Path("tiny.yaml"),
        cases=(suite.cases[0],),
    )
    right = answering_all().replies[only.cases[0].id]
    calls = {"n": 0}

    class Alternating:
        def __call__(self, request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
            calls["n"] += 1
            return LoopResult(right if calls["n"] % 2 == 1 else "not it", (), 1)

    result = runner.run_suite(only, Alternating(), conn, model=MODEL, provider=PROVIDER, samples=3)
    (case,) = result.cases
    assert case.score == scoring.mean_score((1.0, 0.0, 1.0))


def test_samples_against_a_replaying_source_is_still_deterministic(
    conn: sqlite3.Connection,
) -> None:
    suite = tiny_suite()
    one = runner.run_suite(
        suite, answering_all(), conn, model=MODEL, provider=PROVIDER, samples=1, fresh=True
    )
    three = runner.run_suite(
        suite, answering_all(), conn, model=MODEL, provider=PROVIDER, samples=3, fresh=True
    )
    assert one.score == three.score


def test_samples_below_one_is_refused_at_the_door(conn: sqlite3.Connection) -> None:
    with pytest.raises(EvalError):
        runner.run_suite(
            tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER, samples=0
        )


# Ticket 058. One run id, one sample count - whichever surface is asked.


def test_a_resumed_run_and_the_ledger_agree_about_one_id(conn: sqlite3.Connection) -> None:
    """Review-gate finding 5: `samples` had two sources that did not have to agree.

    The live path took the caller's argument; ``_folded_cases`` derived it from the ledger. Resume
    is where they part, and resume is the default: score with three draws, run again with no flag,
    and the same ``eval_run_id`` answers 1 to the runner and 3 to ``eval show``.

    Asserted as an agreement between two surfaces rather than against the literal 3. A test pinned
    to a value passes the day both sides are wrong in the same direction, which is the shape this
    milestone has produced eleven times.
    """
    suite = tiny_suite()
    sampled = runner.run_suite(
        suite, answering_all(), conn, model=MODEL, provider=PROVIDER, samples=3, fresh=True
    )
    resumed = runner.run_suite(suite, answering_all(), conn, model=MODEL, provider=PROVIDER)

    assert resumed.eval_run_id == sampled.eval_run_id, "resume must continue the same run id"

    from_ledger = runner.scored_run(conn, resumed.eval_run_id)
    assert from_ledger is not None
    assert resumed.samples == from_ledger.samples
    assert resumed.score == from_ledger.score


def test_evidence_one_id_answers_the_same_to_both_surfaces(conn: sqlite3.Connection) -> None:
    """The artefact for 058, written from a store where the two surfaces used to disagree.

    A suite whose draws all agree cannot show this: the counts would differ while the scores
    matched, and a reader could not tell a fixed fold from a lucky one. ``case-b`` is answered
    correctly on odd draws only, so before this ticket the resumed run reported one draw and a
    score of 1.0 while the ledger reported three draws and a mean of two thirds.
    """
    only = Suite(
        name="tiny",
        description=tiny_suite().description,
        path=Path("tiny.yaml"),
        cases=(tiny_suite().cases[0],),
    )
    right = answering_all().replies[only.cases[0].id]
    calls = {"n": 0}

    class Alternating:
        def __call__(self, request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
            calls["n"] += 1
            return LoopResult(right if calls["n"] % 2 == 1 else "not it", (), 1)

    sampled = runner.run_suite(
        only, Alternating(), conn, model=MODEL, provider=PROVIDER, samples=3, fresh=True
    )
    resumed = runner.run_suite(only, Alternating(), conn, model=MODEL, provider=PROVIDER)
    ledger = runner.scored_run(conn, resumed.eval_run_id)
    assert ledger is not None
    listed = next(r for r in runner.recent(conn) if r.eval_run_id == resumed.eval_run_id)

    assert resumed.samples == ledger.samples == listed.samples
    assert resumed.score == ledger.score == listed.score

    directory = REPO_ROOT / "evidence" / "058"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "one-id-one-count.txt").write_text(
        "\n".join(
            (
                f"run id {resumed.eval_run_id}, one case, drawn 3 times then"
                " resumed with no flag.",
                "The case is answered correctly on odd draws only, so folding changes the answer.",
                "",
                f"  eval run  (first, --samples 3) : samples={sampled.samples}"
                f" score={sampled.score}",
                f"  eval run  (resumed, no flag)   : samples={resumed.samples}"
                f" score={resumed.score}",
                f"  eval show (the ledger)         : samples={ledger.samples}"
                f" score={ledger.score}",
                f"  eval list (the same fold)      : samples={listed.samples}"
                f" score={listed.score}",
                "",
                "Before this ticket the resumed line read samples=1 score=1.0: it drew once,",
                "folded that single draw, and labelled the run with the argument this call",
                "pass. The ledger still held three rows and answered 3. One id, two answers, and",
                "the promoter read whichever function it was given.",
            )
        ),
        encoding="utf-8",
    )


# Ticket 054. `recent` counted ledger rows; a sampled run has N rows per case.


def plant_flaky_run(conn: sqlite3.Connection, eval_run_id: str, *, samples: int = 3) -> None:
    """A two-case run drawn ``samples`` times where folding changes the answer.

    ``case-a`` passes every draw; ``case-b`` passes all but the last. A suite whose draws all
    agree cannot separate the row count from the case count on ``passed``, which is how four
    earlier evidence drafts on this branch proved nothing.
    """
    at = clock.isoformat(clock.now())
    rows: list[tuple[str, tuple[object, ...]]] = [
        (
            "INSERT INTO eval_runs (eval_run_id, suite, label, started_at) VALUES (?, ?, ?, ?)",
            (eval_run_id, "smoke", "baseline", at),
        )
    ]
    for attempt in range(1, samples + 1):
        for case_id, passed in (("case-a", True), ("case-b", attempt < samples)):
            rows.append(
                (
                    "INSERT INTO eval_results (eval_run_id, case_id, attempt, score, passed, "
                    "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (eval_run_id, case_id, attempt, 1.0 if passed else 0.0, int(passed), "[]", at),
                )
            )
    db.write(conn, tuple(rows))


def test_recent_reports_cases_not_ledger_rows(conn: sqlite3.Connection) -> None:
    """A 2-case run drawn 3 times is 2 cases, and 1 of them passed after folding.

    Before this ticket ``_SELECT_RUNS`` counted rows: 6 cases, 5 passed. Both numbers are the
    ledger's shape rather than the run's, and neither is what ``eval show`` reports.
    """
    plant_flaky_run(conn, "eval_flaky")
    (summary,) = (r for r in runner.recent(conn) if r.eval_run_id == "eval_flaky")

    assert summary.case_count == 2, "the row count is 6; the run has 2 cases"
    assert summary.passes == 1, "5 rows carry passed = 1; one case passed every draw"
    assert summary.samples == 3


def test_recent_and_scored_run_agree_about_one_run(conn: sqlite3.Connection) -> None:
    """The two read paths, pinned to each other rather than either to a literal.

    ``recent`` folding in SQL would be a second definition of "did this case pass", and the
    ticket that next changes the fold would move only one of them. This is the assertion that
    goes red if that happens.
    """
    plant_flaky_run(conn, "eval_flaky")
    (summary,) = (r for r in runner.recent(conn) if r.eval_run_id == "eval_flaky")
    rebuilt = runner.scored_run(conn, "eval_flaky")

    assert rebuilt is not None
    assert summary.case_count == rebuilt.case_count
    assert summary.passes == rebuilt.passes
    assert summary.score == rebuilt.score
    assert summary.samples == rebuilt.samples


def test_a_single_sample_run_reads_exactly_as_it_did(conn: sqlite3.Connection) -> None:
    """AC4: one draw per case is the pre-050 path, and its numbers do not move."""
    plant_flaky_run(conn, "eval_once", samples=1)
    (summary,) = (r for r in runner.recent(conn) if r.eval_run_id == "eval_once")

    assert (summary.case_count, summary.passes, summary.samples) == (2, 1, 1)
    assert summary.score == scoring.mean_score((1.0, 0.0))


def plant_zero_case_run(
    conn: sqlite3.Connection,
    eval_run_id: str = ZERO_CASE_RUN_ID,
    *,
    suite: str = "smoke",
    label: str = "baseline",
) -> str:
    """A run that opened and recorded not one case: the shape FR-47 calls a measurement that
    never happened.

    Planted here, once, because more than one ticket needs it: 096 reads it through the four
    eval surfaces and 098 reads it through ``comparison_payload``. Returns the id so a caller
    that plants two runs does not have to spell it twice.
    """
    db.write(
        conn,
        (
            (
                "INSERT INTO eval_runs (eval_run_id, suite, label, started_at) VALUES (?, ?, ?, ?)",
                (eval_run_id, suite, label, clock.isoformat(clock.now())),
            ),
        ),
    )
    return eval_run_id


def test_a_run_that_started_and_died_still_lists(conn: sqlite3.Connection) -> None:
    """No rows is no evidence: zero cases and a fail-closed 0.0, as it read before."""
    plant_zero_case_run(conn)
    (summary,) = (r for r in runner.recent(conn) if r.eval_run_id == "eval_empty")

    assert (summary.case_count, summary.passes, summary.score, summary.samples) == (0, 0, 0.0, 1)


# ---- ticket 075: one emitter for a summary row -------------------------------------------------


def test_summary_payload_is_the_store_projection_and_nothing_else(conn: sqlite3.Connection) -> None:
    """AC1, AC3: ``summary_payload`` is ``asdict(RunSummary)``, key for key.

    The key set is derived from the dataclass, not spelled out, so a field added to ``RunSummary``
    reaches both call sites through this one function or reaches neither. ``passes`` is the key;
    the CLI's hand-built ``pass_count`` was the drift this ticket removes.
    """
    plant_flaky_run(conn, "eval_flaky")
    (summary,) = (r for r in runner.recent(conn) if r.eval_run_id == "eval_flaky")

    payload = runner.summary_payload(summary)

    expected_keys = {f.name for f in dataclasses_fields(runner.RunSummary)}
    assert set(payload) == expected_keys
    assert payload == dataclasses_asdict(summary)
    assert "pass_count" not in payload
    assert payload["passes"] == summary.passes == 1
    assert "summary_payload" in runner.__all__


# ---- ticket 096: a zero-case run serves a null score, not 0.0 ----------------------------------
#
# FR-47, decision 35. The emitters change and the dataclasses do not: ``RunSummary.score`` and
# ``EvalRun.score`` stay ``float`` and keep the fail-closed ``0.0`` the promoter reads, while a
# reader of the four eval surfaces is served ``null`` for a run that measured nothing.


def test_a_zero_case_summary_payload_serves_a_null_score(conn: sqlite3.Connection) -> None:
    """AC1, on the emitter ``eval list --json`` and ``GET /v1/eval/runs`` both call."""
    plant_zero_case_run(conn)
    (summary,) = (r for r in runner.recent(conn) if r.eval_run_id == ZERO_CASE_RUN_ID)

    payload = runner.summary_payload(summary)

    assert payload["score"] is None, "a run with no cases measured nothing; 0.0 is a measurement"
    assert payload["case_count"] == 0
    assert payload["passes"] == 0
    assert payload["samples"] == 1
    # Every other key is still the store projection, key for key (ticket 075).
    assert set(payload) == {f.name for f in dataclasses_fields(runner.RunSummary)}


def test_a_zero_case_json_payload_serves_a_null_score(conn: sqlite3.Connection) -> None:
    """AC1, on the emitter ``eval show --json`` and ``GET /v1/eval/runs/{id}`` both call."""
    plant_zero_case_run(conn)
    rebuilt = runner.scored_run(conn, ZERO_CASE_RUN_ID)

    assert rebuilt is not None
    payload = runner.json_payload(rebuilt)

    assert payload["score"] is None
    assert payload["case_count"] == 0
    assert payload["pass_count"] == 0
    assert payload["cases"] == []
    assert json.loads(runner.render_json(rebuilt))["score"] is None


def test_a_scored_run_serves_the_same_bytes_it_did(conn: sqlite3.Connection) -> None:
    """AC2: a run with at least one case is untouched, pinned as bytes rather than as a shape.

    Ticket 231 added three keys (``retried_calls``, ``score_over``, ``unscored``) and moved no value
    (ticket 095's pin, re-pinned).
    """
    plant_flaky_run(conn, "eval_flaky")
    (summary,) = (r for r in runner.recent(conn) if r.eval_run_id == "eval_flaky")
    rebuilt = runner.scored_run(conn, "eval_flaky")
    assert rebuilt is not None

    assert json.dumps(runner.summary_payload(summary), sort_keys=True) == json.dumps(
        {
            "case_count": 2,
            "eval_run_id": "eval_flaky",
            "label": "baseline",
            # Planted without the 010 columns, so both read as a pre-migration row (ticket 131).
            "model": None,
            "passes": 1,
            "provider": None,
            "samples": 3,
            "score": 0.833333,
            "started_at": summary.started_at,
            "suite": "smoke",
        },
        sort_keys=True,
    )
    assert runner.render_json(rebuilt) == (
        '{"case_count":2,"cases":['
        '{"attempt":1,"case_id":"case-a","passed":true,"replayed":true,"score":1.0},'
        '{"attempt":1,"case_id":"case-b","passed":false,"replayed":true,"score":0.666667}'
        '],"eval_run_id":"eval_flaky","fresh":false,"label":"baseline","model":null,'
        '"pass_count":1,"provider":null,"resumed":true,"retried_calls":0,"samples":3,'
        '"score":0.833333,"score_over":"scored cases only","scored":0,"suite":"smoke",'
        '"unscored":[]}'
    )


def test_the_dataclasses_still_score_a_zero_case_run_fail_closed(
    conn: sqlite3.Connection,
) -> None:
    """AC3: only the emitters changed. Both dataclasses keep ``0.0`` and keep it a ``float``."""
    plant_zero_case_run(conn)
    (summary,) = (r for r in runner.recent(conn) if r.eval_run_id == ZERO_CASE_RUN_ID)
    rebuilt = runner.scored_run(conn, ZERO_CASE_RUN_ID)

    assert rebuilt is not None
    assert summary.score == 0.0 and isinstance(summary.score, float)
    assert rebuilt.score == 0.0 and isinstance(rebuilt.score, float)
    assert rebuilt.result.score == 0.0


def test_zero_evidence_still_loses_the_promotion(conn: sqlite3.Connection) -> None:
    """AC3: the promoter reads the dataclass, not the payload, so its decision is unchanged.

    A zero-case run folded through ``RunResult`` is the "no evidence" the promoter refuses on,
    and it refuses on the same rule and the same numbers it did before this ticket.
    """
    plant_zero_case_run(conn)
    rebuilt = runner.scored_run(conn, ZERO_CASE_RUN_ID)
    assert rebuilt is not None

    empty = rebuilt.result
    baseline = scoring.RunResult.from_cases(
        (scoring.CaseOutcome(case_id="case-a", score=1.0, passed=True),)
    )

    assert empty.cases == ()
    assert empty.score == 0.0
    assert not promoter.decide(baseline, empty, None).promote
    assert promoter.decide(baseline, empty, None).rule == promoter.MISSING_HOLDOUT
    assert not promoter.decide(baseline, empty, empty).promote
    assert not promoter.decide(baseline, baseline, empty).promote


# ---- ticket 098: the comparison of two runs is one store read (FR-62, decision 37) -------------
#
# The wire-level claims are ``tests/test_eval_compare.py``'s. What belongs here is the store read
# itself: that it reads each side once, refuses what it cannot answer, and hands the pair to
# ``evals/scoring.py`` rather than deciding anything about them.


def test_comparison_reads_both_sides_and_carries_scoring_s_own_object(
    conn: sqlite3.Connection,
) -> None:
    """``EvalComparison`` is two ``EvalRun``s and the ``Comparison`` the promoter's own function
    returns for them - equal to it object for object, not merely shaped like it."""
    plant_flaky_run(conn, "eval_before")
    plant_flaky_run(conn, "eval_after")

    compared = runner.comparison(conn, "eval_before", "eval_after")

    assert compared.before.eval_run_id == "eval_before"
    assert compared.after.eval_run_id == "eval_after"
    assert compared.comparison == scoring.compare(compared.before.result, compared.after.result)
    assert "comparison_payload" in runner.__all__
    assert set(runner.comparison_payload(compared)) == {
        "after",
        "before",
        "missing_cases",
        "new_cases",
        "regressions",
        "soft_drops",
    }


def test_comparison_refuses_an_eval_run_id_no_ledger_holds_and_names_the_side(
    conn: sqlite3.Connection,
) -> None:
    """Both sides, because a refusal that only covers one is a refusal half-built (ticket 079)."""
    plant_flaky_run(conn, "eval_held")
    for before, after, side in (
        ("eval_absent", "eval_held", "before"),
        ("eval_held", "eval_absent", "after"),
    ):
        with pytest.raises(SurfaceNotFound) as refused:
            runner.comparison(conn, before, after)
        assert "eval_absent" in str(refused.value)
        assert side in str(refused.value)


def test_comparison_refuses_two_eval_run_ids_of_different_suites(
    conn: sqlite3.Connection,
) -> None:
    """A delta across two suites is a different question, and it is named as one."""
    plant_flaky_run(conn, "eval_smoke")
    other = plant_zero_case_run(conn, eval_run_id="eval_other", suite="regression")

    with pytest.raises(SuiteMismatch) as refused:
        runner.comparison(conn, "eval_smoke", other)

    message = str(refused.value)
    for named in ("eval_smoke", other, "smoke", "regression"):
        assert named in message, message
    assert isinstance(refused.value, EvalError)


def test_a_zero_case_side_is_served_null_while_its_dataclass_keeps_the_fail_closed_zero(
    conn: sqlite3.Connection,
) -> None:
    """Decision 35's seam, on the comparison emitter: the ``None`` is the payload's alone."""
    plant_flaky_run(conn, "eval_measured")
    plant_zero_case_run(conn, eval_run_id="eval_nothing")

    compared = runner.comparison(conn, "eval_nothing", "eval_measured")
    payload = runner.comparison_payload(compared)
    empty = payload["before"]
    assert isinstance(empty, dict)

    assert compared.before.score == 0.0, "the promoter still reads a fail-closed 0.0"
    assert empty["score"] is None
    assert empty["case_count"] == 0
    measured = payload["after"]
    assert isinstance(measured, dict)
    assert measured["score"] == compared.after.score


# ---- ticket 131: an eval run records its provider and model (FR-72) ----------------------------

EVIDENCE_131 = REPO_ROOT / "evidence" / "131"


def test_run_suite_writes_provider_and_model_on_the_eval_run_row(conn: sqlite3.Connection) -> None:
    result = runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    row = conn.execute(
        "SELECT provider, model FROM eval_runs WHERE eval_run_id = ?", (result.eval_run_id,)
    ).fetchone()
    assert tuple(row) == ("fake", MODEL)
    assert (result.provider, result.model) == ("fake", MODEL)


def test_run_suite_has_no_default_provider(conn: sqlite3.Connection) -> None:
    """Every writer names one: a default would stamp a false provider on a live score."""
    with pytest.raises(TypeError):
        runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL)  # type: ignore[call-arg]


def test_a_resumed_run_keeps_the_provider_its_row_was_opened_under(
    conn: sqlite3.Connection,
) -> None:
    """The row is the done-state: resuming reads provider and model off it, never rewrites them."""
    first = runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    again = runner.run_suite(tiny_suite(), answering_all(), conn, model="other", provider="groq")
    assert again.eval_run_id == first.eval_run_id
    assert (again.provider, again.model) == ("fake", MODEL)


def test_the_three_eval_emitters_carry_provider_and_model(conn: sqlite3.Connection) -> None:
    """AC: ``json_payload`` and ``summary_payload`` carry both, read back off the row."""
    result = runner.run_suite(tiny_suite(), answering_all(), conn, model=MODEL, provider=PROVIDER)
    live = runner.json_payload(result)
    shown = runner.scored_run(conn, result.eval_run_id)
    assert shown is not None
    replayed = runner.json_payload(shown)
    (summary,) = runner.recent(conn)
    listed = runner.summary_payload(summary)
    for payload in (live, replayed, listed):
        assert (payload["provider"], payload["model"]) == ("fake", MODEL), payload
    EVIDENCE_131.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_131 / "eval-run-provider.txt").write_text(
        "ticket 131 - an eval run under fake, as eval run --json and eval list --json emit it\n"
        f"{runner.render_json(result)}\n{json.dumps(listed, sort_keys=True)}\n",
        encoding="utf-8",
    )


def test_a_pre_010_eval_run_serves_null_provider_and_model(conn: sqlite3.Connection) -> None:
    """AC: a pre-migration row reads ``null`` on every emitter, never a default provider."""
    plant_flaky_run(conn, "eval_flaky")
    conn.execute(
        "UPDATE eval_runs SET provider = NULL, model = NULL WHERE eval_run_id = 'eval_flaky'"
    )
    (summary,) = (r for r in runner.recent(conn) if r.eval_run_id == "eval_flaky")
    assert (summary.provider, summary.model) == (None, None)
    shown = runner.scored_run(conn, "eval_flaky")
    assert shown is not None
    payload = runner.json_payload(shown)
    assert (payload["provider"], payload["model"]) == (None, None)
