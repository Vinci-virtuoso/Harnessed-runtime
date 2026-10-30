"""``router_probes`` - what ``harnessed doctor --live`` proved about the pinned router model - and
``decisions``/``decision_options`` - one row per routed decision, written before its handler
starts (ticket 161, FR-108, decision 61).

FR-97 (UJ-21), decision 57's live rung. The rung sends one question of each type to
:data:`~harnessed.config.Settings.router_model` and records the outcome here, passing or
failed, so a later reader has proof instead of a claim. Modeled on ``acp/probes.py::record``
(ticket 089): here too every probe is a new row, never replayed or updated - migration 013 is
the same append-only shape for the same reason, a newer failure must not be shadowed by an
older pass.

**Rounding, once** (MUST rule 29 extended, decision 61): :func:`round_four` and
:func:`round_percent` are the only functions in this project that round a served probability,
confidence, floor or threshold, each computed once from the raw :class:`~decimal.Decimal` so
neither is derived from the other's already-rounded string. :func:`to_nanos` is cost's twin: the
exact reported text is stored verbatim, and the integer nanodollars beside it are for a sum that
must never touch a float.

``probe_state`` - reading the newest probe back for the ladder - is ticket 186's. ``for_session``
(FR-120, decision 67) is this ticket's: the one store read behind ``GET
/v1/sessions/{session_id}/decisions``, a session's decisions in order with their options nested,
every figure already the string :func:`record_decision` stored. It is a read beside the writes
above, never a rewrite of them: an unknown session raises :class:`SurfaceNotFound`, read off
``events/read.py::held`` (ticket 076's rule) rather than a second existence query over
``decisions`` itself, since a session that recorded turns but never routed still exists.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final

from harnessed import clock, config
from harnessed.acp import config as acp_config
from harnessed.errors import SurfaceNotFound
from harnessed.events import read as events_read
from harnessed.memory import db, redact
from harnessed.providers.jev import Choice, Decisions, Score
from harnessed.router.questions import NEEDS_MEMORY_THRESHOLD, floor_for, route_options

__all__ = [
    "COMPARED_REASON",
    "COMPARED_SQL",
    "COMPARED_WORDS",
    "FAILED",
    "PASSED",
    "ROUTING_WORDS",
    "DecisionOption",
    "DecisionRow",
    "RouteFloor",
    "RouterProbe",
    "RouterState",
    "for_session",
    "json_payload",
    "probe_state",
    "record_decision",
    "record_probe",
    "round_four",
    "round_percent",
    "router_json_payload",
    "to_nanos",
]

#: The probe reached the pinned model and its answer parsed clean.
PASSED: Final = "passed"
#: The probe raised a `DecisionError` - unreachable, timed out, or a drifted, refused answer.
FAILED: Final = "failed"

#: The two figures an uncertain stop can have compared with its floor (decision 101, FR-171).
COMPARED_WORDS: Final = ("done_probability", "confidence")

#: What `/v1/router`'s `routing` can say (ticket 316, decision 105); `HUMAN_WORDS` holds a row each.
ROUTING_WORDS: Final = ("on", "off")
#: Why ``compared`` is ``null``: only an ``uncertain`` decision compared a figure with a floor.
COMPARED_REASON: Final = "the router acted"

#: Which figure an uncertain decision ``d`` compared with its floor (decision 101): ``done``'s own
#: probability when it is hop 0 of a task continuation, routed ``done``, and its stored
#: ``confidence`` equals the stored ``done`` option's probability (ticket 281); ``confidence``
#: otherwise, which is what a pre-281 row compared; ``NULL`` for any other outcome. The one
#: expression every read selects, over a ``decisions`` row aliased ``d``; ``loop/messages.py`` and
#: ``tasks/store.py`` copy it word for word, held equal by tests (``loop/`` may not import here).
COMPARED_SQL: Final = (
    "CASE WHEN d.outcome = 'uncertain' THEN "
    "CASE WHEN d.hop = 0 AND d.route = 'done' "
    "AND EXISTS (SELECT 1 FROM task_messages tm WHERE tm.run_id = d.run_id "
    "AND tm.message_seq = d.message_seq AND tm.kind = 'continuation') "
    "AND d.confidence = (SELECT o.probability FROM decision_options o WHERE o.run_id = d.run_id "
    "AND o.idx = d.idx AND o.question = 'route' AND o.option = 'done') "
    "THEN 'done_probability' ELSE 'confidence' END END"
)

_INSERT: Final = (
    "INSERT INTO router_probes (model, status, latency_seconds, cost, detail, probed_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True, slots=True)
class RouterProbe:
    """One recorded probe of the pinned router model, as stored."""

    model: str
    status: str
    latency_seconds: str
    cost: str | None
    detail: str
    probed_at: str


def record_probe(
    conn: sqlite3.Connection,
    *,
    model: str,
    status: str,
    latency_seconds: str,
    cost: str | None,
    detail: str,
) -> RouterProbe:
    """Append one router probe result, scrubbed, and return it as stored.

    ``cost`` is ``None`` on a failed probe: a refusal or a transport error means no usage was
    ever reported to round to a cost, and storing a guess would be worse than the null.
    """
    probe = RouterProbe(
        model=model,
        status=status,
        latency_seconds=latency_seconds,
        cost=cost,
        detail=redact.scrub(detail),
        probed_at=clock.isoformat(clock.now()),
    )
    db.write(
        conn,
        (
            (
                _INSERT,
                (
                    probe.model,
                    probe.status,
                    probe.latency_seconds,
                    probe.cost,
                    probe.detail,
                    probe.probed_at,
                ),
            ),
        ),
    )
    return probe


# ---- decisions / decision_options: written before the handler starts (decision 61) --------------

#: A probability, a confidence, a floor or the threshold, four places, half-up (MUST rule 29).
_FOUR_PLACES: Final = Decimal("0.0001")
#: The same value as a whole percent, half-up.
_WHOLE: Final = Decimal("1")
#: Nanodollars per dollar - the unit a sum over `cost_nanos` is in.
_NANOS_PER_DOLLAR: Final = Decimal("1000000000")

_INSERT_DECISION: Final = (
    "INSERT INTO decisions ("
    "run_id, idx, message_seq, hop, task, outcome, route, handler, confidence, "
    "confidence_percent, floor, floor_percent, needs_memory, needs_memory_percent, memory_kind, "
    "stop_reason, fallback_cause, state_truncated, latency_seconds, input_tokens, output_tokens, "
    "cost, cost_nanos, model, decision_id, task_hash, created_at"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_OPTION: Final = (
    "INSERT INTO decision_options "
    "(run_id, idx, question, position, option, description, probability, probability_percent) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_NEXT_IDX: Final = "SELECT COALESCE(MAX(idx), -1) + 1 FROM decisions WHERE run_id = ?"


def round_four(value: Decimal) -> str:
    """``value`` as a four-decimal string, half-up from the raw value (decision 61)."""
    return str(value.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP))


def round_percent(value: Decimal) -> str:
    """``value`` as a whole-percent string, half-up from the same raw value - never derived from
    :func:`round_four`'s already-rounded string (decision 61's own anti-pattern, MUST rule 29)."""
    return f"{(value * 100).quantize(_WHOLE, rounding=ROUND_HALF_UP)}%"


def to_nanos(cost: str) -> int:
    """``cost``'s exact reported text as integer nanodollars, half-up, for a sum that must never
    be a float (decision 61)."""
    return int((Decimal(cost) * _NANOS_PER_DOLLAR).quantize(_WHOLE, rounding=ROUND_HALF_UP))


def _next_idx(conn: sqlite3.Connection, run_id: str) -> int:
    """The next ``idx`` this run's decisions row gets: 0 for its first, read before every write."""
    return int(conn.execute(_NEXT_IDX, (run_id,)).fetchone()[0])


def _option_rows(
    run_id: str, idx: int, answered: Decisions
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    """One ``decision_options`` row per option of every answered question that carries odds.

    A :class:`~harnessed.providers.jev.Noul` answers with a bare probability, not one per option,
    and rides on the ``decisions`` row's own ``needs_memory`` column instead. ``description`` is
    a :class:`~harnessed.providers.jev.Score`'s legend text for that option; a
    :class:`~harnessed.providers.jev.Choice` carries no legend, so it is ``None`` until a Score
    question joins the vocabulary.
    """
    rows: list[tuple[str, tuple[object, ...]]] = []
    for question, answer in answered.answers.items():
        if not isinstance(answer, Choice | Score):
            continue
        legend = answer.legend if isinstance(answer, Score) else {}
        for position, (option, probability) in enumerate(answer.probabilities.items()):
            rows.append(
                (
                    _INSERT_OPTION,
                    (
                        run_id,
                        idx,
                        question,
                        position,
                        option,
                        legend.get(option),
                        round_four(probability),
                        round_percent(probability),
                    ),
                )
            )
    return tuple(rows)


def record_decision(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    message_seq: int,
    task: str,
    model: str,
    hop: int = 0,
    latency_seconds: str,
    outcome: str,
    route: str | None,
    handler: str | None,
    confidence: Decimal | None,
    floor: Decimal | None,
    needs_memory: Decimal | None,
    memory_kind: str | None,
    stop_reason: str | None,
    fallback_cause: str | None,
    state_truncated: bool,
    answered: Decisions | None,
) -> int:
    """Write one ``decisions`` row and its ``decision_options`` rows, scrubbed, in one transaction
    - **before** the caller's handler starts (MUST rule 6). Returns the row's ``idx``.

    ``answered`` is the raw :class:`~harnessed.providers.jev.Decisions` the Decider call returned,
    or ``None`` on a fallback: nothing was answered, so ``model``, ``decision_id`` and the cost
    columns fall back to what the caller already knew (the configured router model, decided
    before any call) rather than a guess, and no ``decision_options`` row is written. ``task`` is
    stored on ``hop = 0`` only (decision 61): a chain hop's row (ticket 176) leaves it ``NULL``,
    since the message's own row already carries the operator's text.
    """
    idx = _next_idx(conn, run_id)
    resolved_model = model if answered is None else answered.model
    decision_id = None if answered is None else answered.id
    cost = None if answered is None else answered.cost
    cost_nanos = None if cost is None else to_nanos(cost)
    input_tokens = None if answered is None else _as_int(answered.usage.get("input_tokens"))
    output_tokens = None if answered is None else _as_int(answered.usage.get("output_tokens"))
    row = (
        run_id,
        idx,
        message_seq,
        hop,
        redact.scrub(task) if hop == 0 else None,
        outcome,
        route,
        handler,
        None if confidence is None else round_four(confidence),
        None if confidence is None else round_percent(confidence),
        None if floor is None else round_four(floor),
        None if floor is None else round_percent(floor),
        None if needs_memory is None else round_four(needs_memory),
        None if needs_memory is None else round_percent(needs_memory),
        memory_kind,
        stop_reason,
        fallback_cause,
        int(state_truncated),
        latency_seconds,
        input_tokens,
        output_tokens,
        cost,
        cost_nanos,
        resolved_model,
        decision_id,
        None,  # task_hash: no delegation this ticket ever starts (163+ populates it).
        clock.isoformat(clock.now()),
    )
    options = () if answered is None else _option_rows(run_id, idx, answered)
    db.write(conn, ((_INSERT_DECISION, row), *options))
    return idx


def _as_int(value: object) -> int | None:
    """``usage``'s token count, or ``None`` when the answer's usage dict left it out."""
    return None if value is None else int(str(value))


# ---- for_session: the read behind GET /v1/sessions/{session_id}/decisions (decision 67) ---------

_SELECT_FOR_SESSION: Final = (
    "SELECT idx, message_seq, hop, outcome, route, handler, confidence, confidence_percent, "
    "floor, floor_percent, needs_memory, needs_memory_percent, memory_kind, stop_reason, "
    "fallback_cause, state_truncated, latency_seconds, cost, model, created_at, "
    f"{COMPARED_SQL} FROM decisions d WHERE run_id = ? ORDER BY idx"
)
_SELECT_OPTIONS_FOR_SESSION: Final = (
    "SELECT idx, question, option, description, probability, probability_percent "
    "FROM decision_options WHERE run_id = ? ORDER BY idx, question, position"
)


@dataclass(frozen=True, slots=True)
class DecisionOption:
    """One option of one question a decision answered, as the decisions read serves it."""

    question: str
    option: str
    description: str | None
    probability: str
    probability_percent: str


@dataclass(frozen=True, slots=True)
class DecisionRow:
    """One recorded decision, its options nested, as ``GET /v1/sessions/{id}/decisions`` and its
    CLI twin serve it (decision 67, FR-120). Every stored figure crosses as the string
    :func:`record_decision` wrote; nothing here rounds or recomputes one."""

    idx: int
    message_seq: int
    hop: int
    outcome: str
    route: str | None
    handler: str | None
    confidence: str | None
    confidence_percent: str | None
    floor: str | None
    floor_percent: str | None
    needs_memory: str | None
    needs_memory_percent: str | None
    memory_kind: str | None
    stop_reason: str | None
    fallback_cause: str | None
    state_truncated: bool
    latency_seconds: str
    cost: str | None
    model: str
    created_at: str
    options: tuple[DecisionOption, ...]
    #: ``done_probability`` or ``confidence`` for an uncertain stop, else ``None`` (decision 101).
    compared: str | None = None
    #: Why ``compared`` is ``None``; ``None`` when it is served.
    compared_reason: str | None = None


def for_session(conn: sqlite3.Connection, session_id: str) -> tuple[DecisionRow, ...]:
    """Every decision recorded under ``session_id``, in write order, each with its options.

    Raises :class:`~harnessed.errors.SurfaceNotFound` when the events table never held
    ``session_id`` (:func:`~harnessed.events.read.held`, ticket 076's rule): a session that
    exists but never routed - every turn Groq answered directly - still holds, and reads back as
    ``()``, never a 404.
    """
    if not events_read.held(conn, session_id):
        raise SurfaceNotFound(
            f"no session under {session_id!r}: the events table holds nothing under that id."
        )
    options_by_idx: dict[int, list[DecisionOption]] = {}
    for idx, question, option, description, probability, probability_percent in conn.execute(
        _SELECT_OPTIONS_FOR_SESSION, (session_id,)
    ):
        options_by_idx.setdefault(int(idx), []).append(
            DecisionOption(
                question=str(question),
                option=str(option),
                description=None if description is None else str(description),
                probability=str(probability),
                probability_percent=str(probability_percent),
            )
        )
    rows = []
    for (
        idx,
        message_seq,
        hop,
        outcome,
        route,
        handler,
        confidence,
        confidence_percent,
        floor,
        floor_percent,
        needs_memory,
        needs_memory_percent,
        memory_kind,
        stop_reason,
        fallback_cause,
        state_truncated,
        latency_seconds,
        cost,
        model,
        created_at,
        compared,
    ) in conn.execute(_SELECT_FOR_SESSION, (session_id,)):
        rows.append(
            DecisionRow(
                idx=int(idx),
                message_seq=int(message_seq),
                hop=int(hop),
                outcome=str(outcome),
                route=None if route is None else str(route),
                handler=None if handler is None else str(handler),
                confidence=None if confidence is None else str(confidence),
                confidence_percent=None if confidence_percent is None else str(confidence_percent),
                floor=None if floor is None else str(floor),
                floor_percent=None if floor_percent is None else str(floor_percent),
                needs_memory=None if needs_memory is None else str(needs_memory),
                needs_memory_percent=(
                    None if needs_memory_percent is None else str(needs_memory_percent)
                ),
                memory_kind=None if memory_kind is None else str(memory_kind),
                stop_reason=None if stop_reason is None else str(stop_reason),
                fallback_cause=None if fallback_cause is None else str(fallback_cause),
                state_truncated=bool(state_truncated),
                latency_seconds=str(latency_seconds),
                cost=None if cost is None else str(cost),
                model=str(model),
                created_at=str(created_at),
                options=tuple(options_by_idx.get(int(idx), ())),
                compared=None if compared is None else str(compared),
                compared_reason=COMPARED_REASON if compared is None else None,
            )
        )
    return tuple(rows)


def json_payload(rows: tuple[DecisionRow, ...]) -> dict[str, object]:
    """The one wire shape for :func:`for_session`: ``{"decisions": [asdict(row), ...]}``.

    ``dataclasses.asdict`` recurses into each row's nested ``options`` tuple on its own, so a
    :class:`DecisionOption` prints as its own dict with no reshaping here.
    """
    return {"decisions": [dataclasses.asdict(row) for row in rows]}


# ---- probe_state: the read behind GET /v1/router (ticket 186, decision 67, FR-121, G-32) ---------

_LATEST_PROBE: Final = (
    "SELECT model, status, latency_seconds, cost, detail, probed_at "
    "FROM router_probes ORDER BY probe_id DESC LIMIT 1"
)


@dataclass(frozen=True, slots=True)
class RouteFloor:
    """One route's confidence floor, as :func:`probe_state` serves it (percent form included)."""

    route: str
    floor: str
    floor_percent: str


@dataclass(frozen=True, slots=True)
class RouterState:
    """``GET /v1/router`` and its CLI twin, ``harnessed router status`` (decision 67, FR-121).

    ``model`` through ``probed_at`` are the newest recorded :class:`RouterProbe`, or all ``None``
    with ``probed=False`` before any live rung ever wrote one - the router has one pinned model,
    so the newest row is read unscoped, the same way ``acp/probes.py::latest`` reads the newest
    row for one agent. ``floors`` and the needs-memory threshold are never null: they come from
    ``agents.toml`` and the fixed vocabulary (``router/questions.py``), not from a probe, so an
    unprobed router still shows what it would route against.
    """

    model: str | None
    probed: bool
    status: str | None
    latency_seconds: str | None
    cost: str | None
    detail: str | None
    probed_at: str | None
    floors: tuple[RouteFloor, ...]
    needs_memory_threshold: str
    needs_memory_threshold_percent: str
    #: ``on`` or ``off`` (ticket 316, decision 105): whether the configured keys route a message.
    routing: str
    #: Why routing is off, in ``config.py``'s own words; ``None`` while it is on.
    routing_reason: str | None


def probe_state(conn: sqlite3.Connection, path: Path = acp_config.DEFAULT_PATH) -> RouterState:
    """The newest recorded router probe, beside every route's confidence floor.

    ``path`` is read exactly as ``acp/probes.py::states`` reads it for ``GET /v1/agents`` - the
    registry in the working directory, absent means no entries - and :func:`~harnessed.router.
    questions.route_options` widens the fixed Groq pair with one row per installed entry, in
    registry order; ``needs_human`` carries no floor (``route.py`` branches on it before any
    floor check) and is left out.
    """
    agents = acp_config.load_agents(path) if path.exists() else {}
    floors = tuple(
        RouteFloor(
            route=route,
            floor=round_four(floor_for(route, agents)),
            floor_percent=round_percent(floor_for(route, agents)),
        )
        for route in route_options(agents)
        if route != "needs_human"
    )
    threshold = NEEDS_MEMORY_THRESHOLD
    threshold_str = round_four(threshold)
    threshold_percent = round_percent(threshold)
    settings = config.load()
    routing = "on" if settings.routing_on() else "off"
    routing_reason = settings.routing_off_reason()
    row = conn.execute(_LATEST_PROBE).fetchone()
    if row is None:
        return RouterState(
            model=None,
            probed=False,
            status=None,
            latency_seconds=None,
            cost=None,
            detail=None,
            probed_at=None,
            floors=floors,
            needs_memory_threshold=threshold_str,
            needs_memory_threshold_percent=threshold_percent,
            routing=routing,
            routing_reason=routing_reason,
        )
    model, status, latency_seconds, cost, detail, probed_at = row
    return RouterState(
        model=str(model),
        probed=True,
        status=str(status),
        latency_seconds=str(latency_seconds),
        cost=None if cost is None else str(cost),
        detail=str(detail),
        probed_at=str(probed_at),
        floors=floors,
        needs_memory_threshold=threshold_str,
        needs_memory_threshold_percent=threshold_percent,
        routing=routing,
        routing_reason=routing_reason,
    )


def router_json_payload(state: RouterState) -> dict[str, object]:
    """The one wire shape for :func:`probe_state`: ``dataclasses.asdict(state)``.

    ``asdict`` recurses into ``floors`` on its own, exactly as :func:`json_payload` already
    relies on for a :class:`DecisionRow`'s nested options.
    """
    return dataclasses.asdict(state)
