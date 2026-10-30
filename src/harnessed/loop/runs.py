"""The loop-run listing over ``turns``, and the payload one loop run renders as (FR-41, FR-35).

Two read queries the journal never had. :func:`listing` is what ``GET /v1/runs`` renders and
:func:`run_payload` is what ``harnessed run --json`` prints and ``GET /v1/runs/{run_key}``
returns - one callable behind both, so the CLI and the gateway cannot diverge (MUST rule 16).
They live here rather than in ``gateway/`` because a query belongs to the package that owns its
table (MUST rule 14), and ``turns`` is the run journal's.

**The listing's defining hazard is the key shape.** ``turns.run_id`` holds two different things:
a plain **session id** from ``harnessed run`` (``ids.new_run_id()``, ``run_`` plus 26 characters
from an alphabet with no colon in it) and a colon-joined **case run key** from every scored eval
case (``evals/runner.py::case_run_key``, ``eval run id:case id:attempt``). A listing that
ignores the difference reports a five-case eval as five top-level runs - output that reads as
confirmation while the thing it confirms is not true. So every row carries :attr:`LoopRun.key_kind`
and nothing here unifies the two (MUST rule 15, decision 24).

``loop/`` may not import ``evals/``, so :func:`key_kind_of` is a local shape predicate rather than
a call into ``case_run_key``. The two definitions are pinned to each other by
``tests/test_loop_listing.py``, which imports both sides; the predicate is deliberately the
minimal one - a colon anywhere - because a stricter parse that a future case id shape slipped past
would misfile a case run as a session, and that is the exact defect the field exists to prevent.

Stored columns are carried as the strings they are, never re-parsed. The journal serialised the
request on the way in and the decoder is quarantined to ``providers/translate.py``; what this
module returns is read off typed columns - the reply text, the stop reason, the timestamps, a
count of tool rows - and nothing is rebuilt from the transcript.

**Seconds are the one thing computed here, and they are computed here on purpose** (decision
54). ``created_at - requested_at`` bounds a turn since migration 007, so no migration is needed
for :func:`turn_seconds`, a run's ``duration_seconds`` or the trace's per-iteration ``seconds``.
The count runs in SQL; the subtraction runs in Python through ``clock.parse``, because SQLite's
parsing of the ``...Z`` form is version-dependent and no figure is bet on it. Every figure is
rounded to one decimal in this module and emitted as a string (MUST rule 29). A turn from before
007 has no send time: it is **excluded and counted**, with its reason spelled beside the count,
never averaged as a silent subset; and nothing to average is ``None``, never ``"0.0"``.

**Spend is money, so it is ``Decimal`` from the stored string to the emitted one** (decision 53,
FR-74). :func:`spend` joins ``turns`` to ``prices`` - the rows ``harnessed prices refresh``
stored; nothing here asks a provider (MUST rule 28) - multiplies each count by its per-million
rate, rounds the sum half-up to six places and emits a string (MUST rule 29, decision 64). A turn
whose cost cannot be known is sorted into exactly one of three null classes, counted **beside** the
total and never folded into it: no usage (before 006), no provider or model (before 010), or no rate
for something its usage needs. A provider-reported cost counts as a price (FR-115): the decisions'
and the proxy-metered agent turns' figures join the total, summed in SQL over integer
``cost_nanos``; a subscription agent's figure is an API equivalent served beside it. Nothing priced
is ``None``, never ``"0.000000"``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from statistics import fmean
from typing import Final, Literal, overload

from harnessed import clock
from harnessed.errors import SurfaceNotFound
from harnessed.loop.journal import turn_usage
from harnessed.providers.types import Usage

__all__ = [
    "EXCLUDED_REASON",
    "HANDLER_AGENT",
    "HANDLER_CHAT_MODEL",
    "HANDLER_ROUTER",
    "KEY_KINDS",
    "CURRENCY",
    "HandlerRow",
    "KeyKind",
    "LoopRun",
    "NONE_PRICED_REASON",
    "NO_AGENT_COST_REASON",
    "NO_PRICES_REASON",
    "NO_ROUTER_COST_REASON",
    "Spend",
    "Totals",
    "TurnSeconds",
    "handler_payload",
    "key_kind_of",
    "listing",
    "run_payload",
    "spend",
    "spend_payload",
    "totals",
    "totals_payload",
    "turn_seconds",
    "turn_seconds_payload",
    "turn_spans",
    "usage_payload",
]

KeyKind = Literal["session", "case_run"]

#: The two shapes ``turns.run_id`` holds, and there is no third (decision 24).
KEY_KINDS: Final[tuple[KeyKind, ...]] = ("session", "case_run")

#: What separates the three parts of a case run key. A session id never contains it.
_CASE_RUN_SEPARATOR: Final = ":"

#: Why a turn carries no seconds: it was journaled before ``requested_at`` existed (007). The
#: one reason there is, spelled once and served beside every count it explains.
EXCLUDED_REASON: Final = "predates migration 007"

#: The one currency ``prices`` stores (decision 53). A row in any other is not a dollar figure.
CURRENCY: Final = "USD"

#: Why ``spend.total`` is ``null``, one sentence per cause, verbatim from ``ux/design.md``. The
#: tile owns the copy (``dashboard/lib/null-reasons.ts``); the CLI's prose says the same words.
NO_PRICES_REASON: Final = "no prices fetched: run `harnessed prices refresh`"
NONE_PRICED_REASON: Final = "no turn could be priced"

#: Why a handler row's ``cost`` and ``api_equivalent`` are both null (decision 64, FR-120): never
#: set unless neither carries a figure, and never a substitute for either.
NO_ROUTER_COST_REASON: Final = "no decision reported a cost"
NO_AGENT_COST_REASON: Final = "no agent turn reported a cost"

#: The three ``handlers[].kind`` values (decision 64): a chat model priced from the catalog, the
#: router priced from its own reported figure, an agent entry either kind can meter.
HANDLER_CHAT_MODEL: Final = "chat_model"
HANDLER_ROUTER: Final = "router"
HANDLER_AGENT: Final = "agent"

#: Six decimal places, and the rule for the seventh (decision 64: a reported figure as small as a
#: router decision's never reads as zero). A float never touches either.
_SIX_PLACES: Final = Decimal("0.000001")
_PER_MILLION: Final = Decimal(1_000_000)
#: Nanodollars per dollar - the unit every ``cost_nanos`` sum is in (migrations 014 and 015).
_PER_BILLION: Final = Decimal(1_000_000_000)

#: The two ``agent_turns.cost_kind`` values (015): a proxy-metered bill, which is a price, and a
#: subscription agent's ``usage_update.cost``, which is only what the same work would have cost.
_REPORTED: Final = "reported"
_API_EQUIVALENT: Final = "api_equivalent"

# The journal summed per (provider, model) in one pass, then joined to the stored USD rates. SQL
# does only what is exact on integers: counts and token sums. ``cached_input_tokens`` is part of
# ``input_tokens``, so the uncached share is summed apart; the ``bare_`` sums are the turns with no
# cached tokens, the only ones a model with no cached rate can still price. A pre-006 row is NULL
# in all three counts, so ``SUM`` skips it and ``no_usage`` counts it; a pre-010 row groups under
# a NULL provider or model and joins no price. Every rate stays the stored string (decision 53).
_SELECT_SPEND: Final = (
    "SELECT g.provider, g.model, g.held, g.no_usage, g.cached_turns, g.uncached, g.outputs, "
    "  g.cached, g.bare_inputs, g.bare_outputs, p.input, p.output, p.cached_input "
    "FROM (SELECT provider, model, COUNT(*) AS held, "
    "    SUM(input_tokens IS NULL OR output_tokens IS NULL OR cached_input_tokens IS NULL) "
    "      AS no_usage, "
    "    SUM(cached_input_tokens > 0) AS cached_turns, "
    "    SUM(input_tokens - cached_input_tokens) AS uncached, "
    "    SUM(output_tokens) AS outputs, SUM(cached_input_tokens) AS cached, "
    "    SUM(CASE WHEN cached_input_tokens = 0 THEN input_tokens END) AS bare_inputs, "
    "    SUM(CASE WHEN cached_input_tokens = 0 THEN output_tokens END) AS bare_outputs "
    "  FROM turns {scope}GROUP BY provider, model) g "
    "LEFT JOIN prices p "
    f"  ON p.provider = g.provider AND p.model = g.model AND p.currency = '{CURRENCY}'"
)
# A session is held when any of the three ledgers carries its key: a routed message that only
# delegated wrote decisions and agent turns and no ``turns`` row, and it still cost what it cost.
_HOLDS_RUN: Final = (
    "SELECT EXISTS (SELECT 1 FROM turns WHERE run_id = :key) "
    "OR EXISTS (SELECT 1 FROM decisions WHERE run_id = :key) "
    "OR EXISTS (SELECT 1 FROM agent_turns WHERE run_id = :key)"
)
# The reported figures, summed in SQL over the one integer column each ledger keeps
# (``cost_nanos``) on 015's covering indexes (decision 64). Nothing here reads a token count: an
# agent turn or a decision is priced by what was reported for it, or it is not priced.
_SELECT_ROUTER: Final = (
    "SELECT COUNT(*), SUM(cost_nanos), SUM(cost_nanos IS NULL) FROM decisions {scope}"
)
_SELECT_AGENTS: Final = (
    "SELECT cost_kind, COUNT(*), SUM(cost_nanos) FROM agent_turns {scope}GROUP BY cost_kind"
)
# One row per agent entry that acted, first use first (decision 64's handler order): its billing
# kind, how many turns, the reported and API-equivalent nanodollars summed apart, and how many
# turns neither counted (``cost_kind IS NULL``, summed as its own 0/1 column).
_SELECT_AGENT_ENTRIES: Final = (
    "SELECT entry, billing, COUNT(*), "
    "  SUM(CASE WHEN cost_kind = 'reported' THEN cost_nanos END), "
    "  SUM(CASE WHEN cost_kind = 'api_equivalent' THEN cost_nanos END), "
    "  SUM(cost_kind IS NULL), MIN(idx) "
    "FROM agent_turns {scope}GROUP BY entry, billing ORDER BY MIN(idx)"
)
_NEWEST_PRICE: Final = "SELECT MAX(fetched_at) FROM prices"

# When a run started: its first turn's ``requested_at``, or that turn's ``created_at`` when the
# row predates migration 007 and has none. Correlated on the outer query's ``run_id``.
_STARTED: Final = (
    "(SELECT COALESCE(first.requested_at, first.created_at) FROM turns first "
    " WHERE first.run_id = {alias}.run_id ORDER BY first.iteration LIMIT 1)"
)

# One row per key: when it started, how many turns it holds, and the terminal state - the stop
# reason of the last turn on disk. Newest first by start, then by insertion order so two runs
# started within the same clock tick still list deterministically.
_SELECT_LISTING: Final = (
    f"SELECT t.run_id, {_STARTED.format(alias='t')} AS started, MAX(t.created_at), COUNT(*), "
    "  (SELECT stop_reason FROM turns last "
    "   WHERE last.run_id = t.run_id ORDER BY last.iteration DESC LIMIT 1) "
    "FROM turns t GROUP BY t.run_id "
    "ORDER BY started DESC, MIN(t.rowid) DESC LIMIT ?"
)
# The first turn's send time alone, ``NULL`` for a run whose first turn predates 007.
_FIRST_REQUESTED: Final = (
    "(SELECT first.requested_at FROM turns first "
    " WHERE first.run_id = {alias}.run_id ORDER BY first.iteration LIMIT 1)"
)
_SELECT_ONE: Final = (
    f"SELECT {_STARTED.format(alias='turns')}, MAX(created_at), COUNT(*), "
    "  (SELECT stop_reason FROM turns last "
    "   WHERE last.run_id = turns.run_id ORDER BY last.iteration DESC LIMIT 1), "
    "  (SELECT response FROM turns last "
    "   WHERE last.run_id = turns.run_id ORDER BY last.iteration DESC LIMIT 1), "
    f"{_FIRST_REQUESTED.format(alias='turns')}, "
    "  (SELECT provider FROM turns last "
    "   WHERE last.run_id = turns.run_id ORDER BY last.iteration DESC LIMIT 1), "
    "  (SELECT model FROM turns last "
    "   WHERE last.run_id = turns.run_id ORDER BY last.iteration DESC LIMIT 1) "
    "FROM turns WHERE run_id = ?"
)
_COUNT_TOOL_CALLS: Final = "SELECT COUNT(*) FROM tool_results WHERE run_id = ?"

# The whole journal, all time (decision 47): distinct keys of both kinds and every turn row,
# then every tool row. ``tool_results`` keys into ``turns``, so its keys add no run.
_COUNT_JOURNAL: Final = "SELECT COUNT(DISTINCT run_id), COUNT(*) FROM turns"
_COUNT_ALL_TOOL_CALLS: Final = "SELECT COUNT(*) FROM tool_results"

# The count runs in SQL (decision 54): timed turns beside the rest. ``COUNT(column)`` skips
# NULLs, so the first figure is the turns that carry both timestamps and the second the pre-007
# rows that carry one.
_COUNT_TIMED: Final = "SELECT COUNT(requested_at), COUNT(*) - COUNT(requested_at) FROM turns"
# The timestamp pair of every timed turn. The subtraction happens in Python, never here.
_SELECT_TIMED_PAIRS: Final = (
    "SELECT requested_at, created_at FROM turns WHERE requested_at IS NOT NULL"
)
_SELECT_RUN_PAIRS: Final = (
    "SELECT iteration, requested_at, created_at FROM turns WHERE run_id = ? ORDER BY iteration"
)


@dataclass(frozen=True, slots=True)
class LoopRun:
    """One line of the loop-run listing: which key, which kind, when it started, how it ended.

    ``stop_reason`` is the last recorded turn's, read off the journal and never inferred; it is
    ``None`` only for a row an older build wrote without one.
    """

    run_key: str
    key_kind: KeyKind
    started_at: str
    iterations: int
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class Totals:
    """What the run journal holds, counted once, all time (FR-67).

    ``runs`` is distinct run keys of **both** key kinds: a five-case eval and one ``harnessed
    run`` count six, never one and never five (decision 24). ``counted_at`` is when the count
    was taken, because a figure that says "all time" has to say which "now" that was.
    """

    runs: int
    turns: int
    tool_calls: int
    counted_at: str


@dataclass(frozen=True, slots=True)
class TurnSeconds:
    """The mean turn, in seconds, over every turn that carries both timestamps (FR-68).

    ``average_seconds`` is already rounded to one decimal, or ``None`` when ``turns_counted``
    is zero: nothing to average is not an average of nothing. ``turns_excluded`` is the pre-007
    rows the mean could not include; they are counted so the figure says what it left out.
    """

    average_seconds: float | None
    turns_counted: int
    turns_excluded: int


@dataclass(frozen=True, slots=True)
class Spend:
    """What the journal cost, as far as the stored catalog and the reported figures can say
    (FR-74, FR-115, decisions 53 and 64).

    ``total`` is priced loop turns plus ``router_cost`` plus ``agent_cost``, already rounded to
    six places, or ``None`` when nothing is priced. Every loop turn is in exactly one of the four
    counts: priced, or one of the three null classes that are served beside the total and never
    summed into it. ``unpriced_models`` names the ``provider/model`` pairs behind
    ``unpriced_turns``, sorted.

    ``router_cost`` is the decisions' reported cost over ``router_decisions`` decisions;
    ``agent_cost`` is the proxy-metered agent turns' reported bills. ``api_equivalent`` is what
    the subscription agents' work would have cost, served **beside** the total and never in it,
    and ``not_metered_turns`` counts the agent turns nothing reported a cost for. Each money
    figure is ``None`` when no row carries one - never zero.
    """

    total: Decimal | None
    priced_turns: int
    unknown_usage_turns: int
    unknown_model_turns: int
    unpriced_turns: int
    unpriced_models: tuple[str, ...]
    prices_fetched_at: str | None
    router_cost: Decimal | None
    router_decisions: int
    agent_cost: Decimal | None
    api_equivalent: Decimal | None
    not_metered_turns: int
    handlers: tuple[HandlerRow, ...]


@dataclass(frozen=True, slots=True)
class HandlerRow:
    """One handler that acted in the session, in the store's order: chat models, the router,
    then entries by first use (decision 64, FR-115, FR-120).

    ``cost`` is a reported or catalog-priced figure; ``api_equivalent`` is what a subscription
    agent's work would have cost, served beside ``cost`` and never summed with it - a handler
    carries at most one of the two. ``reason`` explains a handler whose ``cost`` and
    ``api_equivalent`` are both ``None``: it is set only then, and it is never ``"0"``.
    """

    handler: str
    kind: str
    billing: str | None
    turns: int
    cost: Decimal | None
    api_equivalent: Decimal | None
    not_metered_turns: int
    reason: str | None


def key_kind_of(run_key: str) -> KeyKind:
    """Which of the two shapes ``run_key`` is. A colon anywhere makes it a case run key."""
    return "case_run" if _CASE_RUN_SEPARATOR in run_key else "session"


def listing(conn: sqlite3.Connection, limit: int = 20) -> tuple[LoopRun, ...]:
    """At most ``limit`` loop runs, newest first, each labelled with its key kind.

    Distinct from ``evals/runner.py::recent``, which lists eval runs: the ids the two return
    never overlap, because an eval run id is never a journal key - its case run keys are.
    An empty journal lists nothing.
    """
    return tuple(
        LoopRun(
            run_key=str(row[0]),
            key_kind=key_kind_of(str(row[0])),
            started_at=str(row[1]),
            iterations=int(row[3]),
            stop_reason=None if row[4] is None else str(row[4]),
        )
        for row in conn.execute(_SELECT_LISTING, (limit,))
    )


def run_payload(conn: sqlite3.Connection, run_key: str) -> dict[str, object]:
    """The payload ``harnessed run --json`` prints for one loop run, built from the journal.

    The same dict the gateway returns for ``GET /v1/runs/{run_key}``, so the two cannot disagree.
    Every field is read off the journal's typed columns: a run the CLI has just finished and a
    run the gateway is asked about a week later describe themselves the same way.

    ``started_at`` is when the first turn's request was sent and ``finished_at`` when the last
    turn was recorded, for both key kinds. The retrieval gate's own call precedes the first
    request, so it falls outside that span: the duration is the loop's, not the whole command's.
    A first turn recorded before migration 007 has no send time, and ``started_at`` falls back to
    that turn's ``created_at`` - never ``null``.

    ``usage`` is the three token counts summed over every turn, read from the journal rather
    than recomputed. It is ``None`` when any turn predates migration 006: that turn's cost is
    unknown, and a partial sum would understate the run.

    ``provider`` and ``model`` are the last turn's, the same turn ``reply`` and ``stop_reason``
    come from: who produced the answer this payload shows (FR-72). Both are ``None`` for a turn
    recorded before migration 010, and a reader renders that ``unknown``, never a default name.

    Raises :class:`~harnessed.errors.SurfaceNotFound` when the journal holds nothing for
    ``run_key``. An empty payload would read as a run that did nothing, which is a different
    claim from a run that never happened.
    """
    row = conn.execute(_SELECT_ONE, (run_key,)).fetchone()
    if row is None or int(row[2]) == 0:
        raise SurfaceNotFound(
            f"no loop run under {run_key!r}: the run journal holds no turn for that "
            f"{key_kind_of(run_key).replace('_', ' ')} key."
        )
    started_at, finished_at, iterations, stop_reason, reply, first_requested, provider, model = row
    tool_calls = conn.execute(_COUNT_TOOL_CALLS, (run_key,)).fetchone()
    duration = _seconds_between(first_requested, finished_at)
    return {
        "run_key": run_key,
        "key_kind": key_kind_of(run_key),
        "started_at": str(started_at),
        "finished_at": str(finished_at),
        "iterations": int(iterations),
        "tool_calls": 0 if tool_calls is None else int(tool_calls[0]),
        "stop_reason": None if stop_reason is None else str(stop_reason),
        "reply": str(reply),
        "usage": _usage_total(conn, run_key),
        # The loop's span: first request sent to last turn recorded, one decimal, a string.
        # A first turn from before 007 has no send time, and the reason travels with the null.
        "duration_seconds": duration,
        "duration_reason": EXCLUDED_REASON if duration is None else None,
        "provider": None if provider is None else str(provider),
        "model": None if model is None else str(model),
    }


def totals(conn: sqlite3.Connection) -> Totals:
    """Loop runs, turns and tool calls over the whole journal - the store read behind
    ``GET /v1/runs/totals`` and ``harnessed runs totals`` (decision 41).

    An empty journal counts to zero, not to nothing: the tables exist and hold no rows, and
    that is a measurement. Nothing is windowed and nothing is cached (decision 47).
    """
    runs_held, turns_held = conn.execute(_COUNT_JOURNAL).fetchone()
    (tool_calls_held,) = conn.execute(_COUNT_ALL_TOOL_CALLS).fetchone()
    return Totals(
        runs=int(runs_held),
        turns=int(turns_held),
        tool_calls=int(tool_calls_held),
        counted_at=clock.isoformat(clock.now()),
    )


def totals_payload(found: Totals) -> dict[str, object]:
    """The one wire shape for :func:`totals`, printed by the CLI and served by the gateway.

    Every count is a **string** (MUST rule 29): the tile renders the characters as sent and
    derives nothing. ``key_kinds`` says out loud that ``runs`` spans both id shapes.
    """
    return {
        "runs": str(found.runs),
        "turns": str(found.turns),
        "tool_calls": str(found.tool_calls),
        "key_kinds": list(KEY_KINDS),
        "counted_at": found.counted_at,
    }


def turn_seconds(conn: sqlite3.Connection) -> TurnSeconds:
    """The mean turn in seconds over the whole journal - the store read behind
    ``GET /v1/runs/turn_seconds`` and ``harnessed runs turn-seconds`` (decision 54).

    The count is SQL's; the subtraction is ``clock.parse``'s, pair by pair, so no figure rests
    on how this SQLite build reads a ``Z`` suffix. The mean is rounded here, to one decimal,
    and is ``None`` when no turn carries both timestamps.
    """
    counted, excluded = conn.execute(_COUNT_TIMED).fetchone()
    seconds = [
        _elapsed(str(requested_at), str(created_at))
        for requested_at, created_at in conn.execute(_SELECT_TIMED_PAIRS)
    ]
    return TurnSeconds(
        average_seconds=round(fmean(seconds), 1) if seconds else None,
        turns_counted=int(counted),
        turns_excluded=int(excluded),
    )


def turn_seconds_payload(found: TurnSeconds) -> dict[str, object]:
    """The one wire shape for :func:`turn_seconds`, printed by the CLI and served by the gateway.

    The mean and both counts cross as **strings** (MUST rule 29); nothing to average crosses as
    ``null``, never ``"0.0"``. ``excluded_reason`` is always present, so the tile can name what
    the count left out without knowing the migration history itself.
    """
    return {
        "average_seconds": _one_decimal(found.average_seconds),
        "turns_counted": str(found.turns_counted),
        "turns_excluded": str(found.turns_excluded),
        "excluded_reason": EXCLUDED_REASON,
    }


def spend(conn: sqlite3.Connection, session: str | None = None) -> Spend:
    """What the journal cost in USD - the store read behind ``GET /v1/spend`` and ``harnessed
    spend`` (decision 41). ``session`` scopes the same read to one run key.

    A provider-reported cost counts as a price (FR-115): a decision's and a proxy-metered agent
    turn's reported figure is summed in integer nanodollars and joins the total; a subscription
    agent's figure is an API equivalent served beside it; an agent turn with no figure is counted
    as not metered. **0** of them is priced from a rate, whatever the catalog holds for its model.

    SQL counts and adds integer token counts per model; Python sorts each group into its class and
    multiplies each sum by its stored rate as a ``Decimal``, which is exact because multiplication
    distributes over the sum. A turn is priced only when the catalog gives every rate its usage
    needs: ``input`` and ``output`` always, ``cached_input`` when ``cached_input_tokens > 0``.
    ``cached_input_tokens`` is a breakdown of ``input_tokens`` (``providers/types.py::Usage``), so
    the cached share is charged at the cached rate and the rest at the input rate. A missing rate
    makes the turn unpriced; nothing is ever substituted for it (MUST rule 28).

    Raises :class:`~harnessed.errors.SurfaceNotFound` when ``session`` names a key the journal
    does not hold: a run that never happened did not cost nothing.
    """
    scope = ""
    params: tuple[str, ...] = ()
    if session is not None:
        (held,) = conn.execute(_HOLDS_RUN, {"key": session}).fetchone()
        if not held:
            raise SurfaceNotFound(
                f"no loop run under {session!r}: the run journal holds no turn for that "
                "session key, so it has no spend."
            )
        scope, params = "WHERE run_id = ? ", (session,)
    (newest,) = conn.execute(_NEWEST_PRICE).fetchone()
    rows = conn.execute(_SELECT_SPEND.format(scope=scope), params).fetchall()
    total = Decimal(0)
    priced = unknown_usage = unknown_model = unpriced = 0
    unpriced_models: set[str] = set()
    # Decision 64's chat-model handler rows: one per provider, in the order ``rows`` groups them,
    # accumulated beside the totals above rather than re-queried.
    chat_turns: dict[str, int] = {}
    chat_priced_turns: dict[str, int] = {}
    chat_amount: dict[str, Decimal] = {}
    for (
        provider,
        model,
        held,
        no_usage,
        cached_turns,
        *sums,
        input_rate,
        output_rate,
        cached_rate,
    ) in rows:
        uncached, outputs, cached, bare_inputs, bare_outputs = (_tokens(value) for value in sums)
        unknown_usage += int(no_usage)
        known = int(held) - int(no_usage)
        if provider is not None:
            chat_turns[provider] = chat_turns.get(provider, 0) + int(held)
        if known == 0:
            continue
        if provider is None or model is None:
            unknown_model += known
            continue
        if input_rate is None:
            unpriced += known
            unpriced_models.add(f"{provider}/{model}")
            continue
        if cached_rate is None and int(cached_turns) > 0:
            # No cached rate: the turns that need one are unpriced, never charged a substitute.
            unpriced += int(cached_turns)
            unpriced_models.add(f"{provider}/{model}")
            priced += known - int(cached_turns)
            chat_priced_turns[provider] = (
                chat_priced_turns.get(provider, 0) + known - int(cached_turns)
            )
            amount = bare_inputs * Decimal(str(input_rate)) + bare_outputs * Decimal(
                str(output_rate)
            )
            total += amount
            chat_amount[provider] = chat_amount.get(provider, Decimal(0)) + amount
            continue
        priced += known
        chat_priced_turns[provider] = chat_priced_turns.get(provider, 0) + known
        amount = uncached * Decimal(str(input_rate)) + outputs * Decimal(str(output_rate))
        if cached > 0:
            amount += cached * Decimal(str(cached_rate))
        total += amount
        chat_amount[provider] = chat_amount.get(provider, Decimal(0)) + amount
    decisions, router_nanos, router_not_metered = conn.execute(
        _SELECT_ROUTER.format(scope=scope), params
    ).fetchone()
    agent_entries = conn.execute(_SELECT_AGENT_ENTRIES.format(scope=scope), params).fetchall()
    agent_nanos: dict[str | None, object] = {}
    not_metered = 0
    for kind, held_turns, nanos in conn.execute(_SELECT_AGENTS.format(scope=scope), params):
        agent_nanos[kind] = nanos
        if kind is None:
            not_metered = int(held_turns)
    router_cost = _dollars(router_nanos)
    agent_cost = _dollars(agent_nanos.get(_REPORTED))
    total /= _PER_MILLION
    for reported in (router_cost, agent_cost):
        if reported is not None:
            total += reported
    anything_priced = priced > 0 or router_cost is not None or agent_cost is not None
    handlers: list[HandlerRow] = []
    for provider, turns_count in chat_turns.items():
        cost = _rounded(chat_amount[provider] / _PER_MILLION) if provider in chat_amount else None
        handlers.append(
            HandlerRow(
                handler=provider,
                kind=HANDLER_CHAT_MODEL,
                billing=None,
                turns=turns_count,
                cost=cost,
                api_equivalent=None,
                not_metered_turns=turns_count - chat_priced_turns.get(provider, 0),
                reason=None
                if cost is not None
                else (NO_PRICES_REASON if newest is None else NONE_PRICED_REASON),
            )
        )
    router_handler_cost = _rounded(router_cost)
    if int(decisions) > 0:
        handlers.append(
            HandlerRow(
                handler="router",
                kind=HANDLER_ROUTER,
                billing=None,
                turns=int(decisions),
                cost=router_handler_cost,
                api_equivalent=None,
                not_metered_turns=int(router_not_metered),
                reason=None if router_handler_cost is not None else NO_ROUTER_COST_REASON,
            )
        )
    for (
        entry,
        billing,
        held_turns,
        reported_nanos,
        api_nanos,
        entry_not_metered,
        _idx,
    ) in agent_entries:
        entry_cost = _rounded(_dollars(reported_nanos))
        entry_api_equivalent = _rounded(_dollars(api_nanos))
        handlers.append(
            HandlerRow(
                handler=str(entry),
                kind=HANDLER_AGENT,
                billing=None if billing is None else str(billing),
                turns=int(held_turns),
                cost=entry_cost,
                api_equivalent=entry_api_equivalent,
                not_metered_turns=int(entry_not_metered),
                reason=(
                    None
                    if entry_cost is not None or entry_api_equivalent is not None
                    else NO_AGENT_COST_REASON
                ),
            )
        )
    return Spend(
        total=_rounded(total) if anything_priced else None,
        priced_turns=priced,
        unknown_usage_turns=unknown_usage,
        unknown_model_turns=unknown_model,
        unpriced_turns=unpriced,
        unpriced_models=tuple(sorted(unpriced_models)),
        prices_fetched_at=None if newest is None else str(newest),
        router_cost=_rounded(router_cost),
        router_decisions=int(decisions),
        agent_cost=_rounded(agent_cost),
        api_equivalent=_rounded(_dollars(agent_nanos.get(_API_EQUIVALENT))),
        not_metered_turns=not_metered,
        handlers=tuple(handlers),
    )


def spend_payload(found: Spend) -> dict[str, object]:
    """The one wire shape for :func:`spend`, printed by the CLI and served by the gateway.

    ``total`` crosses as the six-decimal string the store rounded, or ``null`` - never
    ``"0.000000"`` for nothing priced; so does every v5 money field (decision 64). The counts
    cross as strings (MUST rule 29) and sit beside ``total`` so the tile can tell a run that cost
    nothing from a run whose cost is unknown.
    """
    return {
        "total": _money(found.total),
        "currency": CURRENCY,
        "priced_turns": str(found.priced_turns),
        "unknown_usage_turns": str(found.unknown_usage_turns),
        "unknown_model_turns": str(found.unknown_model_turns),
        "unpriced_turns": str(found.unpriced_turns),
        "unpriced_models": list(found.unpriced_models),
        "prices_fetched_at": found.prices_fetched_at,
        "router_cost": _money(found.router_cost),
        "router_decisions": str(found.router_decisions),
        "agent_cost": _money(found.agent_cost),
        "api_equivalent": _money(found.api_equivalent),
        "not_metered_turns": str(found.not_metered_turns),
        "handlers": [handler_payload(row) for row in found.handlers],
    }


def handler_payload(row: HandlerRow) -> dict[str, object]:
    """One ``handlers[]`` element: every figure a string, ``reason`` set only when ``cost`` and
    ``api_equivalent`` are both ``None`` (decision 64, FR-120)."""
    return {
        "handler": row.handler,
        "kind": row.kind,
        "billing": row.billing,
        "turns": str(row.turns),
        "cost": _money(row.cost),
        "api_equivalent": _money(row.api_equivalent),
        "not_metered_turns": str(row.not_metered_turns),
        "reason": row.reason,
    }


def turn_spans(conn: sqlite3.Connection, run_key: str) -> dict[int, str | None]:
    """Each of one run's turns in seconds, one decimal, as a string; ``None`` for a pre-007 row.

    The per-iteration half of decision 54, beside ``journal.turn_usage``: the trace projects
    both onto its objects and computes neither. Keyed by iteration like ``turn_usage`` is.
    """
    return {
        int(iteration): _seconds_between(requested_at, created_at)
        for iteration, requested_at, created_at in conn.execute(_SELECT_RUN_PAIRS, (run_key,))
    }


def usage_payload(usage: Usage | None) -> dict[str, int] | None:
    """The three counts as the wire spells them, or ``None`` for a turn whose cost is unknown."""
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
    }


@overload
def _rounded(amount: Decimal) -> Decimal: ...
@overload
def _rounded(amount: None) -> None: ...
def _rounded(amount: Decimal | None) -> Decimal | None:
    """Six places, half-up, or ``None`` left as the state it is (decision 64)."""
    return None if amount is None else amount.quantize(_SIX_PLACES, ROUND_HALF_UP)


def _dollars(nanos: object) -> Decimal | None:
    """A ``SUM(cost_nanos)`` as exact dollars, or ``None`` where no row carried a figure."""
    return None if nanos is None else Decimal(int(str(nanos))) / _PER_BILLION


def _money(amount: Decimal | None) -> str | None:
    return None if amount is None else str(amount)


def _tokens(summed: object) -> int:
    """One summed token count; ``SUM`` over no rows is NULL, which is zero tokens, not unknown."""
    return 0 if summed is None else int(str(summed))


def _seconds_between(requested_at: object, created_at: object) -> str | None:
    """One span as the wire carries it: one decimal, a string, or ``None`` without a send time."""
    if requested_at is None or created_at is None:
        return None
    return _one_decimal(_elapsed(str(requested_at), str(created_at)))


def _elapsed(requested_at: str, created_at: str) -> float:
    """``created_at - requested_at`` in seconds, through ``clock.parse`` and nothing else."""
    return (clock.parse(created_at) - clock.parse(requested_at)).total_seconds()


def _one_decimal(seconds: float | None) -> str | None:
    """The characters the browser renders: rounded here, never by the tile (MUST rule 29)."""
    return None if seconds is None else f"{round(seconds, 1):.1f}"


def _usage_total(conn: sqlite3.Connection, run_key: str) -> dict[str, int] | None:
    """The run's token counts summed over its turns, or ``None`` if any turn's are unknown."""
    per_turn = turn_usage(conn, run_key).values()
    if any(usage is None for usage in per_turn):
        return None
    known = [usage for usage in per_turn if usage is not None]
    return {
        "input_tokens": sum(usage.input_tokens for usage in known),
        "output_tokens": sum(usage.output_tokens for usage in known),
        "cached_input_tokens": sum(usage.cached_input_tokens for usage in known),
    }
