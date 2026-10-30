"""The improvement_steps ledger, and the cycle it makes resumable (FR-18).

The flywheel cycle is the longest-running operation in the milestone: two eval suites, one
reflection and one promotion, each of which costs real work. So it is the one that most needs to
survive an interruption, and the mechanism is the one every other ledger in this repository
already uses (``state-and-idempotency.md``):

    read the done-state row for this step
      present  -> return its recorded payload, do no work
      absent   -> do the work, then persist the row BEFORE moving to the next step

``improvement_steps`` is keyed ``(improvement_run_id, step, key)`` and stores the step's payload,
so a resumed cycle rebuilds every number it reports from rows rather than from re-execution. The
payload is deliberately complete - a measurement carries each case's ``detail`` as well as its
score, because the reflection prompt is built from that detail and a resumed reflection that
asked a different question would not be the same reflection.

Three things this module deliberately does not do:

* **It does not decide anything.** The promoter's arithmetic and the reflector's one question are
  frozen; this module sequences them and records what they returned.
* **It does not own a candidate's identity.** The id is content-addressed over the procedure text
  and its source case ids (``improve/candidate.py``), which is exactly what makes a resumed run
  produce the same id. The improvement run id is provenance, never an input to the hash.
* **It does not print.** ``json_payload``/``render_json``/``describe`` are projections; the CLI
  writes them. That is what lets the four-command proof compare in-process and shelled-out runs.

Every seam the cycle needs but may not reach arrives as an argument: the solver, the reflection
model, and the procedure-envelope builder that lives in ``loop/prompt.py``. ``improve/`` may not
import ``loop/``, and injection is the standing answer to an import-table wall here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from harnessed import clock, ids
from harnessed.errors import ImproveError
from harnessed.evals.runner import CaseResult, EvalRun, Solve, run_suite
from harnessed.evals.scoring import RunResult
from harnessed.evals.suite import Suite
from harnessed.events.bus import EventBus
from harnessed.improve import candidate as candidate_module
from harnessed.improve import promoter as promoter_module
from harnessed.improve import reflector as reflector_module
from harnessed.improve.candidate import Build
from harnessed.improve.promoter import Outcome
from harnessed.improve.reflector import Model
from harnessed.memory import db, procedural, redact

__all__ = [
    "BASELINE",
    "CANDIDATE",
    "Cycle",
    "HOLDOUT",
    "Measurement",
    "Model",
    "PROMOTE",
    "REFLECT",
    "RUN_FINISHED",
    "RUN_STARTED",
    "STEPS",
    "STEP_RECORDED",
    "Step",
    "cycle_for",
    "describe",
    "get",
    "json_payload",
    "latest",
    "measurement",
    "put",
    "record",
    "render_json",
    "run_cycle",
    "start",
]

#: The two moments FR-18 requires on the bus. Both are in the frozen taxonomy.
RUN_STARTED: Final = "improve.run.started"
STEP_RECORDED: Final = "improve.step.recorded"

#: The cycle's end, published on every return path of :func:`run_cycle` and never by a cycle
#: that raises. It is what closes an improve session's stream: the inner evals publish
#: ``eval.run.finished`` on this same bus, so only this name can say the cycle is over
#: (decision 17, ticket 081).
RUN_FINISHED: Final = "improve.run.finished"

#: The prefix ``ids.new_id`` stamps on an improvement run.
IMPROVE_ID_PREFIX: Final = "imp"

BASELINE: Final = "baseline"
REFLECT: Final = "reflect"
CANDIDATE: Final = "candidate"
HOLDOUT: Final = "holdout"
PROMOTE: Final = "promote"

#: The cycle in order. A step name outside this tuple is refused at write time, so a typo fails
#: where it happened rather than reappearing as a step that never resumes.
STEPS: Final[tuple[str, ...]] = (BASELINE, REFLECT, CANDIDATE, HOLDOUT, PROMOTE)

#: What the reflect step records when there was nothing to say. An empty id is an answer - the
#: baseline did not fail anything the corpus keys on - and recording it is what stops a resumed
#: cycle from asking the same question a second time.
NO_CANDIDATE: Final = ""

_COLUMNS: Final = "improvement_run_id, step, key, payload, created_at"

_INSERT: Final = f"INSERT INTO improvement_steps ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)"

_SELECT_ONE: Final = (
    f"SELECT {_COLUMNS} FROM improvement_steps "
    f"WHERE improvement_run_id = ? AND step = ? AND key = ?"
)

_INSERT_RUN: Final = (
    "INSERT INTO improvement_runs (improvement_run_id, suite, started_at) VALUES (?, ?, ?)"
)

_FINISH_RUN: Final = "UPDATE improvement_runs SET finished_at = ? WHERE improvement_run_id = ?"

_SELECT_LATEST_RUN: Final = (
    "SELECT improvement_run_id FROM improvement_runs WHERE suite = ? "
    "ORDER BY started_at DESC, rowid DESC LIMIT 1"
)

_SELECT_RUN: Final = "SELECT suite FROM improvement_runs WHERE improvement_run_id = ?"


# ---- what a row is ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Step:
    """One ``improvement_steps`` row.

    ``replayed`` is how a caller tells a step this pass actually performed from one read back off
    the ledger. The payload is identical either way - that is the point of the ledger - but the
    distinction is what makes "the resumed cycle repeated nothing" observable.
    """

    improvement_run_id: str
    step: str
    key: str
    payload: dict[str, object]
    created_at: str
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Measurement:
    """One scored eval run, as a measurement step recorded it.

    Carries whole :class:`~harnessed.evals.runner.CaseResult` values rather than a score, because
    the reflection is built from the failing cases' ``detail`` and a resumed cycle must be able
    to ask the same question the interrupted one asked.
    """

    eval_run_id: str
    suite: str
    label: str
    cases: tuple[CaseResult, ...]
    #: How many times each case was drawn to produce these numbers (ticket 050). Carried through
    #: the payload rather than defaulted here, because the promoter refuses a comparison drawn
    #: fewer times than the caller's minimum, and a measurement that forgot its draws is refused
    #: however many it actually paid for (ticket 052).
    samples: int = 1
    #: The provider and model the eval run was opened under (FR-72). ``None`` on a step recorded
    #: before ticket 131, whose payload never carried them: unknown, never a default name.
    provider: str | None = None
    model: str | None = None

    @property
    def result(self) -> RunResult:
        """The projection ``evals/scoring.py`` compares. The pass is carried, never re-derived."""
        return RunResult.from_cases((case.outcome for case in self.cases), samples=self.samples)

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        """The cases that did not pass, in the order the suite declared them."""
        return tuple(case for case in self.cases if not case.passed)


@dataclass(frozen=True, slots=True)
class Cycle:
    """One flywheel cycle: what it measured, what it produced and what it decided.

    Every field is reconstructible from ``improvement_steps`` alone, which is the whole of FR-18:
    a cycle re-issued after an interruption returns the same object the uninterrupted one would
    have returned, having paid only for the steps that were missing.
    """

    improvement_run_id: str
    suite: str
    cand_id: str
    resumed: bool
    dry_run: bool
    steps: tuple[Step, ...]
    before: Measurement | None = None
    after: Measurement | None = None
    holdout: Measurement | None = None
    outcome: Outcome | None = None

    @property
    def has_candidate(self) -> bool:
        """Whether the reflection produced anything. An empty id is an answer, not an error."""
        return bool(self.cand_id)

    @property
    def promoted(self) -> bool:
        return self.outcome is not None and self.outcome.promoted

    @property
    def rule(self) -> str:
        """The deciding rule, or the empty string when no decision was reached."""
        return "" if self.outcome is None else self.outcome.rule

    @property
    def score_before(self) -> float | None:
        """The decided value, else the measured one, else ``None``: never a score nobody took."""
        if self.outcome is not None:
            return self.outcome.score_before
        return None if self.before is None else self.before.result.score

    @property
    def score_after(self) -> float | None:
        """As :attr:`score_before`, for the candidate's measurement (FR-46, ticket 095)."""
        if self.outcome is not None:
            return self.outcome.score_after
        return None if self.after is None else self.after.result.score

    @property
    def scored(self) -> int:
        """How many steps this pass actually paid for. Zero on a re-issued completed cycle."""
        return sum(1 for step in self.steps if not step.replayed)


# ---- the improvement run ----------------------------------------------------------------------


def latest(conn: sqlite3.Connection, suite: str) -> str | None:
    """The most recent improvement run over ``suite``, or ``None``. A miss is an answer."""
    row = conn.execute(_SELECT_LATEST_RUN, (suite,)).fetchone()
    return None if row is None else str(row[0])


def start(conn: sqlite3.Connection, suite: str, *, fresh: bool = False) -> tuple[str, bool]:
    """Settle which improvement run this pass belongs to, and say whether it was resumed.

    Without ``fresh`` the most recent run over this suite is continued, whether it was interrupted
    or finished - so re-issuing the same command is idempotent and costs nothing. ``fresh`` is the
    documented way to start a second cycle, and it leaves the previous one exactly as it stood.
    The same rule ``evals/runner.py`` applies to an eval run, for the same reason.
    """
    if not fresh:
        existing = latest(conn, suite)
        if existing is not None:
            return existing, True
    improvement_run_id = ids.new_id(IMPROVE_ID_PREFIX)
    db.write(
        conn,
        ((_INSERT_RUN, (improvement_run_id, suite, clock.isoformat(clock.now()))),),
    )
    return improvement_run_id, False


def finish(conn: sqlite3.Connection, improvement_run_id: str) -> None:
    """Stamp the cycle finished. Re-issuing a finished cycle simply stamps it again."""
    db.write(conn, ((_FINISH_RUN, (clock.isoformat(clock.now()), improvement_run_id)),))


# ---- the ledger, the pattern verbatim ----------------------------------------------------------


def get(conn: sqlite3.Connection, improvement_run_id: str, step: str, key: str) -> Step | None:
    """One recorded step, or ``None``. A miss is an answer, not an error."""
    row = conn.execute(_SELECT_ONE, (improvement_run_id, step, key)).fetchone()
    if row is None:
        return None
    stored_run, stored_step, stored_key, payload, created_at = row
    return Step(
        improvement_run_id=str(stored_run),
        step=str(stored_step),
        key=str(stored_key),
        payload=_loads(str(payload)),
        created_at=str(created_at),
    )


def put(
    conn: sqlite3.Connection,
    improvement_run_id: str,
    step: str,
    key: str,
    payload: Mapping[str, object],
) -> Step:
    """Scrub the payload and write the row. One short transaction, committed before we return.

    Committed before we return is the load-bearing half: the next step may not begin until this
    row is visible to a connection that did not write it.
    """
    if step not in STEPS:
        raise ImproveError(
            f"unknown improvement step {step!r}. The cycle is {', '.join(STEPS)}, and a step "
            f"outside it would record done-state no resumed run ever reads."
        )
    scrubbed = redact.scrub(dict(payload))
    created_at = clock.isoformat(clock.now())
    db.write(
        conn,
        ((_INSERT, (improvement_run_id, step, key, _dumps(scrubbed), created_at)),),
    )
    return Step(
        improvement_run_id=improvement_run_id,
        step=step,
        key=key,
        payload=scrubbed,
        created_at=created_at,
    )


def record(
    conn: sqlite3.Connection,
    improvement_run_id: str,
    step: str,
    key: str,
    work: Callable[[], Mapping[str, object]],
    *,
    publish: Publish | None = None,
) -> Step:
    """The ledger pattern, and the only way a step ever runs.

    ``work`` is not called when the row is already there, which is what makes the resume free
    rather than merely correct. The event announces the row, so a replay publishes nothing: a
    listener counting decisions per cycle reads the ledger, never the bus.
    """
    existing = get(conn, improvement_run_id, step, key)
    if existing is not None:
        return replace(existing, replayed=True)
    stored = put(conn, improvement_run_id, step, key, work())
    if publish is not None:
        publish(
            STEP_RECORDED,
            {"improvement_run_id": improvement_run_id, "step": step, "key": key},
        )
    return stored


# ---- payload projections -----------------------------------------------------------------------


def measurement(step: Step) -> Measurement:
    """The eval run a measurement step recorded, rebuilt from its row and nothing else."""
    payload = step.payload
    rows = payload.get("cases", [])
    if not isinstance(rows, list):
        raise ImproveError(
            f"step {step.step!r} of {step.improvement_run_id} recorded no case list, so the run "
            f"it measured cannot be recovered without re-running it."
        )
    return Measurement(
        eval_run_id=str(payload.get("eval_run_id", "")),
        suite=str(payload.get("suite", "")),
        label=str(payload.get("label", "")),
        cases=tuple(_case(row) for row in rows),
        samples=int(str(payload.get("samples", 1))),
        provider=_optional_text(payload.get("provider")),
        model=_optional_text(payload.get("model")),
    )


def _optional_text(value: object) -> str | None:
    """A recorded string field, or ``None`` where the step never recorded one."""
    return None if value is None else str(value)


def _measurement_payload(run: EvalRun) -> dict[str, object]:
    """One eval run flattened into the row. Complete, because the resume rebuilds from it."""
    return {
        "eval_run_id": run.eval_run_id,
        "suite": run.suite,
        "label": run.label,
        "samples": run.samples,
        "provider": run.provider,
        "model": run.model,
        "cases": [
            {
                "case_id": case.case_id,
                "score": case.score,
                "passed": case.passed,
                "detail": case.detail,
                "attempt": case.attempt,
            }
            for case in run.cases
        ],
    }


def _case(row: object) -> CaseResult:
    """One recorded case, read positionally-by-name into the frozen type the runner owns."""
    if not isinstance(row, Mapping):
        raise ImproveError(f"a recorded case must be an object, got {type(row).__name__}")
    return CaseResult(
        case_id=str(row["case_id"]),
        score=float(row["score"]),
        passed=bool(row["passed"]),
        detail=str(row["detail"]),
        attempt=int(row["attempt"]),
    )


def _reflection_payload(cand_id: str, cases: Sequence[str]) -> dict[str, object]:
    return {"cand_id": cand_id, "source_case_ids": list(cases)}


def _outcome_payload(outcome: Outcome) -> dict[str, object]:
    """The decision as the row holds it: the numbers and the rule, counts rather than detail.

    A replayed outcome carries a truthful count and an empty detail tuple, which beats one that
    pretends the per-case detail survived a round trip through a column of counts.
    """
    return {
        "cand_id": outcome.cand_id,
        "promoted": outcome.promoted,
        "rule": outcome.rule,
        "score_before": outcome.score_before,
        "score_after": outcome.score_after,
        "pass_count_before": outcome.pass_count_before,
        "pass_count_after": outcome.pass_count_after,
        "regression_count": outcome.regression_count,
        "soft_drop_count": outcome.soft_drop_count,
        "samples_before": outcome.samples_before,
        "samples_after": outcome.samples_after,
        "decided_at": outcome.decided_at,
    }


def _outcome(step: Step) -> Outcome:
    """The recorded decision, back as the type the promoter reports."""
    payload = step.payload
    return Outcome(
        cand_id=str(payload["cand_id"]),
        promoted=bool(payload["promoted"]),
        rule=str(payload["rule"]),
        score_before=float(str(payload["score_before"])),
        score_after=float(str(payload["score_after"])),
        pass_count_before=int(str(payload["pass_count_before"])),
        pass_count_after=int(str(payload["pass_count_after"])),
        regression_count=int(str(payload["regression_count"])),
        soft_drop_count=int(str(payload["soft_drop_count"])),
        samples_before=int(str(payload.get("samples_before", 1))),
        samples_after=int(str(payload.get("samples_after", 1))),
        decided_at=str(payload["decided_at"]),
        replayed=step.replayed,
    )


# ---- the cycle -----------------------------------------------------------------------------------

#: How an event reaches the bus, injected for the same reason every other seam here is.
Publish = Callable[[str, Mapping[str, object]], object]


def run_cycle(
    conn: sqlite3.Connection,
    *,
    suite: Suite,
    holdout: Suite | None,
    solve: Solve,
    reflector: Model,
    model: str,
    provider: str,
    build: Build,
    dry_run: bool = False,
    fresh: bool = False,
    samples: int = 1,
    minimum_samples: int = 1,
    bus: EventBus | None = None,
    on_start: Callable[[str], None] | None = None,
    on_step: Callable[[str], None] | None = None,
) -> Cycle:
    """Measure, reflect, measure again, check the unseen suite, then decide - each step a row.

    ``dry_run`` stops before the promotion, so nothing reaches the active procedure set. Every
    earlier step is still recorded, which means a dry run followed by a real one resumes at the
    decision rather than re-measuring anything.

    ``holdout`` is the unseen suite the reflector never read. Passing ``None`` is permitted and
    fails the decision closed (FR-19): a cycle that did not look outside the reflector's view has
    no evidence a promotion could rest on.

    ``provider`` is the configured provider's name, recorded on every eval run the cycle opens
    beside ``model`` (FR-72). No default, for the reason ``run_suite`` has none.

    ``on_step`` is handed each step's name at the top of that step, before the ledger is read. It
    is what lets a caller - or a test - observe the world as it stood between two steps.
    """
    improvement_run_id, resumed = start(conn, suite.name, fresh=fresh)
    if on_start is not None:
        on_start(improvement_run_id)
    publish = _publisher(bus)
    _announce(publish, improvement_run_id, suite, resumed=resumed, dry_run=dry_run)

    recorded: list[Step] = []
    active = tuple(procedure.text for procedure in procedural.active(conn))

    def step(name: str, key: str, work: Callable[[], Mapping[str, object]]) -> Step:
        if on_step is not None:
            on_step(name)
        stored = record(conn, improvement_run_id, name, key, work, publish=publish)
        recorded.append(stored)
        return stored

    baseline = measurement(
        step(
            BASELINE,
            suite.name,
            lambda: _measurement_payload(
                _measure(
                    suite,
                    solve,
                    conn,
                    model=model,
                    provider=provider,
                    system=build("", active),
                    label=f"{improvement_run_id}/{BASELINE}",
                    samples=samples,
                    bus=bus,
                )
            ),
        )
    )

    cand_id = str(
        step(
            REFLECT,
            suite.name,
            lambda: _reflect(
                reflector,
                baseline,
                conn,
                suite=suite,
                model=model,
                provenance=improvement_run_id,
                publish=publish,
            ),
        ).payload["cand_id"]
    )
    if cand_id == NO_CANDIDATE:
        return _settled(
            conn,
            Cycle(
                improvement_run_id=improvement_run_id,
                suite=suite.name,
                cand_id=NO_CANDIDATE,
                resumed=resumed,
                dry_run=dry_run,
                steps=tuple(recorded),
                before=baseline,
            ),
            publish,
        )

    text = _text_of(conn, cand_id)
    improved = build("", (*active, text))

    after = measurement(
        step(
            CANDIDATE,
            cand_id,
            lambda: _measurement_payload(
                _measure(
                    suite,
                    solve,
                    conn,
                    model=model,
                    provider=provider,
                    system=improved,
                    label=f"{improvement_run_id}/{CANDIDATE}",
                    samples=samples,
                    bus=bus,
                )
            ),
        )
    )

    unseen: Measurement | None = None
    if holdout is not None:
        unseen = measurement(
            step(
                HOLDOUT,
                cand_id,
                lambda: _measurement_payload(
                    _measure(
                        holdout,
                        solve,
                        conn,
                        model=model,
                        provider=provider,
                        system=improved,
                        label=f"{improvement_run_id}/{HOLDOUT}",
                        samples=samples,
                        bus=bus,
                    )
                ),
            )
        )

    cycle = Cycle(
        improvement_run_id=improvement_run_id,
        suite=suite.name,
        cand_id=cand_id,
        resumed=resumed,
        dry_run=dry_run,
        steps=tuple(recorded),
        before=baseline,
        after=after,
        holdout=unseen,
    )
    if dry_run:
        return _settled(conn, cycle, publish)

    decision = step(
        PROMOTE,
        cand_id,
        lambda: _outcome_payload(
            promoter_module.promote(
                cand_id,
                text,
                baseline.result,
                after.result,
                None if unseen is None else unseen.result,
                lookup=_lookup(conn),
                commit=_commit(conn),
                publish=publish,
                minimum_samples=minimum_samples,
            )
        ),
    )
    return _settled(
        conn,
        replace(cycle, steps=tuple(recorded), outcome=_outcome(decision)),
        publish,
    )


def cycle_for(conn: sqlite3.Connection, improvement_run_id: str) -> Cycle | None:
    """One recorded cycle, rebuilt from the ledger by its id, or ``None``. A miss is an answer.

    This is the resume path of :func:`run_cycle` with the work removed: the run row names the
    suite, the suite keys the baseline and the reflection, the reflection names the candidate,
    and the candidate keys the rest. Every projection is the one the resume already uses, so a
    read-back cannot report a number a resumed run would not (FR-40, ticket 074).

    Three fields describe a pass rather than a cycle and have no column to be read from:
    ``resumed``, ``dry_run`` and each step's ``replayed``. A read-back reports the cycle as a
    whole - not resumed, not dry, every recorded step counted as paid for - which is what a fresh
    full run reports, and what ``json_payload`` renders identically on both sides.

    A run row with no step rows is an empty cycle, not a miss: the run started and was interrupted
    before its first step landed, and that is a fact the ledger holds.
    """
    row = conn.execute(_SELECT_RUN, (improvement_run_id,)).fetchone()
    if row is None:
        return None
    suite = str(row[0])
    recorded: list[Step] = []

    def stored(step: str, key: str) -> Step | None:
        found = get(conn, improvement_run_id, step, key)
        if found is not None:
            recorded.append(found)
        return found

    baseline = stored(BASELINE, suite)
    reflection = stored(REFLECT, suite)
    cand_id = NO_CANDIDATE if reflection is None else str(reflection.payload["cand_id"])
    after = holdout = decision = None
    if cand_id != NO_CANDIDATE:
        after = stored(CANDIDATE, cand_id)
        holdout = stored(HOLDOUT, cand_id)
        decision = stored(PROMOTE, cand_id)

    return Cycle(
        improvement_run_id=improvement_run_id,
        suite=suite,
        cand_id=cand_id,
        resumed=False,
        dry_run=False,
        steps=tuple(recorded),
        before=None if baseline is None else measurement(baseline),
        after=None if after is None else measurement(after),
        holdout=None if holdout is None else measurement(holdout),
        outcome=None if decision is None else _outcome(decision),
    )


def _settled(conn: sqlite3.Connection, cycle: Cycle, publish: Publish | None) -> Cycle:
    """Close a cycle on its way out: every return path of :func:`run_cycle` passes here.

    The ledger stamp skips a dry run, which reached its own end early and leaves the decision
    for a later pass. The finish event does not skip it: the pass is over either way, and a
    client streaming this session is waiting on exactly that (decision 17).
    """
    if not cycle.dry_run:
        finish(conn, cycle.improvement_run_id)
    if publish is not None:
        publish(
            RUN_FINISHED,
            {
                "improvement_run_id": cycle.improvement_run_id,
                "cand_id": cycle.cand_id,
                "dry_run": cycle.dry_run,
                "promoted": cycle.promoted,
            },
        )
    return cycle


def _measure(
    suite: Suite,
    solve: Solve,
    conn: sqlite3.Connection,
    *,
    model: str,
    provider: str,
    system: str,
    label: str,
    samples: int = 1,
    bus: EventBus | None,
) -> EvalRun:
    """One scored suite. The label carries the improvement run, so each cycle owns its runs.

    Without that, a second cycle would resume the previous cycle's finished eval run and report
    scores measured under a system prompt that is no longer the one in force.

    ``samples`` is how many times each case is drawn (ticket 050). Every measurement in a cycle
    draws the same number of times, so before and after are compared like with like.
    """
    return run_suite(
        suite,
        solve,
        conn,
        model=model,
        provider=provider,
        system=system,
        label=label,
        samples=samples,
        bus=bus,
    )


def _reflect(
    reflector: Model,
    baseline: Measurement,
    conn: sqlite3.Connection,
    *,
    suite: Suite,
    model: str,
    provenance: str,
    publish: Publish | None,
) -> dict[str, object]:
    """Ask once about the baseline's failures, and record the answer - including no answer.

    ``visible`` is the suite the caller just ran and nothing else, which is what keeps the
    reflection blind to the unseen suite (FR-19). It is the reflector's allowlist, not a filter.
    """
    written = reflector_module.reflect(
        reflector,
        baseline.failures,
        conn,
        visible=suite.case_ids,
        model_name=model,
        provenance=provenance,
        publish=publish,
    )
    if written is None:
        return _reflection_payload(NO_CANDIDATE, ())
    return _reflection_payload(written.cand_id, written.source_case_ids)


def _text_of(conn: sqlite3.Connection, cand_id: str) -> str:
    """The candidate's procedure text, or an error naming the id the ledger pointed at."""
    stored = candidate_module.get(conn, cand_id)
    if stored is None:
        raise ImproveError(
            f"the reflect step recorded candidate {cand_id!r}, but no candidate is stored under "
            f"that id. The candidate row is written before the step that names it."
        )
    return stored.text


def _lookup(conn: sqlite3.Connection) -> Callable[[str], Outcome | None]:
    """The recorded decision for a candidate id, read back as the promoter's own type."""

    def read(cand_id: str) -> Outcome | None:
        recorded = procedural.promotion(conn, cand_id)
        if recorded is None:
            return None
        return Outcome(
            cand_id=recorded.cand_id,
            promoted=recorded.promoted,
            rule=recorded.reason,
            score_before=recorded.score_before,
            score_after=recorded.score_after,
            pass_count_before=recorded.passes_before,
            pass_count_after=recorded.passes_after,
            regression_count=recorded.regressions,
            soft_drop_count=recorded.soft_drops,
            decided_at=recorded.decided_at,
        )

    return read


def _commit(conn: sqlite3.Connection) -> Callable[[Outcome, str], Outcome]:
    """Record the decision and, on an accept, activate the procedure. One transaction, in the
    store: the promoter may import ``evals.scoring`` and ``errors`` and nothing else."""

    def write(outcome: Outcome, text: str) -> Outcome:
        recorded = procedural.record_promotion(
            conn,
            cand_id=outcome.cand_id,
            text=text,
            promoted=outcome.promoted,
            reason=outcome.rule,
            score_before=outcome.score_before,
            score_after=outcome.score_after,
            passes_before=outcome.pass_count_before,
            passes_after=outcome.pass_count_after,
            regressions=outcome.regression_count,
            soft_drops=outcome.soft_drop_count,
        )
        return replace(outcome, decided_at=recorded.decided_at)

    return write


def _publisher(bus: EventBus | None) -> Publish | None:
    """The bus behind the one-call seam every module in this package publishes through."""
    if bus is None:
        return None

    def publish(name: str, payload: Mapping[str, object]) -> object:
        return bus.publish(name, dict(payload))

    return publish


def _announce(
    publish: Publish | None,
    improvement_run_id: str,
    suite: Suite,
    *,
    resumed: bool,
    dry_run: bool,
) -> None:
    if publish is None:
        return
    publish(
        RUN_STARTED,
        {
            "improvement_run_id": improvement_run_id,
            "suite": suite.name,
            "case_count": len(suite.cases),
            "resumed": resumed,
            "dry_run": dry_run,
        },
    )


# ---- the projections a CLI renders ---------------------------------------------------------------


def json_payload(cycle: Cycle) -> dict[str, object]:
    """One cycle as plain data: what ``harnessed improve run --json`` hands to a parser.

    Deliberately flat and deliberately complete, because the shell-parity half of the FR-25 proof
    compares this object across a process boundary: every number a human reads must be in it.

    An undecided cycle serves ``None`` for the four counts, and for each score nothing measured:
    a ``0`` there would be a number no ledger holds (FR-46, ticket 095).

    ``provider`` and ``model`` are the baseline eval run's, the measurement every later step is
    compared against (FR-72). ``None`` when the baseline step predates ticket 131.
    """
    outcome = cycle.outcome
    baseline = cycle.before
    return {
        "improvement_run_id": cycle.improvement_run_id,
        "suite": cycle.suite,
        "cand_id": cycle.cand_id,
        "candidate": cycle.has_candidate,
        "dry_run": cycle.dry_run,
        "resumed": cycle.resumed,
        "promoted": cycle.promoted,
        "rule": cycle.rule,
        "score_before": cycle.score_before,
        "score_after": cycle.score_after,
        "pass_count_before": None if outcome is None else outcome.pass_count_before,
        "pass_count_after": None if outcome is None else outcome.pass_count_after,
        "regressions": None if outcome is None else outcome.regression_count,
        "soft_drops": None if outcome is None else outcome.soft_drop_count,
        "scored": cycle.scored,
        "steps": [step.step for step in cycle.steps],
        "provider": None if baseline is None else baseline.provider,
        "model": None if baseline is None else baseline.model,
    }


def render_json(cycle: Cycle) -> str:
    """The single JSON object ``--json`` emits, and nothing else - no banner, no trailing prose.

    A string rather than a write, because output belongs to ``cli/render.py``. The caller writes
    exactly this and adds at most a newline.
    """
    return _dumps(json_payload(cycle))


def describe(cycle: Cycle, *, provider: str) -> str:
    """The cycle as an operator reads it. Rendering is separated from running, as everywhere here.

    "no candidate" is a sentence rather than a blank, because a cycle that found nothing to say
    and a cycle that failed to run must not look the same in a transcript.

    ``provider`` is the session's configured provider and it is a required keyword, passed down
    to ``promoter.describe`` unread by this function. It decides one line: the caveat scoping a
    promotion to the mechanism rather than to model quality. This module used to hand the
    promoter the literal ``fake``, so the caveat was printed under every provider - which is the
    same disclaimer under a live vendor, where it is false, and a disclaimer nobody reads by the
    day it is true. No default, because the value this argument would default to is the bug
    (ticket 053).
    """
    lines = [f"improvement run {cycle.improvement_run_id} over suite {cycle.suite}"]
    if cycle.resumed:
        lines.append(f"resumed - {cycle.scored} of {len(cycle.steps)} steps run this pass")
    if not cycle.has_candidate:
        lines.append("no candidate - the baseline failed nothing this reflection could act on")
        return "\n".join(lines)

    lines.append(f"candidate {cycle.cand_id}")
    lines.append(f"score {_score(cycle.score_before)} -> {_score(cycle.score_after)}")
    if cycle.dry_run:
        lines.append("dry run - no promotion was decided and the active set is untouched")
        return "\n".join(lines)
    if cycle.outcome is not None:
        lines.append(promoter_module.describe(cycle.outcome, provider=provider))
    return "\n".join(lines)


def _score(value: float | None) -> str:
    """A score as ``describe`` prints it; a side nothing measured says so rather than ``0.0``."""
    return "not measured" if value is None else str(value)


def _dumps(payload: object) -> str:
    """Canonical JSON, the spelling ``architecture.md`` fixes under "Format"."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _loads(raw: str) -> dict[str, object]:
    """A stored payload, back as a mapping.

    Decoded through an explicit decoder rather than the module-level shorthand: that shorthand is
    quarantined to ``providers/translate.py``, the one module permitted to parse model output,
    and a package-wide scan reads text rather than intent (ticket 017).
    """
    decoded = json.JSONDecoder().decode(raw)
    if not isinstance(decoded, dict):
        raise ImproveError(f"a step payload must be a JSON object, got {type(decoded).__name__}")
    return {str(key): value for key, value in decoded.items()}
