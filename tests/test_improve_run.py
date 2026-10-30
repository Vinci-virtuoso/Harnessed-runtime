"""FR-18: the flywheel cycle survives an interruption, because every step is a ledger row.

The improvement cycle is the longest-running operation in the milestone - two eval suites, a
reflection and a promotion - so it is the one that most needs to resume rather than restart.
Four claims carry this file, and each is measured against the thing that would quietly break it:

* **A completed step is never repeated.** Measured by interrupting a cycle at a named step and
  re-issuing it, then counting the work the second pass actually paid for: zero solver calls for
  the cases already scored, zero reflection requests, and no second ``eval_runs`` row. Asserting
  the final ledger alone would only show that the rows exist, never that the resumed pass
  refused to rebuild them.
* **A recorded payload is recoverable without re-execution.** The resumed cycle reconstructs
  every number it reports from ``improvement_steps`` and nothing else, which is checked by
  reading the row back and comparing it to what the first pass returned - detail included, since
  the reflection prompt is built from it.
* **The row is on disk before the next step begins.** Proved from inside the run through a probe
  fired at the top of each step, reading the ledger over its *own* connection: a row a second
  connection can see is a row that is committed, not one that is merely queued.
* **The candidate is gated on an observed failure.** A baseline that does not fail the case the
  ``_reflect`` corpus keys on selects the ``base`` variant, which answers with nothing, and the
  cycle reports no candidate rather than inventing one. That is the same gate the FR-25 proof
  rides on, so it is asserted here against the real ``FakeProvider`` and the committed corpus.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from harnessed import config
from harnessed.errors import ImproveError
from harnessed.evals.suite import Case, GraderSpec, Suite, load_named_suite
from harnessed.events.bus import EventBus
from harnessed.events.names import NAMES
from harnessed.improve import candidate as candidate_module
from harnessed.improve import steps as steps_module
from harnessed.improve.promoter import FAKE
from harnessed.loop.engine import LoopResult, run_loop
from harnessed.loop.prompt import build_system_prompt
from harnessed.memory import db, procedural
from harnessed.providers.fake import FakeProvider
from harnessed.providers.types import ChatRequest, ChatResponse
from harnessed.tools import default_registry
from support.spies import RecordingObserver, is_subsequence

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "026"

MODEL = "fake-1"

#: The one smoke case the committed corpus genuinely fails at baseline. The whole flywheel is
#: gated on that observed failure (see ``tests/test_eval_runner.py``).
FAILING_CASE = "smoke-003-json-only"

#: What the ``_reflect`` corpus answers when that case failed. Asserted rather than paraphrased:
#: the candidate id is the hash of this text, so a corpus edit must break this file loudly.
AUTHORED = (
    "When the task asks for structured data, respond with a single JSON object and no prose "
    "before or after it."
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
    return load_named_suite("smoke")


@pytest.fixture
def holdout() -> Suite:
    return load_named_suite("holdout")


def real_solver(home: Path, bus: EventBus | None = None) -> Callable[[ChatRequest], LoopResult]:
    """The real loop, the real FakeProvider and the real registry behind one callable."""
    provider = FakeProvider(config.load())
    registry = default_registry(home, bus=bus)

    def solve(request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        return run_loop(provider, registry, request, limit=6, bus=bus)

    return solve


@dataclass
class CountingModel:
    """The real ``FakeProvider`` behind a counter, so a replayed reflection is observable."""

    inner: FakeProvider
    calls: int = 0

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        return self.inner.complete(request)


@dataclass
class CountingSolver:
    """Answer each case from a table and record the scope of every request issued."""

    replies: dict[str, str] = field(default_factory=dict)
    scopes: list[str] = field(default_factory=list)
    run_keys: list[str | None] = field(default_factory=list)

    def __call__(self, request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        self.run_keys.append(run_key)
        scope = str(request.metadata["scope"])
        self.scopes.append(scope)
        return LoopResult(self.replies.get(scope, ""), (), 1)


class Boom(RuntimeError):
    """The interruption. Deliberately not a ``HarnessError``: nothing may dress it up."""


def tiny_suite() -> Suite:
    """Two cases the ``_reflect`` corpus has no variant for, one of which fails."""
    return Suite(
        name="tiny",
        path=Path("tiny.yaml"),
        cases=(
            Case(
                id="tiny-1-alpha",
                prompt="say alpha",
                graders=(GraderSpec(name="exact_match", expect={"value": "alpha"}),),
            ),
            Case(
                id="tiny-2-beta",
                prompt="say beta",
                graders=(GraderSpec(name="exact_match", expect={"value": "beta"}),),
            ),
        ),
    )


def cycle(
    conn: sqlite3.Connection,
    home: Path,
    suite: Suite,
    holdout: Suite | None,
    *,
    solve: Callable[[ChatRequest], LoopResult] | None = None,
    reflector: steps_module.Model | None = None,
    dry_run: bool = False,
    fresh: bool = False,
    bus: EventBus | None = None,
    on_step: Callable[[str], None] | None = None,
) -> steps_module.Cycle:
    """One cycle with every seam wired the way a composition root wires it."""
    return steps_module.run_cycle(
        conn,
        suite=suite,
        holdout=holdout,
        solve=real_solver(home, bus) if solve is None else solve,
        reflector=FakeProvider(config.load()) if reflector is None else reflector,
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
        dry_run=dry_run,
        fresh=fresh,
        bus=bus,
        on_step=on_step,
    )


def recorded_steps(connection: sqlite3.Connection, improvement_run_id: str) -> tuple[str, ...]:
    """The step names on disk for one improvement run, in insertion order."""
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT step FROM improvement_steps WHERE improvement_run_id = ? ORDER BY rowid",
            (improvement_run_id,),
        )
    )


def counted(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(row[0])


# ---- the vocabulary and the surface -------------------------------------------------------------


def test_the_cycle_publishes_only_frozen_names() -> None:
    assert {steps_module.RUN_STARTED, steps_module.STEP_RECORDED} <= NAMES


def test_the_public_surface_is_exported() -> None:
    for name in (
        "Cycle",
        "Measurement",
        "STEPS",
        "Step",
        "get",
        "json_payload",
        "put",
        "record",
        "render_json",
        "run_cycle",
    ):
        assert name in steps_module.__all__


def test_the_step_vocabulary_is_the_cycle_in_order() -> None:
    assert steps_module.STEPS == (
        steps_module.BASELINE,
        steps_module.REFLECT,
        steps_module.CANDIDATE,
        steps_module.HOLDOUT,
        steps_module.PROMOTE,
    )


# ---- the ledger itself --------------------------------------------------------------------------


def test_a_recorded_step_is_returned_rather_than_repeated(conn: sqlite3.Connection) -> None:
    run_id, _ = steps_module.start(conn, "smoke")
    calls = 0

    def work() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"answer": 41 + calls}

    first = steps_module.record(conn, run_id, steps_module.BASELINE, "k", work)
    second = steps_module.record(conn, run_id, steps_module.BASELINE, "k", work)

    assert calls == 1
    assert first.replayed is False
    assert second.replayed is True
    assert second.payload == first.payload == {"answer": 42}
    assert counted(conn, "improvement_steps") == 1


def test_the_key_is_the_triple(conn: sqlite3.Connection) -> None:
    run_id, _ = steps_module.start(conn, "smoke")
    steps_module.put(conn, run_id, steps_module.BASELINE, "one", {"n": 1})
    steps_module.put(conn, run_id, steps_module.BASELINE, "two", {"n": 2})
    steps_module.put(conn, run_id, steps_module.REFLECT, "one", {"n": 3})

    assert counted(conn, "improvement_steps") == 3
    stored = steps_module.get(conn, run_id, steps_module.BASELINE, "two")
    assert stored is not None and stored.payload == {"n": 2}
    assert steps_module.get(conn, run_id, steps_module.HOLDOUT, "one") is None


def test_a_step_payload_is_scrubbed_before_it_reaches_the_row(conn: sqlite3.Connection) -> None:
    secret = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDDEEEEFFFF"
    run_id, _ = steps_module.start(conn, "smoke")
    steps_module.put(conn, run_id, steps_module.REFLECT, "k", {"note": f"key {secret} here"})

    raw = str(conn.execute("SELECT payload FROM improvement_steps").fetchone()[0])
    assert secret not in raw
    assert "[redacted:anthropic_key]" in raw


# ---- AC 1: the dry run ---------------------------------------------------------------------------


def test_a_dry_run_produces_a_candidate_and_writes_no_active_procedure(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    result = cycle(conn, tmp_harnessed_home, smoke, holdout, dry_run=True)

    assert result.dry_run is True
    assert result.cand_id.startswith("cand_")
    assert result.cand_id == candidate_module.candidate_id(AUTHORED, (FAILING_CASE,))
    assert candidate_module.get(conn, result.cand_id) is not None
    assert result.outcome is None
    assert counted(conn, "active_procedures") == 0
    assert counted(conn, "promotions") == 0
    assert procedural.active(conn) == ()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "improve-dry-run.txt").write_text(
        f"{steps_module.describe(result, provider=FAKE)}\n\n{steps_module.render_json(result)}\n",
        encoding="utf-8",
    )


def test_a_dry_run_reports_the_candidate_id(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    result = cycle(conn, tmp_harnessed_home, smoke, holdout, dry_run=True)
    report = steps_module.describe(result, provider=FAKE)

    assert result.cand_id in report
    assert "dry run" in report.casefold()
    assert "no candidate" not in report.casefold()


def test_the_candidate_id_carries_no_improvement_run_id(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    first = cycle(conn, tmp_harnessed_home, smoke, holdout, dry_run=True)
    second = cycle(conn, tmp_harnessed_home, smoke, holdout, dry_run=True, fresh=True)

    assert first.improvement_run_id != second.improvement_run_id
    assert first.cand_id == second.cand_id


# ---- AC 2 and AC 3: resume -----------------------------------------------------------------------


def test_an_interrupted_cycle_resumes_at_the_first_unrecorded_step(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    reflector = CountingModel(FakeProvider(config.load()))

    def interrupt(step: str) -> None:
        if step == steps_module.CANDIDATE:
            raise Boom(step)

    with pytest.raises(Boom):
        cycle(conn, tmp_harnessed_home, smoke, holdout, reflector=reflector, on_step=interrupt)

    assert reflector.calls == 1
    interrupted = steps_module.latest(conn, smoke.name)
    assert interrupted is not None
    assert recorded_steps(conn, interrupted) == (steps_module.BASELINE, steps_module.REFLECT)
    eval_runs_after_interrupt = counted(conn, "eval_runs")

    result = cycle(conn, tmp_harnessed_home, smoke, holdout, reflector=reflector)

    assert result.improvement_run_id == interrupted
    assert result.resumed is True
    assert reflector.calls == 1
    assert counted(conn, "eval_runs") == eval_runs_after_interrupt + 2
    assert recorded_steps(conn, result.improvement_run_id) == steps_module.STEPS
    assert [step.step for step in result.steps if step.replayed] == [
        steps_module.BASELINE,
        steps_module.REFLECT,
    ]
    assert result.promoted is True


def test_a_resumed_cycle_re_scores_nothing_it_already_scored(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    suite = tiny_suite()
    solver = CountingSolver(replies={"tiny-1-alpha": "alpha", "tiny-2-beta": "beta"})

    def interrupt(step: str) -> None:
        if step == steps_module.REFLECT:
            raise Boom(step)

    with pytest.raises(Boom):
        cycle(conn, tmp_harnessed_home, suite, None, solve=solver, on_step=interrupt)

    assert solver.scopes == ["tiny-1-alpha", "tiny-2-beta"]
    solver.scopes.clear()

    result = cycle(conn, tmp_harnessed_home, suite, None, solve=solver)

    assert solver.scopes == []
    assert result.before is not None
    assert result.before.result.score == 1.0


def test_a_completed_step_is_fully_recoverable_from_its_row(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    first = cycle(conn, tmp_harnessed_home, smoke, holdout, dry_run=True)
    assert first.before is not None

    stored = steps_module.get(conn, first.improvement_run_id, steps_module.BASELINE, smoke.name)
    assert stored is not None

    recovered = steps_module.measurement(stored)
    assert recovered == first.before
    assert recovered.result == first.before.result
    assert [case.detail for case in recovered.cases] == [case.detail for case in first.before.cases]
    assert {case.case_id: case.passed for case in recovered.cases}[FAILING_CASE] is False


def test_a_dry_run_followed_by_a_real_run_resumes_at_the_promotion(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    dry = cycle(conn, tmp_harnessed_home, smoke, holdout, dry_run=True)
    assert recorded_steps(conn, dry.improvement_run_id) == steps_module.STEPS[:-1]

    real = cycle(conn, tmp_harnessed_home, smoke, holdout)

    assert real.improvement_run_id == dry.improvement_run_id
    assert real.cand_id == dry.cand_id
    assert real.promoted is True
    assert [step.step for step in real.steps if not step.replayed] == [steps_module.PROMOTE]
    assert counted(conn, "promotions") == 1
    assert [procedure.text for procedure in procedural.active(conn)] == [AUTHORED]


def test_a_second_pass_over_a_finished_cycle_changes_nothing(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    first = cycle(conn, tmp_harnessed_home, smoke, holdout)
    before = (counted(conn, "promotions"), counted(conn, "improvement_steps"))

    second = cycle(conn, tmp_harnessed_home, smoke, holdout)

    assert second.improvement_run_id == first.improvement_run_id
    assert second.promoted == first.promoted
    assert steps_module.json_payload(second) == steps_module.json_payload(first) | {
        "resumed": True,
        "scored": 0,
    }
    assert (counted(conn, "promotions"), counted(conn, "improvement_steps")) == before


# ---- AC 4: persisted before the next step begins -------------------------------------------------


def test_each_step_row_is_on_disk_before_the_next_step_begins(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """Read the ledger over a second connection at the top of each step.

    A row a connection that did not write it can see is a row that is committed rather than one
    that is merely queued, which is the claim resumability actually rests on.
    """
    observed: dict[str, tuple[str, ...]] = {}

    def probe(step: str) -> None:
        witness = db.connect()
        try:
            run_id = steps_module.latest(witness, smoke.name)
            observed[step] = () if run_id is None else recorded_steps(witness, run_id)
        finally:
            witness.close()

    result = cycle(conn, tmp_harnessed_home, smoke, holdout, on_step=probe)

    assert observed[steps_module.BASELINE] == ()
    for index, step in enumerate(steps_module.STEPS[1:], start=1):
        assert observed[step] == steps_module.STEPS[:index], step
    assert recorded_steps(conn, result.improvement_run_id) == steps_module.STEPS


# ---- AC 5: no candidate --------------------------------------------------------------------------


def test_a_baseline_that_fails_nothing_the_corpus_keys_on_yields_no_candidate(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    suite = tiny_suite()
    solver = CountingSolver(replies={"tiny-1-alpha": "alpha", "tiny-2-beta": "wrong"})

    result = cycle(conn, tmp_harnessed_home, suite, None, solve=solver)

    assert result.before is not None
    assert {case.case_id: case.passed for case in result.before.cases} == {
        "tiny-1-alpha": True,
        "tiny-2-beta": False,
    }
    assert result.cand_id == ""
    assert result.has_candidate is False
    assert result.after is None
    assert result.outcome is None
    assert "no candidate" in steps_module.describe(result, provider=FAKE).casefold()
    assert recorded_steps(conn, result.improvement_run_id) == (
        steps_module.BASELINE,
        steps_module.REFLECT,
    )
    assert counted(conn, "candidates") == 0
    assert counted(conn, "active_procedures") == 0


def test_the_null_candidate_is_recorded_so_a_resumed_cycle_does_not_re_reflect(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    suite = tiny_suite()
    solver = CountingSolver(replies={"tiny-1-alpha": "alpha", "tiny-2-beta": "wrong"})
    reflector = CountingModel(FakeProvider(config.load()))

    first = cycle(conn, tmp_harnessed_home, suite, None, solve=solver, reflector=reflector)
    second = cycle(conn, tmp_harnessed_home, suite, None, solve=solver, reflector=reflector)

    assert reflector.calls == 1
    assert second.cand_id == first.cand_id == ""
    assert second.resumed is True


# ---- AC 6: the events ----------------------------------------------------------------------------


def test_the_cycle_publishes_started_and_one_recorded_per_step(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    bus = EventBus(conn=conn)
    observer = bus.subscribe(RecordingObserver())

    result = cycle(conn, tmp_harnessed_home, smoke, holdout, bus=bus)

    names = [event.name for event in observer.events]
    assert is_subsequence([steps_module.RUN_STARTED, steps_module.STEP_RECORDED], names)
    assert names.count(steps_module.RUN_STARTED) == 1
    assert names.count(steps_module.STEP_RECORDED) == len(steps_module.STEPS)

    started = next(event for event in observer.events if event.name == steps_module.RUN_STARTED)
    assert started.payload["improvement_run_id"] == result.improvement_run_id
    assert started.payload["suite"] == smoke.name
    assert started.payload["resumed"] is False

    recorded = [event for event in observer.events if event.name == steps_module.STEP_RECORDED]
    assert [event.payload["step"] for event in recorded] == list(steps_module.STEPS)


def test_a_replayed_step_publishes_nothing(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    cycle(conn, tmp_harnessed_home, smoke, holdout)

    bus = EventBus(conn=conn)
    observer = bus.subscribe(RecordingObserver())
    cycle(conn, tmp_harnessed_home, smoke, holdout, bus=bus)

    names = [event.name for event in observer.events]
    assert steps_module.STEP_RECORDED not in names
    assert names.count(steps_module.RUN_STARTED) == 1
    assert names[-1] == steps_module.RUN_FINISHED, "a replayed cycle still ends (ticket 081)"


# ---- ticket 081: the cycle announces its own end ----------------------------------------------


def test_the_finish_name_is_frozen_beside_its_start() -> None:
    assert steps_module.RUN_FINISHED == "improve.run.finished"
    assert steps_module.RUN_FINISHED in NAMES


@pytest.mark.parametrize("path", ["promotion", "dry_run", "no_candidate"])
def test_every_return_path_publishes_the_finish_last_and_once(
    conn: sqlite3.Connection,
    tmp_harnessed_home: Path,
    smoke: Suite,
    holdout: Suite,
    path: str,
) -> None:
    bus = EventBus(conn=conn)
    observer = bus.subscribe(RecordingObserver())
    if path == "no_candidate":
        result = cycle(
            conn, tmp_harnessed_home, tiny_suite(), None, solve=CountingSolver(), bus=bus
        )
        assert not result.has_candidate
    else:
        result = cycle(conn, tmp_harnessed_home, smoke, holdout, dry_run=path == "dry_run", bus=bus)
        assert result.has_candidate

    names = [event.name for event in observer.events]
    assert names[-1] == steps_module.RUN_FINISHED
    assert names.count(steps_module.RUN_FINISHED) == 1
    finished = observer.events[-1].payload
    assert finished["improvement_run_id"] == result.improvement_run_id
    assert finished["cand_id"] == result.cand_id
    assert finished["dry_run"] is (path == "dry_run")
    assert finished["promoted"] is result.promoted
    assert result.promoted is (path == "promotion")


def test_a_cycle_that_raises_publishes_no_finish(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    bus = EventBus(conn=conn)
    observer = bus.subscribe(RecordingObserver())

    def interrupt(name: str) -> None:
        if name == steps_module.HOLDOUT:
            raise Boom(name)

    with pytest.raises(Boom):
        cycle(conn, tmp_harnessed_home, smoke, holdout, bus=bus, on_step=interrupt)
    names = [event.name for event in observer.events]
    assert steps_module.RUN_STARTED in names
    assert steps_module.RUN_FINISHED not in names


# ---- AC 7: the machine-readable projection -------------------------------------------------------


def test_json_is_a_single_object_on_stdout_and_nothing_else(
    conn: sqlite3.Connection,
    tmp_harnessed_home: Path,
    smoke: Suite,
    holdout: Suite,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = cycle(conn, tmp_harnessed_home, smoke, holdout)
    sys.stdout.write(steps_module.render_json(result))
    captured = capsys.readouterr()

    assert captured.err == ""
    parsed = json.loads(captured.out)
    assert isinstance(parsed, dict)
    assert json.dumps(parsed, sort_keys=True, separators=(",", ":")) == captured.out


def test_the_json_payload_carries_the_numbers_a_caller_needs(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    result = cycle(conn, tmp_harnessed_home, smoke, holdout)
    payload = steps_module.json_payload(result)

    assert payload["improvement_run_id"] == result.improvement_run_id
    assert payload["suite"] == smoke.name
    assert payload["cand_id"] == result.cand_id
    assert payload["promoted"] is True
    assert payload["dry_run"] is False
    assert payload["resumed"] is False
    assert payload["rule"] == "score"
    assert payload["regressions"] == 0
    assert isinstance(payload["score_before"], float)
    assert float(payload["score_after"]) > float(payload["score_before"])
    assert payload["steps"] == list(steps_module.STEPS)


# ---- refusals, and the paths a cycle takes when something is missing ----------------------------


def test_the_run_id_reaches_the_caller_before_the_first_step(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """The id is chosen inside, and a caller interrupted mid-cycle still has to name the run."""
    seen: list[str] = []
    solver = CountingSolver(replies={"tiny-1-alpha": "alpha", "tiny-2-beta": "beta"})

    result = steps_module.run_cycle(
        conn,
        suite=tiny_suite(),
        holdout=None,
        solve=solver,
        reflector=FakeProvider(config.load()),
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
        on_start=seen.append,
    )

    assert seen == [result.improvement_run_id]


def test_a_cycle_without_an_unseen_suite_fails_the_decision_closed(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite
) -> None:
    """FR-19: no evidence from outside the reflector's view is not the same as good evidence."""
    result = cycle(conn, tmp_harnessed_home, smoke, None)

    assert result.has_candidate is True
    assert result.holdout is None
    assert result.promoted is False
    assert result.rule == "missing_case"
    assert recorded_steps(conn, result.improvement_run_id) == (
        steps_module.BASELINE,
        steps_module.REFLECT,
        steps_module.CANDIDATE,
        steps_module.PROMOTE,
    )
    assert counted(conn, "active_procedures") == 0
    assert result.cand_id in steps_module.describe(result, provider=FAKE)


def test_a_step_name_outside_the_cycle_is_refused_at_write_time(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = steps_module.start(conn, "smoke")

    with pytest.raises(ImproveError) as raised:
        steps_module.put(conn, run_id, "reflct", "k", {"cand_id": ""})

    assert "reflct" in str(raised.value)
    assert counted(conn, "improvement_steps") == 0


def test_a_reflect_row_naming_a_candidate_that_is_not_stored_is_an_error(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """The candidate row is written before the step that names it, so its absence is a bug."""
    dry = cycle(conn, tmp_harnessed_home, smoke, holdout, dry_run=True)
    db.write(conn, (("DELETE FROM candidates WHERE cand_id = ?", (dry.cand_id,)),))

    with pytest.raises(ImproveError) as raised:
        cycle(conn, tmp_harnessed_home, smoke, holdout)

    assert dry.cand_id in str(raised.value)


def test_a_payload_that_is_not_an_object_is_refused_on_the_way_out(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = steps_module.start(conn, "smoke")
    db.write(
        conn,
        (
            (
                "INSERT INTO improvement_steps "
                "(improvement_run_id, step, key, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, steps_module.BASELINE, "smoke", "[]", "2026-08-22T00:00:00+00:00"),
            ),
        ),
    )

    with pytest.raises(ImproveError):
        steps_module.get(conn, run_id, steps_module.BASELINE, "smoke")


def test_a_measurement_row_without_its_cases_cannot_be_recovered(
    conn: sqlite3.Connection,
) -> None:
    run_id, _ = steps_module.start(conn, "smoke")
    stored = steps_module.put(conn, run_id, steps_module.BASELINE, "smoke", {"cases": "none"})

    with pytest.raises(ImproveError) as raised:
        steps_module.measurement(stored)

    assert steps_module.BASELINE in str(raised.value)


def test_a_decision_recorded_without_its_step_row_is_reported_rather_than_retaken(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """The crash window the promoter's own idempotency exists for.

    A pass that recorded the promotion and died before writing its ``improvement_steps`` row
    leaves exactly this state. The resumed cycle runs the step, finds the decision already
    recorded against the content-addressed candidate id, and changes nothing: no second row, no
    second activation.
    """
    dry = cycle(conn, tmp_harnessed_home, smoke, holdout, dry_run=True)
    stored = candidate_module.get(conn, dry.cand_id)
    assert stored is not None
    procedural.record_promotion(
        conn,
        cand_id=stored.cand_id,
        text=stored.text,
        promoted=True,
        reason="score",
        score_before=0.8,
        score_after=1.0,
        passes_before=4,
        passes_after=5,
        regressions=0,
        soft_drops=0,
    )

    resumed = cycle(conn, tmp_harnessed_home, smoke, holdout)

    assert resumed.improvement_run_id == dry.improvement_run_id
    assert resumed.promoted is True
    assert resumed.rule == "score"
    assert resumed.score_before == 0.8
    assert counted(conn, "promotions") == 1
    assert len(procedural.active(conn)) == 1


def test_a_cycle_after_a_promotion_finds_nothing_left_to_reflect_on(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """The flywheel's own success condition: the procedure is active, so the case passes.

    The second cycle measures its baseline under the promoted procedure, fails nothing the
    corpus keys on, and reports no candidate. A cycle that produced one anyway would be
    reflecting on a failure it did not observe.
    """
    first = cycle(conn, tmp_harnessed_home, smoke, holdout)
    assert first.promoted is True

    second = cycle(conn, tmp_harnessed_home, smoke, holdout, fresh=True)

    assert second.improvement_run_id != first.improvement_run_id
    assert second.before is not None
    assert all(case.passed for case in second.before.cases)
    assert second.cand_id == ""
    assert "no candidate" in steps_module.describe(second, provider=FAKE).casefold()
    assert counted(conn, "promotions") == 1


def test_a_resumed_cycle_says_how_much_of_it_it_actually_ran(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    cycle(conn, tmp_harnessed_home, smoke, holdout)
    resumed = cycle(conn, tmp_harnessed_home, smoke, holdout)

    report = steps_module.describe(resumed, provider=FAKE)
    assert "resumed" in report
    assert f"0 of {len(steps_module.STEPS)} steps" in report


def test_a_recorded_case_that_is_not_an_object_is_refused(conn: sqlite3.Connection) -> None:
    run_id, _ = steps_module.start(conn, "smoke")
    stored = steps_module.put(conn, run_id, steps_module.BASELINE, "smoke", {"cases": ["nope"]})

    with pytest.raises(ImproveError):
        steps_module.measurement(stored)


# Ticket 050. The flywheel refuses to promote on one draw from a sampling source.


def test_one_draw_against_a_sampling_source_is_refused_by_the_cycle(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """The arithmetic can be right and the input a coin; the cycle must know which it has.

    Wired exactly as ``cycle()`` wires a promoting run - same solver, same reflector - so the
    only thing that differs from a promotion is the minimum the caller declared. Against a live
    model the composition root declares MINIMUM_LIVE_SAMPLES; here the test declares 3 and draws
    once, and the refusal names why rather than promoting on a number it cannot trust.
    """
    result = steps_module.run_cycle(
        conn,
        suite=smoke,
        holdout=holdout,
        solve=real_solver(tmp_harnessed_home),
        reflector=FakeProvider(config.load()),
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
        samples=1,
        minimum_samples=3,
    )
    assert result.outcome.promoted is False
    assert result.outcome.rule == "undersampled"
    assert result.outcome.samples_before == 1
    assert result.outcome.samples_after == 1
    # Ticket 052. Without this line the assertions above are vacuous: they passed for two
    # tickets while ``Measurement.result`` dropped ``samples`` on the way to the promoter, so a
    # cycle that drew ten times was refused with exactly this wording. What makes the refusal
    # above a correct one rather than the bug is that the recorded draws are the ones asked for.
    assert result.before is not None and result.before.samples == 1


def test_the_refusal_lifts_when_the_cycle_draws_what_the_minimum_asks_for(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """Ticket 052. The other half of the test above, and the reason it is not vacuous.

    Same wiring, same declared minimum, one thing different: the cycle draws three times. If
    ``samples`` is dropped anywhere between the measurement and the promotion this goes red,
    which is what the refusal above cannot tell on its own. ``tests/test_improve_steps.py`` pins
    the same value at the argument boundary; this pins the consequence.
    """
    result = steps_module.run_cycle(
        conn,
        suite=smoke,
        holdout=holdout,
        solve=real_solver(tmp_harnessed_home),
        reflector=FakeProvider(config.load()),
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
        samples=3,
        minimum_samples=3,
    )
    assert result.before is not None and result.before.samples == 3
    assert result.after is not None and result.after.samples == 3
    assert result.outcome is not None
    assert result.outcome.rule != "undersampled"
    assert result.outcome.samples_before == 3
    assert result.outcome.samples_after == 3


def test_the_deterministic_exemption_is_a_minimum_of_one(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """FakeProvider replays; one draw is the truth, and the caller says so with 1 - the default."""
    result = steps_module.run_cycle(
        conn,
        suite=smoke,
        holdout=holdout,
        solve=real_solver(tmp_harnessed_home),
        reflector=FakeProvider(config.load()),
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
    )
    assert result.outcome.rule == "score"
    assert result.outcome.promoted is True
