"""The resumable eval runner: drive one suite, write ``eval_results``, resume where it stopped.

FR-16. This is the second of the four ledgers, and it is the one the whole improvement thesis
rests on. If two ``fresh`` runs of the same suite against the same corpus could disagree, then
``score_after > score_before`` would be a coin toss with extra steps and the promoter's
arithmetic (ticket 020) would prove nothing. Everything below exists to make that impossible:

* **The ledger pattern, verbatim** (``state-and-idempotency.md``). Before a case is scored, its
  row is read; a row that exists is returned and no work is done. After a case is scored, its row
  is written *before the next case begins*, so an interruption costs at most the case in flight.
* **No hidden state.** The runner holds nothing between cases that is not on disk. ``metadata
  ["scope"]`` is the case id and the iteration is derived from the request, so FakeProvider
  reconstructs the same fixture key on a resumed run without remembering anything either
  (``architecture.md``, THE CRUX).
* **The aggregate is not computed here.** ``evals/scoring.py`` owns the mean, the epsilon and the
  regression rule (All AI agents MUST rule 1). This module projects each scored case onto a
  :class:`~harnessed.evals.scoring.CaseOutcome` and hands the set over. It never means two
  numbers together and never asks whether one score beat another.

**Why the loop arrives as a callable.** ``run_loop`` needs a tool registry, and the import table
puts ``tools`` outside what ``evals/`` may reach (``evals -> loop, memory, providers(types only),
events``). Rather than widen the table, the caller composes the loop, the provider and the
registry into one :class:`Solve` and hands it in - the same injection ticket 010 used for the
sandbox root and ticket 013 for the call serializer. The composition root is ``cli/eval_cmd.py``
(ticket 028); this module stays a function of its arguments, which is also what lets a test drive
it with the real loop and with a counting spy from the same signature.

The ``--json`` projection is a string, not a ``print``. Rendering belongs to ``cli/render.py``;
what lives here is the single JSON object that command writes and nothing else.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Final, Protocol

from harnessed import clock, ids
from harnessed.errors import (
    DecisionDropped,
    EntryMissing,
    EvalError,
    SuiteMismatch,
    SurfaceNotFound,
    TransportDropped,
)
from harnessed.evals import scoring
from harnessed.evals.graders import Answer, Grade, grade_all
from harnessed.evals.scoring import CaseOutcome, Comparison, RunResult, mean_score
from harnessed.evals.suite import KIND_WORK, Case, GraderSpec, Suite
from harnessed.memory import db, redact
from harnessed.providers.types import ChatRequest, Message

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from harnessed.events.bus import EventBus
    from harnessed.loop.engine import LoopResult

__all__ = [
    "CASE_SCORED",
    "CASE_STARTED",
    "DEFAULT_ATTEMPT",
    "DEFAULT_LABEL",
    "RUN_FINISHED",
    "RUN_STARTED",
    "CaseResult",
    "RetryLog",
    "Unscored",
    "EvalComparison",
    "EvalRun",
    "Solve",
    "comparison",
    "comparison_payload",
    "json_payload",
    "render_json",
    "run_suite",
    "summary_payload",
]

RUN_STARTED: Final = "eval.run.started"
CASE_STARTED: Final = "eval.case.started"
CASE_SCORED: Final = "eval.case.scored"
RUN_FINISHED: Final = "eval.run.finished"

#: The id prefix ``architecture.md`` reserves for an eval run.
EVAL_ID_PREFIX: Final = "eval"

#: One scoring pass over a case. A second attempt is a second key, never an overwrite: the
#: ledger records what happened, and a re-score that erased its predecessor would make the
#: history of a case unreadable.
DEFAULT_ATTEMPT: Final = 1

#: What a run is called when the caller does not say. The promoter runs one suite twice under
#: two labels, which is what keeps ``before`` and ``after`` two rows rather than one.
DEFAULT_LABEL: Final = "baseline"

_INSERT_RUN: Final = (
    "INSERT INTO eval_runs (eval_run_id, suite, label, started_at, provider, model) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_INSERT_RETRY: Final = (
    "INSERT INTO eval_retries "
    "(eval_run_id, case_id, call, attempt, error_class, backoff_s, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_COUNT_RETRIES: Final = "SELECT COUNT(*) FROM eval_retries WHERE eval_run_id = ?"
_FINISH_RUN: Final = "UPDATE eval_runs SET finished_at = ? WHERE eval_run_id = ?"
_SELECT_LATEST_RUN: Final = (
    "SELECT eval_run_id FROM eval_runs WHERE suite = ? AND label = ? "
    "ORDER BY started_at DESC, rowid DESC LIMIT 1"
)
_INSERT_RESULT: Final = (
    "INSERT INTO eval_results (eval_run_id, case_id, attempt, score, passed, detail, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
#: Recent runs, newest first. ``harnessed eval list`` exists so ``eval show`` has a way to learn
#: its own argument: before ticket 045 the group had one command, and a run scored an hour ago
#: was reachable only by opening SQLite by hand.
#:
#: Run identity only. This query used to carry four correlated subqueries - two ``COUNT(*)``s, an
#: ``AVG`` and a ``MAX(attempt)`` - and the counts were over ledger *rows*. That was right until
#: ticket 050 gave a case one row per draw, at which point ``list`` reported a 5-case suite drawn
#: 3 times as 15 cases while ``show`` reported 5. The counts now come from ``_folded_cases``,
#: which is the same fold ``run`` and ``show`` go through, so the surfaces cannot drift apart
#: again (ticket 054).
_SELECT_RUNS: Final = (
    "SELECT eval_run_id, suite, label, started_at, provider, model "
    "FROM eval_runs ORDER BY started_at DESC, rowid DESC LIMIT ?"
)

_SELECT_LATEST_OF: Final = (
    "SELECT eval_run_id, suite, label, started_at, provider, model "
    "FROM eval_runs WHERE suite = ? ORDER BY started_at DESC, rowid DESC LIMIT 1"
)

_SELECT_RUN: Final = (
    "SELECT eval_run_id, suite, label, provider, model FROM eval_runs WHERE eval_run_id = ?"
)

_SELECT_ALL_RESULTS: Final = (
    "SELECT case_id, score, passed, detail, attempt FROM eval_results "
    "WHERE eval_run_id = ? ORDER BY rowid"
)

_SELECT_RESULT: Final = (
    "SELECT score, passed, detail FROM eval_results "
    "WHERE eval_run_id = ? AND case_id = ? AND attempt = ?"
)

#: How many draws this run already holds for one case (ticket 058). A resumed run continues those
#: draws rather than re-measuring with whatever ``samples`` this call happened to pass, which is
#: what kept one ``eval_run_id`` answering 1 to the runner and 3 to ``eval show``.
_COUNT_CASE_ATTEMPTS: Final = (
    "SELECT COUNT(*) FROM eval_results WHERE eval_run_id = ? AND case_id = ?"
)


class Solve(Protocol):
    """The loop, already composed with its provider, its registry and its ceiling.

    One request in, one :class:`~harnessed.loop.engine.LoopResult` out - which is exactly
    ``run_loop`` with everything but the request bound. Injected rather than imported so this
    module never reaches ``tools/``; see the module docstring.

    ``run_key`` names the run journal's key for this call (FR-7, ticket 045). It is keyword-only
    and optional so an existing caller is unaffected, and a case passes one built from its own
    ``(eval_run_id, case_id)``: one session scores every case of a suite, so a journal keyed on
    anything the session owns would have every case replaying the first one's turns.
    """

    def __call__(self, request: ChatRequest, *, run_key: str | None = None) -> LoopResult: ...


@dataclass(frozen=True, slots=True)
class Served:
    """What a work case's pinned handler produced: the reply as served, or a failed hop."""

    text: str = ""
    failed: bool = False


class Serve(Protocol):
    """A work case through its recorded handler (ticket 236, decision 83), already composed.

    Injected like :class:`Solve`, because the routed composition lives in ``cli/`` and ``evals/``
    imports no router module (MUST rule 30). ``request`` is the one every suite builds for the
    case, so a fixture is keyed as every suite's is. Raises
    :class:`~harnessed.errors.EntryMissing` when the case's agent entry is not installed.
    """

    def __call__(self, case: Case, request: ChatRequest, *, run_key: str) -> Served: ...


@dataclass(frozen=True, slots=True)
class Unscored:
    """A case with no ``eval_results`` row: its call dropped on every attempt it was given."""

    case_id: str
    reason: str
    attempts: int
    error_class: str


class RetryLog:
    """Where a live run's retries are written (ticket 231, migration 017).

    The retrying provider and decider know a call and an attempt; only the runner knows which
    case is in flight and which run it belongs to. ``run_suite`` binds the log to the run and
    names each case as it starts, and :meth:`on_retry` is what the wrappers are handed.
    """

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._eval_run_id = ""
        self._case_id = ""

    def bind(self, conn: sqlite3.Connection, eval_run_id: str) -> None:
        self._conn = conn
        self._eval_run_id = eval_run_id

    def case(self, case_id: str) -> None:
        self._case_id = case_id

    def on_retry(self, call: str, attempt: int, error_class: str, backoff_s: float) -> None:
        if self._conn is None:
            return
        db.write(
            self._conn,
            (
                (
                    _INSERT_RETRY,
                    (
                        self._eval_run_id,
                        self._case_id,
                        call,
                        attempt,
                        error_class,
                        backoff_s,
                        clock.isoformat(clock.now()),
                    ),
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case's outcome in one run: what it scored, whether it passed, and where it came from.

    ``replayed`` is how a caller tells a case that was scored in this pass from one that was read
    back off the ledger. The score is identical either way - that is the point of the ledger -
    but the distinction is what makes "a resumed run re-scored nothing" observable.
    """

    case_id: str
    score: float
    passed: bool
    detail: str = ""
    attempt: int = DEFAULT_ATTEMPT
    replayed: bool = False

    @property
    def outcome(self) -> CaseOutcome:
        """The projection ``evals/scoring.py`` compares. The pass is carried, never re-derived."""
        return CaseOutcome(case_id=self.case_id, score=self.score, passed=self.passed)


@dataclass(frozen=True, slots=True)
class EvalRun:
    """One execution of one suite, and the ledger rows it now owns."""

    eval_run_id: str
    suite: str
    label: str
    cases: tuple[CaseResult, ...]
    fresh: bool = False
    resumed: bool = False
    #: How many times each case was drawn to produce these numbers (ticket 050).
    samples: int = 1
    #: Which provider and model the run row was opened under (FR-72, migration 010), read back
    #: off the row. ``None`` for a run opened before 010: unknown, never a default name.
    provider: str | None = None
    model: str | None = None
    #: Cases a dropped connection left without a row, after their retries (ticket 231, FR-160).
    #: They are absent from ``cases`` and so from the mean, which is over scored cases only.
    unscored: tuple[Unscored, ...] = ()
    #: Work cases still awaiting the operator's verdict: skipped, never scored (ticket 236).
    pending: tuple[str, ...] = ()
    #: How many retries this run's ledger holds in ``eval_retries``.
    retried_calls: int = 0

    @property
    def result(self) -> RunResult:
        """The run in the shape the scoring module reads. Duplicates are refused there."""
        return RunResult.from_cases((case.outcome for case in self.cases), samples=self.samples)

    @property
    def score(self) -> float:
        """The aggregate, from ``evals/scoring.py``. Nothing here averages anything."""
        return self.result.score

    @property
    def passes(self) -> int:
        """How many cases passed, from ``evals/scoring.py``.

        Named ``passes`` rather than ``pass_count`` on purpose: ``tests/test_scoring.py`` scans
        the tree for any module redefining one of the six functions ticket 018 owns, and a
        property is a ``FunctionDef`` like any other. The number still comes from there.
        """
        return self.result.pass_count

    @property
    def case_count(self) -> int:
        """How many cases the run covers."""
        return len(self.cases)

    @property
    def scored_now(self) -> int:
        """How many cases this pass actually paid for. Zero on a re-issued completed run."""
        return sum(1 for case in self.cases if not case.replayed)


def run_suite(
    suite: Suite,
    solve: Solve,
    conn: sqlite3.Connection,
    *,
    model: str,
    provider: str,
    system: str = "",
    label: str = DEFAULT_LABEL,
    fresh: bool = False,
    attempt: int = DEFAULT_ATTEMPT,
    samples: int = 1,
    bus: EventBus | None = None,
    on_start: Callable[[str], None] | None = None,
    resume: str | None = None,
    retries: RetryLog | None = None,
    serve: Serve | None = None,
) -> EvalRun:
    """Score every case of ``suite``, resuming from the ledger unless ``fresh`` is asked for.

    Without ``fresh`` the most recent run of this ``(suite, label)`` is continued, whether it was
    interrupted or finished - so re-issuing the same command is idempotent and costs nothing.
    ``fresh`` is the documented way to start a second run, and it leaves the previous one exactly
    as it stood.

    ``provider`` is the configured provider's name, and it has no default (FR-72): a new run row
    records it beside ``model``, and a resumed run keeps the pair its row was opened under.

    ``on_start`` is handed the ``eval_run_id`` the moment it is settled, before the first case
    runs. It exists because the id is chosen inside and a caller that is interrupted mid-suite
    still needs to know which run to resume.

    ``resume`` re-opens that eval run (it must exist and be of this suite) and re-scores only the
    cases without a row. A case whose call dropped on every attempt it was given (ticket 231,
    FR-160) is left with no row, named in ``unscored``, and the run is not stamped finished; the
    other cases are scored and the mean is over them alone. ``retries`` is bound to the run so the
    wrapped provider's retries land in ``eval_retries`` under the case that made them.

    A work suite (ticket 236) is run through ``serve`` instead of ``solve``: a ``pending`` case is
    skipped and named in ``pending``, and a case whose entry is not installed is left unscored
    with the reason ``entry_missing``. The score is the same ``mean_score`` over what was scored.
    """
    if suite.kind == KIND_WORK and serve is None:
        raise EvalError(f"suite {suite.name!r} is a work suite and needs a pinned composition")
    if attempt < 1:
        raise EvalError(
            f"attempt must be 1 or greater, got {attempt}. An attempt numbers a scoring pass "
            f"over a case and is part of the eval_results key."
        )
    if samples < 1:
        raise EvalError(
            f"samples must be 1 or greater, got {samples}. A run is at least one draw; zero draws "
            f"is not a run that scored nothing, it is no run at all."
        )

    if resume is not None:
        eval_run_id, resumed = _reopen(conn, resume, suite.name), True
    else:
        eval_run_id, resumed = _settle_run(
            conn, suite.name, label, fresh=fresh, provider=provider, model=model
        )
    if retries is not None:
        retries.bind(conn, eval_run_id)
    recorded_provider, recorded_model = _source(conn, eval_run_id)
    if on_start is not None:
        on_start(eval_run_id)
    _publish(
        bus,
        RUN_STARTED,
        {
            "eval_run_id": eval_run_id,
            "suite": suite.name,
            "label": label,
            "case_count": len(suite.cases),
            "attempt": attempt,
            "fresh": fresh,
            "resumed": resumed,
        },
    )

    results: list[CaseResult] = []
    unscored: list[Unscored] = []
    pending: list[str] = []
    drawn = samples
    for case in suite.cases:
        if suite.kind == KIND_WORK and case.pending:
            pending.append(case.id)
            continue
        if retries is not None:
            retries.case(case.id)
        _publish(
            bus,
            CASE_STARTED,
            {"eval_run_id": eval_run_id, "case_id": case.id, "attempt": attempt},
        )
        # ``samples`` draws per case, each under its own attempt number so every draw is its
        # own ledger row and a resumed run replays exactly the draws it already made (ticket
        # 050). ``attempt`` is the first; the rest follow it. One draw is the pre-050 path,
        # unchanged - and against a replaying source it is exactly the truth.
        #
        # Ticket 058: a resumed run CONTINUES the previous draws rather than re-measuring with
        # this call's argument. Score a suite at ``--samples 3``, run it again with no flag, and
        # the old code drew once and labelled the run ``samples: 1`` while the ledger still held
        # three rows - so one ``eval_run_id`` answered 1 to the runner and 3 to ``eval show``.
        # Folding what the ledger holds keeps the two surfaces from disagreeing by construction.
        # Every replayed draw costs nothing; ``_score_case`` reads the row rather than the model.
        # ``--fresh`` remains the way to start a new measurement, and it starts a new id.
        held = _attempts_recorded(conn, eval_run_id, case.id)
        per_case = max(samples, held)
        drawn = max(drawn, per_case)
        try:
            draws = [
                _score_case(
                    case,
                    solve,
                    conn,
                    eval_run_id,
                    attempt=attempt + i,
                    model=model,
                    system=system,
                    serve=serve if suite.kind == KIND_WORK else None,
                )
                for i in range(per_case)
            ]
        except EntryMissing as missing:
            unscored.append(Unscored(case.id, "entry_missing", 0, type(missing).__name__))
            continue
        except (TransportDropped, DecisionDropped) as dropped:
            unscored.append(Unscored(case.id, "transport", dropped.attempts, dropped.error_class))
            continue
        result = _fold(draws)
        results.append(result)
        _publish(
            bus,
            CASE_SCORED,
            {
                "eval_run_id": eval_run_id,
                "case_id": result.case_id,
                "attempt": result.attempt,
                "score": result.score,
                "passed": result.passed,
                "replayed": result.replayed,
            },
        )

    # Ticket 058. The result describes the LEDGER, not this call's arguments. `samples` used to be
    # the caller's argument here while `_folded_cases` derived it from the rows, so a resumed run -
    # the default - answered 1 to the runner and 3 to `eval show` for one `eval_run_id`. The two
    # surfaces now cannot disagree, because there is one fold and `run_suite` reads it too.
    #
    # Resume semantics, decided here and stated so it is not implicit in whichever function a
    # caller reached for: a resumed run CONTINUES the previous draws. Its score and its sample
    # count are those of every row the ledger holds for that id, not of the draws this call made.
    # `--fresh` remains the documented way to start a new measurement, and it starts a new id.
    #
    # `results` above is still what this call drew, and it is what the per-case events reported;
    # that is deliberate, because an event describes an action while the run describes a state.
    run = EvalRun(
        eval_run_id=eval_run_id,
        suite=suite.name,
        label=label,
        cases=tuple(results),
        fresh=fresh,
        resumed=resumed,
        samples=drawn,
        provider=recorded_provider,
        model=recorded_model,
        unscored=tuple(unscored),
        pending=tuple(pending),
        retried_calls=_retried(conn, eval_run_id),
    )
    if not any(item.reason == "transport" for item in unscored):
        _finish(conn, eval_run_id)
    _publish(
        bus,
        RUN_FINISHED,
        {
            "eval_run_id": eval_run_id,
            "suite": run.suite,
            "label": run.label,
            "score": run.score,
            "pass_count": run.passes,
            "case_count": run.case_count,
            "scored": run.scored_now,
        },
    )
    return run


# ---- one case, the ledger pattern verbatim -------------------------------------------------------


def _fold(draws: Sequence[CaseResult]) -> CaseResult:
    """N draws of one case into one result: the mean score, passed only if every draw passed.

    The mean is what the promoter compares, so it is what the case reports. ``passed`` is the
    conjunction rather than a majority, because a case that passes two draws in three has shown
    it can fail, and a pass that means "usually" is not what any grader here promises. The
    detail is the first draw's, with the spread appended, so ``eval show`` can display what
    happened without a second query.
    """
    first: CaseResult = draws[0]
    if len(draws) == 1:
        return first
    scores = tuple(d.score for d in draws)
    return CaseResult(
        case_id=first.case_id,
        score=mean_score(scores),
        passed=all(d.passed for d in draws),
        detail=f"{first.detail} | draws: {list(scores)}",
        attempt=first.attempt,
        replayed=all(d.replayed for d in draws),
    )


def case_run_key(eval_run_id: str, case_id: str, attempt: int) -> str:
    """The run journal's key for one scored case.

    Built from the three things that identify a case's work: which run, which case, which attempt.
    A key missing any of them makes two different pieces of work share a ledger, and a journal
    whose key is too coarse replays somebody else's turns rather than resuming its own.
    """
    return f"{eval_run_id}:{case_id}:{attempt}"


def _attempts_recorded(conn: sqlite3.Connection, eval_run_id: str, case_id: str) -> int:
    """How many draws this run already holds for one case. Zero for a case never scored.

    Ticket 058. This is what makes a resumed run continue its previous draws instead of
    re-measuring with whatever ``samples`` the caller passed this time - the divergence that let
    one ``eval_run_id`` report 1 to the runner and 3 to ``eval show``.
    """
    row = conn.execute(_COUNT_CASE_ATTEMPTS, (eval_run_id, case_id)).fetchone()
    return 0 if row is None else int(row[0])


def _score_case(
    case: Case,
    solve: Solve,
    conn: sqlite3.Connection,
    eval_run_id: str,
    *,
    attempt: int,
    model: str,
    system: str,
    serve: Serve | None = None,
) -> CaseResult:
    """Read the done-state row and return it, or do the work and persist before the next case."""
    recorded = _recorded(conn, eval_run_id, case.id, attempt)
    if recorded is not None:
        return recorded

    request = _request(case, model=model, system=system)
    run_key = case_run_key(eval_run_id, case.id, attempt)
    if serve is not None:
        served = serve(case, request, run_key=run_key)
        answer = Answer(text=served.text, failed=served.failed)
        specs: tuple[GraderSpec, ...] = (
            GraderSpec("verdict", {"verdict": case.verdict, "traits": list(case.traits)}),
        )
    else:
        answer, specs = _answer(solve(request, run_key=run_key)), case.graders
    grades = grade_all(specs, answer)
    scored = CaseResult(
        case_id=case.id,
        score=mean_score(grade.score for grade in grades),
        passed=all(grade.passed for grade in grades),
        detail=_detail(grades),
        attempt=attempt,
    )
    _persist(conn, eval_run_id, scored)
    return scored


def _request(case: Case, *, model: str, system: str) -> ChatRequest:
    """The one request a case starts from.

    ``metadata["scope"]`` is the case id and nothing else, which is what lets FakeProvider find
    ``corpus/wire/fake/<case id>/`` with no special case anywhere (THE CRUX). ``system``
    is handed over intact because the variant predicate reads it: a procedure that genuinely
    carries its traits changes which fixture answers, and one that does not, does not.
    """
    return ChatRequest(
        model=model,
        system=system,
        messages=(Message(role="user", content=case.prompt),),
        metadata={"scope": case.id},
    )


def _answer(result: LoopResult) -> Answer:
    """A finished run reduced to what a grader may see: the reply and the work, in call order."""
    return Answer(text=result.reply, tool_names=tuple(call.name for call in result.tool_calls))


def _detail(grades: tuple[Grade, ...]) -> str:
    """Every grader's verdict as one canonical JSON string, written for the reader.

    The per-grader breakdown is what makes a failed case diagnosable without re-running it, and
    ticket 021's reflector reads exactly this column to say what went wrong.
    """
    return _dumps(
        [
            {
                "grader": grade.grader,
                "score": grade.score,
                "threshold": grade.threshold,
                "passed": grade.passed,
                "detail": grade.detail,
            }
            for grade in grades
        ]
    )


def _recorded(
    conn: sqlite3.Connection, eval_run_id: str, case_id: str, attempt: int
) -> CaseResult | None:
    """The row already on disk for this key, rebuilt, or ``None`` when there is work to do."""
    row = conn.execute(_SELECT_RESULT, (eval_run_id, case_id, attempt)).fetchone()
    if row is None:
        return None
    return CaseResult(
        case_id=case_id,
        score=float(row[0]),
        passed=bool(row[1]),
        detail=str(row[2]),
        attempt=attempt,
        replayed=True,
    )


def _persist(conn: sqlite3.Connection, eval_run_id: str, result: CaseResult) -> None:
    """Write one scored case, scrubbed, before the next case begins (FR-13, FR-16).

    The key columns go over unscrubbed: an id that was rewritten on the way in could not be
    found by the id the caller holds, and the ledger would stop being resumable. Everything a
    model produced - which is all of ``detail`` - passes ``redact.scrub()``.
    """
    db.write(
        conn,
        (
            (
                _INSERT_RESULT,
                (
                    eval_run_id,
                    result.case_id,
                    result.attempt,
                    result.score,
                    int(result.passed),
                    redact.scrub(result.detail),
                    clock.isoformat(clock.now()),
                ),
            ),
        ),
    )


# ---- the run row ------------------------------------------------------------------------------


def _settle_run(
    conn: sqlite3.Connection, suite: str, label: str, *, fresh: bool, provider: str, model: str
) -> tuple[str, bool]:
    """The id this pass writes under, and whether it was inherited rather than made."""
    if not fresh:
        row = conn.execute(_SELECT_LATEST_RUN, (suite, label)).fetchone()
        if row is not None:
            return str(row[0]), True
    return _start(conn, suite, label, provider=provider, model=model), False


def _reopen(conn: sqlite3.Connection, eval_run_id: str, suite: str) -> str:
    """The run ``--resume`` names, or an ``EvalError`` saying why it cannot be re-opened."""
    row = conn.execute(_SELECT_RUN, (eval_run_id,)).fetchone()
    if row is None:
        raise EvalError(
            f"no eval run with id {eval_run_id!r} to resume. `harnessed eval list` prints the "
            f"ids it knows."
        )
    if str(row[1]) != suite:
        raise SuiteMismatch(
            f"eval run {eval_run_id!r} scored suite {str(row[1])!r}, not {suite!r}; "
            f"--resume re-opens a run of the suite it names."
        )
    return str(row[0])


def _retried(conn: sqlite3.Connection, eval_run_id: str) -> int:
    """How many retries the ledger holds for one run."""
    return int(conn.execute(_COUNT_RETRIES, (eval_run_id,)).fetchone()[0])


def _start(conn: sqlite3.Connection, suite: str, label: str, *, provider: str, model: str) -> str:
    """Open a new eval run. The id is generated once, in ``ids.py``, and never rebuilt.

    The row records which provider and model it is opened under (FR-72). Written once, here,
    and never updated: a resumed run is the same run, under the pair it started with.
    """
    eval_run_id = ids.new_id(EVAL_ID_PREFIX)
    at = clock.isoformat(clock.now())
    db.write(conn, ((_INSERT_RUN, (eval_run_id, suite, label, at, provider, model)),))
    return eval_run_id


def _source(conn: sqlite3.Connection, eval_run_id: str) -> tuple[str | None, str | None]:
    """The provider and model a run row holds; ``None`` for each on a row opened before 010."""
    row = conn.execute(_SELECT_RUN, (eval_run_id,)).fetchone()
    return (None, None) if row is None else (_text(row[3]), _text(row[4]))


def _text(value: object) -> str | None:
    """A nullable ``TEXT`` column as the string it holds, or ``None`` where it holds nothing."""
    return None if value is None else str(value)


def _finish(conn: sqlite3.Connection, eval_run_id: str) -> None:
    """Stamp the run finished. Re-issuing a finished run simply stamps it again."""
    db.write(conn, ((_FINISH_RUN, (clock.isoformat(clock.now()), eval_run_id)),))


# ---- the machine-readable projection -----------------------------------------------------------


def json_payload(run: EvalRun) -> dict[str, object]:
    """One run as plain data: what ``harnessed eval run --json`` hands to a parser.

    Deliberately flat and deliberately complete. The shell-parity test in ticket 025 compares
    this object across a process boundary, so every number a human reads must be in it.

    A run that recorded no case serves ``None`` for ``score``: on a screen a ``0.0`` there is a
    measurement that never happened (FR-47, ticket 096).
    """
    payload: dict[str, object] = {
        "eval_run_id": run.eval_run_id,
        "suite": run.suite,
        "label": run.label,
        "score": _served_score(run.score, run.case_count),
        "pass_count": run.passes,
        "case_count": run.case_count,
        "scored": run.scored_now,
        "fresh": run.fresh,
        "resumed": run.resumed,
        "samples": run.samples,
        "provider": run.provider,
        "model": run.model,
        "retried_calls": run.retried_calls,
        "unscored": [
            {
                "case_id": item.case_id,
                "reason": item.reason,
                "attempts": item.attempts,
                "error_class": item.error_class,
            }
            for item in run.unscored
        ],
        "score_over": "scored cases only",
        "cases": [
            {
                "case_id": case.case_id,
                "score": case.score,
                "passed": case.passed,
                "attempt": case.attempt,
                "replayed": case.replayed,
            }
            for case in run.cases
        ],
    }
    if run.pending:  # only a work suite has any; every other suite's bytes stay as they were
        payload["pending"] = list(run.pending)
    return payload


def render_json(run: EvalRun) -> str:
    """The single JSON object ``--json`` emits, and nothing else - no banner, no trailing prose.

    A string rather than a write, because output belongs to ``cli/render.py``. The caller writes
    exactly this and adds at most a newline.
    """
    return _dumps(json_payload(run))


def _dumps(payload: object) -> str:
    """Canonical JSON, the spelling ``architecture.md`` fixes under "Format"."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _publish(bus: EventBus | None, name: str, payload: Mapping[str, object]) -> None:
    """Announce one moment of the run, when there is a bus listening."""
    if bus is not None:
        bus.publish(name, dict(payload))


# ---- reading a scored run back (ticket 045) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One line of ``harnessed eval list``: what a run was, and how it went."""

    eval_run_id: str
    suite: str
    label: str
    started_at: str
    case_count: int
    passes: int
    score: float
    #: Read back as the highest attempt number the run recorded: a run drawn N times leaves
    #: attempts 1..N, so the ledger already knows without a column of its own (ticket 050).
    samples: int = 1
    #: The row's provider and model (FR-72); ``None`` on a run opened before migration 010.
    provider: str | None = None
    model: str | None = None

    # ``score`` is a field, not a property, and that is ticket 049's whole change. It used to be
    # computed here as ``passes / case_count`` - a pass ratio, which is not the score. The score
    # is the mean of case scores, a case can score between 0 and 1, and the promoter counts the
    # difference. For one run, ``--json`` said 0.3 and ``list`` said 0.2; a human noticed and no
    # test did. Every one of these numbers now comes from ``RunResult`` over the folded cases -
    # the same object ``eval run`` and ``eval show`` report - so the two cannot disagree.


def summary_payload(row: RunSummary) -> dict[str, object]:
    """One summary row as plain data: what ``eval list --json`` prints and ``GET /v1/eval/runs``
    carries, from the same call (ticket 075).

    The store projection, key for key - ``asdict`` over the dataclass, no renaming. This is the
    **only** place a :class:`RunSummary` becomes a payload; a call site that spells the keys out
    by hand is how the CLI came to say ``pass_count`` while the wire said ``passes`` (decision
    19, the same defect 062 fixed for separators). A field added to the dataclass reaches both
    surfaces through here or reaches neither.

    One value is not the stored one: a row with no cases serves ``None`` for ``score``, the same
    rule :func:`json_payload` applies (FR-47, ticket 096). The key set is still the dataclass's.
    """
    payload: dict[str, object] = asdict(row)
    payload["score"] = _served_score(row.score, row.case_count)
    return payload


def _served_score(score: float, case_count: int) -> float | None:
    """The score as a reader is served it: ``None`` when the run measured nothing.

    The emitters' rule and only the emitters' (decision 35). ``RunSummary.score`` and
    ``EvalRun.score`` stay ``float`` and keep the fail-closed ``0.0`` that
    ``evals/scoring.py::mean_score`` gives an empty run, because the promoter reads those and a
    ``None`` there would turn "no evidence" into a missing comparison rather than a refused one.
    What changes is what a screen is told: a run with no cases has no score to show.
    """
    return None if case_count == 0 else score


def recent(conn: sqlite3.Connection, limit: int = 20) -> tuple[RunSummary, ...]:
    """The most recent scored runs, newest first.

    Each line's numbers are folded, not counted: the run's rows are read and handed to
    ``_folded_cases``, the same fold ``run_suite`` applies to live draws and ``scored_run``
    applies to replayed ones. A run with no rows folds to no cases, which is the fail-closed
    reading ``mean_score`` already gives - zero evidence scores 0.0. That 0.0 is the promoter's
    and stays on the dataclass; :func:`summary_payload` serves a reader ``None`` (ticket 096).

    That costs one query per listed run and reads every row of each, where the old form was a
    single query with four subqueries. It is a bounded cost - ``limit`` runs of a suite that is
    five cases by convention - and the alternative is a second definition of "did this case
    pass", written in SQL, that the next ticket to change the fold would leave behind (ticket
    054).
    """
    return tuple(_summary(conn, row) for row in conn.execute(_SELECT_RUNS, (limit,)).fetchall())


def latest_of(conn: sqlite3.Connection, suite: str) -> RunSummary | None:
    """The newest scored run of one suite, or ``None`` when it never ran (ticket 238)."""
    row = conn.execute(_SELECT_LATEST_OF, (suite,)).fetchone()
    return None if row is None else _summary(conn, row)


def _summary(conn: sqlite3.Connection, row: Sequence[object]) -> RunSummary:
    """One ``eval_runs`` row folded into the summary every listing serves."""
    cases, samples = _folded_cases(conn, str(row[0]))
    result = RunResult.from_cases((case.outcome for case in cases), samples=samples)
    return RunSummary(
        eval_run_id=str(row[0]),
        suite=str(row[1]),
        label=str(row[2]),
        started_at=str(row[3]),
        case_count=len(cases),
        passes=result.pass_count,
        score=result.score,
        samples=samples,
        provider=_text(row[4]),
        model=_text(row[5]),
    )


def _folded_cases(conn: sqlite3.Connection, eval_run_id: str) -> tuple[tuple[CaseResult, ...], int]:
    """Every draw of one run, grouped by case and folded, plus how many draws it took.

    One row per draw, so a run sampled N times has N rows per case. Folded exactly the way
    ``run_suite`` folds live draws - otherwise a twice-drawn run hands ``RunResult`` two cases
    with one id, which it refuses, and ``show --json`` prints nothing.

    The only place the ledger's rows become a run's cases. Both read paths come through here, so
    "did this case pass" has one definition and ``list`` and ``show`` cannot describe the same
    run at two different sizes (ticket 054).
    """
    by_case: dict[str, list[CaseResult]] = {}
    for r in conn.execute(_SELECT_ALL_RESULTS, (eval_run_id,)):
        by_case.setdefault(str(r[0]), []).append(
            CaseResult(
                case_id=str(r[0]),
                score=float(r[1]),
                passed=bool(r[2]),
                detail=str(r[3]),
                attempt=int(r[4]),
                replayed=True,
            )
        )
    # Counted before folding: ``_fold`` reports the first draw's attempt number, so a folded
    # case cannot say how many draws it was. The raw rows can.
    samples = max((c.attempt for draws in by_case.values() for c in draws), default=1)
    cases = tuple(_fold(sorted(draws, key=lambda c: c.attempt)) for draws in by_case.values())
    return cases, samples


def scored_run(conn: sqlite3.Connection, eval_run_id: str) -> EvalRun | None:
    """One finished run, rebuilt from the ledger. ``None`` when no such run was ever scored.

    Rebuilt rather than re-executed: this reports what the run *did* score, which is the only
    honest thing to show for a run that happened against a store and a model that have since
    moved on.
    """
    row = conn.execute(_SELECT_RUN, (eval_run_id,)).fetchone()
    if row is None:
        return None
    cases, samples = _folded_cases(conn, eval_run_id)
    return EvalRun(
        eval_run_id=str(row[0]),
        suite=str(row[1]),
        label=str(row[2]),
        cases=cases,
        fresh=False,
        resumed=True,
        # The ledger already knows: a run drawn N times left attempts 1..N. Read back through
        # the same helper ``recent`` reads, so list and show cannot disagree about one run.
        samples=samples,
        provider=_text(row[3]),
        model=_text(row[4]),
        retried_calls=_retried(conn, eval_run_id),
    )


# ---- comparing two scored runs (FR-62, decision 37) ---------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalComparison:
    """Two scored runs of one suite, and how the candidate differs from the baseline.

    The difference is ``evals/scoring.py``'s own
    :class:`~harnessed.evals.scoring.Comparison`, carried whole rather than unpacked: the four
    lists are that module's, in its order, and nothing here re-sorts or re-derives them. There is
    no verdict on the object and never will be one - whether the candidate is *better* is
    :func:`~harnessed.evals.scoring.promotes`' question and the promoter's alone (MUST rule 1).
    """

    before: EvalRun
    after: EvalRun
    comparison: Comparison


def comparison(conn: sqlite3.Connection, before: str, after: str) -> EvalComparison:
    """Two eval run ids of one suite, read back and handed to ``scoring.compare``.

    The one store read FR-62 serves. It is a composition and nothing else: read each side, refuse
    a side no ledger holds and a pair that is not one suite, then apply the comparison through the
    ``scoring`` module attribute, so a test can substitute that function and watch the wire follow
    it. ``GET /v1/eval/compare`` and ``harnessed eval compare`` both call this and then
    :func:`comparison_payload`, so there is one composition rather than two (MUST rule 16).

    This body holds no operator of any kind - no equality band, no ordering, no arithmetic -
    because every one of those belongs to ``evals/scoring.py``. The three conditions the read does
    test live in :func:`_side` and :func:`_one_suite` beside it, and an AST scan in
    ``tests/test_eval_compare.py`` holds this function and the handler to that.
    """
    earlier = _side(conn, before, "before")
    later = _side(conn, after, "after")
    _one_suite(earlier, later)
    return EvalComparison(
        before=earlier,
        after=later,
        comparison=scoring.compare(earlier.result, later.result),
    )


def _side(conn: sqlite3.Connection, eval_run_id: str, side: str) -> EvalRun:
    """One side of a comparison, or the refusal that names *which* side asked and for what.

    Naming the side is ticket 079's lesson: a bare "not found" leaves an operator comparing two
    ids by eye to work out which of them the ledgers do not hold.
    """
    found = scored_run(conn, eval_run_id)
    if found is None:
        raise SurfaceNotFound(f"no eval run id {eval_run_id!r} on the {side} side.")
    return found


def _one_suite(before: EvalRun, after: EvalRun) -> None:
    """Refuse two runs that are not the same suite, naming both ids and both suites."""
    if before.suite != after.suite:
        raise SuiteMismatch(
            f"eval run {before.eval_run_id!r} is suite {before.suite!r} and eval run "
            f"{after.eval_run_id!r} is suite {after.suite!r}: a comparison needs one suite."
        )


def comparison_payload(compared: EvalComparison) -> dict[str, object]:
    """One comparison as plain data: what ``eval compare --json`` prints and
    ``GET /v1/eval/compare`` carries, from the same call (decision 37).

    Each side is its run-level result and nothing case by case; the four lists are
    ``scoring.compare``'s own, in its order, each ``ScoreDelta`` projected key for key by
    ``asdict``. A side that recorded no case serves ``None`` for ``score``, the emitters' rule
    (decision 35, ticket 096), so a screen is never told a measurement happened that did not.

    There is no ``promote``, no ``rule``, no verdict and no difference figure. This payload
    reports what moved; whether that is an improvement is the promoter's question, and a
    difference figure - if a gate ever wants one - is a field on ``ScoreDelta`` computed in
    ``scoring.py``, never here and never in a browser (decision 37's fallback).
    """
    return {
        "before": _side_payload(compared.before),
        "after": _side_payload(compared.after),
        "regressions": [asdict(delta) for delta in compared.comparison.regressions],
        "soft_drops": [asdict(delta) for delta in compared.comparison.soft_drops],
        "new_cases": list(compared.comparison.new_cases),
        "missing_cases": list(compared.comparison.missing_cases),
    }


def _side_payload(run: EvalRun) -> dict[str, object]:
    """One side of a comparison: the run's identity and its run-level result."""
    return {
        "case_count": run.case_count,
        "eval_run_id": run.eval_run_id,
        "label": run.label,
        "model": run.model,
        "pass_count": run.passes,
        "provider": run.provider,
        "samples": run.samples,
        "score": _served_score(run.score, run.case_count),
        "suite": run.suite,
    }
