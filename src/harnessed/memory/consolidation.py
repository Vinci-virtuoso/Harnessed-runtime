"""Consolidation: a batch of episodes distilled into facts by one small-model call (FR-148, FR-149).

Memory grows transcripts without ever distilling them, so a preference repeated across five
sessions stays buried in five episodes. Consolidation reads the oldest unconsolidated episodes,
asks one small model what they establish, and writes the answer as semantic facts. It lives in
``memory/`` and never inside ``loop/``: the three doors call :func:`maybe_consolidate` after a
message has finished and its run lease is released, so it adds **0** latency to the message, and
nothing under ``loop/``, ``evals/`` or ``improve/`` reaches it.

**The ledger is two tables** (migration 023). ``consolidation_batches`` holds one row per batch,
keyed by :func:`batch_id` - the same episodes always make the same id. ``mem_consolidated`` holds
one row per episode a done batch consumed, so "unconsolidated" is "has no row there".

**The call goes through the gate's seam.** One ordinary ``ChatRequest`` with scope
``_consolidate`` and ``model`` the caller's small model, so FakeProvider serves it out of
``corpus/wire/fake/_consolidate/`` and the socket guard proves **0** sockets. The provider
is injected (MUST rule 30) and restated as this module's own :class:`Model` Protocol, exactly as
the gate does. The system prompt is the static :data:`INSTRUCTION`; **no procedure ever enters the
request** - this module imports neither ``procedural`` nor ``improve``, and it writes facts only.

**Each step is its own transaction** (decision 81's order): the batch row ``running`` with its
holder, the one call, the facts written together with the archives they imply, then the episodes
marked and the batch settled ``done``. A failure before the facts writes nothing. A crash between
the last two leaves the facts under ``consolidation:<batch_id>``; the next pass finds them by that
exact source through the index, marks the episodes and settles ``done`` with **0** model calls and
**0** duplicates. A ``running`` row whose holder is a live process is skipped.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Protocol

from harnessed import clock, host, ids, proc
from harnessed.config import Settings
from harnessed.errors import ProviderError, TranslationError
from harnessed.memory import db, episodic, redact, semantic
from harnessed.providers import translate
from harnessed.providers.types import (
    ChatRequest,
    ChatResponse,
    ConsolidatedFact,
    Message,
    ResponseSchema,
    Usage,
)

__all__ = [
    "CONSOLIDATION_SCOPE",
    "EPISODE_CHARS",
    "INSTRUCTION",
    "REPLY_SCHEMA",
    "FAILURES",
    "FINISHED",
    "SOURCE_PREFIX",
    "STATUSES",
    "TIMEOUT_S",
    "TOP_K",
    "Bus",
    "Model",
    "Outcome",
    "Plan",
    "batch_id",
    "consolidate",
    "BatchRow",
    "Listing",
    "listing",
    "listing_payload",
    "diagnostics",
    "maybe_consolidate",
    "plan",
    "user_turn",
]

#: The fixture scope every consolidation request declares. FakeProvider keys selection on it.
CONSOLIDATION_SCOPE: Final = "_consolidate"

#: How long the one call may take before it is a ``timeout`` rather than a batch (decision 81).
TIMEOUT_S: Final = 60.0

#: Existing semantic hits offered to the model with their ``mem_id``.
TOP_K: Final = 10

#: Each episode is cut to this many characters before it enters the request.
EPISODE_CHARS: Final = 2000

#: The reply is a short JSON object; a ceiling this size leaves room for a reasoning model.
MAX_TOKENS: Final = 1024

#: A written fact's source is this prefix plus the batch id; a batch's facts are read by it.
SOURCE_PREFIX: Final = "consolidation:"

#: The one event a settled batch publishes, as the first and only event of its own session.
FINISHED: Final = "memory.consolidation.finished"

#: The words a batch's ``status`` column holds; ``running`` is a batch still in flight.
STATUSES: Final = ("running", "done", "failed")

#: The words a failed batch's ``failure`` column holds (migration 023's CHECK).
FAILURES: Final = ("provider_error", "timeout", "unparseable")

#: The tag every consolidated fact carries.
TAG: Final = "consolidation"

INSTRUCTION: Final = (
    "Distill numbered episodes into durable facts absent from the listed existing facts: about the "
    "operator, projects and setup, or a decision taken. Each fact is read alone. "
    "Answer in the schema given. text is one self-contained sentence. actor is "
    '"operator", or the name of the agent an episode marks as having done the work. path is the '
    "file or repository the fact is about, as the episodes write it, or null. kind is "
    "preference, convention, setup or decision. relation is new, or contradicts when the fact "
    "overturns an existing one, whose mem_id from the list below is then replaces; otherwise "
    "replaces is null. Infer general preferences across episodes, not one fact per edit. "
    "General operator "
    "preferences use actor operator and path null. A document label or description is not a "
    "file path: use null for labels such as a report or an agenda. A non-null path must be "
    "one of the admissible paths supplied below; never invent one. If the episodes establish "
    "nothing, facts is empty."
)

#: Decision 98's reply schema, strict: every key required, nullable by a type union. The same
#: object is sent to an endpoint that enforces it and checked by ``translate.consolidation_facts``
#: where none does.
REPLY_SCHEMA: Final[dict[str, Any]] = {
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

#: The wire name of :data:`REPLY_SCHEMA`.
SCHEMA_NAME: Final = "consolidation_facts"

#: The one actor that needs no agent hop to name it.
OPERATOR: Final = "operator"


class Model(Protocol):
    """What consolidation knows about a provider: one request in, one response out.

    Structurally the gate's own ``Model``, restated rather than imported so ``memory/`` reaches
    one module under ``providers/`` for its types and ``translate`` for the reply.
    """

    def complete(self, request: ChatRequest) -> ChatResponse: ...


class Bus(Protocol):
    """What consolidation knows about the bus: it publishes one frozen name with a payload."""

    def publish(self, name: str, payload: Mapping[str, object] | None = None) -> object: ...


@dataclass(frozen=True, slots=True)
class Plan:
    """What a pass would run over: the batch's id and its episodes, oldest first."""

    batch_id: str
    episodes: tuple[episodic.Episode, ...]


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a pass did. ``status`` is ``done``, ``failed`` or ``skipped`` (already done).

    ``failure`` is the word a failed batch carries, ``None`` otherwise; ``facts`` are the facts the
    batch wrote, oldest first; ``calls`` is how many model calls the pass made (0 or 1).
    """

    batch_id: str
    status: str
    episode_ids: tuple[str, ...]
    failure: str | None = None
    facts: tuple[semantic.Fact, ...] = ()
    archived: int = 0
    calls: int = 0
    skipped: int = 0


def batch_id(episode_ids: tuple[str, ...] | list[str]) -> str:
    """``bat_`` plus the first 16 hex of a sha256 over the canonical JSON of the sorted ids."""
    return ids.content_hash("bat", sorted(episode_ids))


def plan(conn: sqlite3.Connection, size: int) -> Plan | None:
    """The oldest ``size`` unconsolidated episodes and their batch id, or ``None`` with none."""
    episodes = episodic.unconsolidated(conn, size)
    if not episodes:
        return None
    return Plan(batch_id([episode.episode_id for episode in episodes]), episodes)


def _hits(
    conn: sqlite3.Connection, episodes: tuple[episodic.Episode, ...]
) -> tuple[semantic.Fact, ...]:
    """The top-:data:`TOP_K` existing active facts for the batch's own text."""
    text = " ".join(episode.text[:EPISODE_CHARS] for episode in episodes)
    return semantic.searched(conn, text, TOP_K).facts


def _work(conn: sqlite3.Connection, episodes: tuple[episodic.Episode, ...]) -> dict[str, str]:
    """For each agent-hop episode, the entries that did its work and their working directories.

    Read from ``agent_turns`` joined to ``delegations`` on the task hash, both rows the delegation
    already wrote (ticket 278): the episode itself holds only the session's text.
    """
    found: dict[str, str] = {}
    for episode in episodes:
        if episode.kind != "delegation":
            continue
        rows = conn.execute(_WORK, (episode.run_id,)).fetchall()
        if rows:
            found[episode.episode_id] = "; ".join(f"agent {row[0]}, cwd {row[1]}" for row in rows)
    return found


def _places(conn: sqlite3.Connection, episodes: tuple[episodic.Episode, ...]) -> tuple[str, ...]:
    """The folded places the batch's episodes establish (decision 99).

    A token of an episode's text matching :data:`_ABSOLUTE` or :data:`_RELATIVE_FILE`, whole; and
    the working directory of each agent hop (read as :func:`_work` reads it) with its last segment,
    the repository's name. Never the last segment of a token from text.
    """
    places: set[str] = set()
    for episode in episodes:
        text = episode.text[:EPISODE_CHARS]
        for pattern in (_ABSOLUTE, _RELATIVE_FILE):
            places.update(_folded_path(token) for token in pattern.findall(text))
        if episode.kind == "delegation":
            for row in conn.execute(_WORK, (episode.run_id,)):
                if row[1]:
                    folded = _folded_path(row[1])
                    places.update((folded, folded.rsplit("/", 1)[-1]))
    places.discard("")
    return tuple(places)


def _folded_path(token: str) -> str:
    """A path folded for matching: casefolded, slashes forward, trailing ``/.,;:`` stripped."""
    return _slashed(token).rstrip("/.,;:")


def _slashed(text: str) -> str:
    """Casefolded text with every backslash a slash, the key a path is matched by."""
    return host.fold_separators(text.casefold())


def _entries(conn: sqlite3.Connection, episodes: tuple[episodic.Episode, ...]) -> frozenset[str]:
    """The agent entry names the batch's hops name (what :func:`_work` reads, names only)."""
    return frozenset(
        str(row[0])
        for episode in episodes
        if episode.kind == "delegation"
        for row in conn.execute(_WORK, (episode.run_id,))
    )


def _admitted(fact: ConsolidatedFact, places: tuple[str, ...], actors: frozenset[str]) -> bool:
    """Whether a parsed fact passes decision 98's checks: a known actor, a real place, a path.

    A non-null ``path`` must be a place; a fact whose text mentions a file or a repository must
    carry one.
    """
    return _rejection_reason(fact, places, actors) is None


def _rejection_reason(
    fact: ConsolidatedFact, places: tuple[str, ...], actors: frozenset[str]
) -> str | None:
    """The first admission rule a candidate violates; never relax a guard to explain it."""
    if fact.actor != OPERATOR and fact.actor not in actors:
        return "unknown_actor"
    if fact.path is None:
        return "missing_path" if _MENTION.search(_PROSE_ABBREVIATION.sub("", fact.text)) else None
    return None if _folded_path(fact.path) in places else "unsupported_path"


def _tags(fact: ConsolidatedFact) -> tuple[str, ...]:
    """The tags a stored fact carries beside :data:`TAG`: its kind, its actor and its path."""
    path = () if fact.path is None else (f"path:{fact.path}",)
    return (f"kind:{fact.kind}", f"actor:{fact.actor}", *path)


def user_turn(
    episodes: tuple[episodic.Episode, ...],
    hits: tuple[semantic.Fact, ...],
    work: Mapping[str, str] | None = None,
) -> str:
    """The request's one user message: the scrubbed, cut episodes then the existing facts.

    An agent-hop episode in ``work`` is prefixed with who did it and where.
    """
    lines = ["Episodes, oldest first:"]
    for number, episode in enumerate(episodes, start=1):
        done = (work or {}).get(episode.episode_id)
        prefix = "" if done is None else f"(done by {redact.scrub(done)}) "
        lines.append(f"[{number}] {prefix}{redact.scrub(episode.text[:EPISODE_CHARS])}")
    lines.append("")
    lines.append("Existing facts:")
    if hits:
        lines.extend(f"[{fact.mem_id}] {redact.scrub(fact.text)}" for fact in hits)
    else:
        lines.append("(none)")
    return "\n".join(lines)


def _call(model: Model, request: ChatRequest) -> ChatResponse:
    """One ``complete`` with the :data:`TIMEOUT_S` ceiling; ``TimeoutError`` when it is passed."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harnessed-consolidate")
    future = pool.submit(model.complete, request)
    try:
        return future.result(timeout=TIMEOUT_S)
    except FutureTimeout as exc:
        raise TimeoutError(f"the consolidation call took longer than {TIMEOUT_S}s") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


_OPEN: Final = (
    "INSERT INTO consolidation_batches "
    "(batch_id, status, attempts, episode_count, model, started_at, session_id)"
    " VALUES (?, 'running', 1, ?, ?, ?, ?) ON CONFLICT (batch_id) DO UPDATE SET status = 'running',"
    " failure = NULL, attempts = attempts + 1, model = excluded.model,"
    " started_at = excluded.started_at, finished_at = NULL, session_id = excluded.session_id"
)
_HOLDER: Final = (
    "UPDATE consolidation_batches SET holder_pid = ?, holder_started_at = ? WHERE batch_id = ?"
)
_HELD: Final = (
    "SELECT status, holder_pid, holder_started_at FROM consolidation_batches WHERE batch_id = ?"
)
_PRICE: Final = (
    "SELECT input, output, cached_input FROM prices "
    "WHERE provider = ? AND model = ? AND currency = 'USD'"
)
_SIX_PLACES: Final = Decimal("0.000001")
_PER_MILLION: Final = Decimal(1_000_000)
_PER_BILLION: Final = Decimal(1_000_000_000)
_COST: Final = "SELECT cost FROM consolidation_batches WHERE batch_id = ?"
_ARCHIVED: Final = (
    "SELECT COUNT(*) FROM mem_fact_links WHERE reason = ? AND relation = 'contradicts'"
)
_FAIL: Final = (
    "UPDATE consolidation_batches SET status = 'failed', failure = ?, finished_at = ? "
    "WHERE batch_id = ?"
)
_DONE: Final = (
    "UPDATE consolidation_batches SET status = 'done', failure = NULL, facts_written = ?, "
    "facts_archived = ?, facts_skipped = ?, input_tokens = ?, output_tokens = ?, cost = ?, "
    "cost_nanos = ?, finished_at = ? "
    "WHERE batch_id = ?"
)
_ACTIVE_TEXTS: Final = "SELECT text FROM mem_semantic WHERE status = 'active'"
_PROCEDURE_TEXTS: Final = "SELECT text FROM procedures"
_WORK: Final = (
    "SELECT DISTINCT t.entry, d.cwd FROM agent_turns t JOIN delegations d "
    "ON d.task_hash = t.task_hash WHERE t.run_id = ? ORDER BY t.idx"
)
#: A place, decision 99: an absolute path (a drive root, or a POSIX root with two segments).
_ABSOLUTE: Final = re.compile(r"(?<![\w./\\-])(?:[A-Za-z]:[\\/]|/(?=[^\s/]+/))[^\s\"'`<>|,;()]+")
#: A place, decision 99: a relative path with a separator and a file extension on its last segment.
_RELATIVE_FILE: Final = re.compile(
    r"(?<![\w./\\:-])(?:[\w.-]+[\\/])+[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7}\b"
)
#: A fact mentions a file (``name.ext``, the extension starting with a letter) or a repository.
# Exclude complete prose abbreviations, never prefixes of filenames such as e.g.py.
_PROSE_ABBREVIATION: Final = re.compile(r"(?<![\w./\\-])(?:e\.g|i\.e)\.(?=[\s,;:!?)]|$)", re.I)
_MENTION: Final = re.compile(r"\brepositor(?:y|ies)\b|\b[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7}\b", re.I)
_MARK: Final = (
    "INSERT INTO mem_consolidated (episode_id, batch_id, consolidated_at) VALUES (?, ?, ?)"
)


def _folded(text: str) -> str:
    """Case- and whitespace-folded text, the key two facts are called the same by."""
    return " ".join(text.casefold().split())


def _fresh(
    conn: sqlite3.Connection,
    facts: tuple[ConsolidatedFact, ...],
    places: tuple[str, ...] = (),
    actors: frozenset[str] = frozenset(),
    *,
    rejected: list[dict[str, object]] | None = None,
) -> tuple[tuple[ConsolidatedFact, ...], int]:
    """The facts that are not repeats or orphans, and how many were skipped.

    A repeat equals, folded, an active fact, a procedure's text (read from the table, never
    through ``procedural``) or an earlier fact of the same reply. An orphan mentions a file or a
    repository without a ``path``, or names an unknown actor or a path that is no place of the
    batch (:func:`_admitted`).
    """
    seen = {_folded(row[0]) for row in conn.execute(_ACTIVE_TEXTS)}
    seen.update(_folded(row[0]) for row in conn.execute(_PROCEDURE_TEXTS))
    kept: list[ConsolidatedFact] = []
    for index, fact in enumerate(facts):
        key = _folded(fact.text)
        reason = "duplicate" if key in seen else _rejection_reason(fact, places, actors)
        if reason is not None:
            if rejected is not None:
                rejected.append({"index": str(index), "reason": reason, "fact": asdict(fact)})
            continue
        seen.add(key)
        kept.append(fact)
    return tuple(kept), len(facts) - len(kept)


def consolidate(
    conn: sqlite3.Connection,
    *,
    provider: Model,
    settings: Settings,
    now: datetime,
    size: int,
    after_facts: Callable[[], None] | None = None,
    bus_factory: Callable[[], Bus] | None = None,
    session_id: str | None = None,
    scope: str = CONSOLIDATION_SCOPE,
) -> Outcome | None:
    """Run one batch, and say when it settled.

    ``scope`` is the request's FakeProvider scope: ``_consolidate`` always, except the quality
    set's ``_consolidate-<case id>`` (decision 98), which is how a case finds its own reply.

    A batch that settles ``done`` or ``failed`` publishes ``memory.consolidation.finished`` on the
    bus ``bus_factory`` builds - a fresh session of its own, the event its first and only one
    (decision 17, MUST rule 38) - and never on the triggering ``session_id``, whose ``run.finished``
    has long since closed its stream. A skipped batch published its own event when it ran.
    """
    outcome = _run(
        conn,
        provider=provider,
        settings=settings,
        now=now,
        size=size,
        after_facts=after_facts,
        session_id=session_id,
        scope=scope,
    )
    if outcome is not None and outcome.status != "skipped" and bus_factory is not None:
        _announce(conn, outcome, bus_factory, session_id)
    return outcome


def _announce(
    conn: sqlite3.Connection,
    outcome: Outcome,
    bus_factory: Callable[[], Bus],
    session_id: str | None,
) -> None:
    """Publish the batch's one event, its cost as the ledger holds it (a string or ``None``)."""
    cost = conn.execute(_COST, (outcome.batch_id,)).fetchone()
    bus = bus_factory()
    bus.publish(
        FINISHED,
        {
            "batch_id": outcome.batch_id,
            "status": outcome.status,
            "episode_count": len(outcome.episode_ids),
            "facts_written": len(outcome.facts),
            "facts_archived": str(outcome.archived),
            "facts_skipped": str(outcome.skipped),
            "cost": None if cost is None else cost[0],
            "failure": outcome.failure,
            "session_id": session_id,
        },
    )


def _run(
    conn: sqlite3.Connection,
    *,
    provider: Model,
    settings: Settings,
    now: datetime,
    size: int,
    after_facts: Callable[[], None] | None,
    session_id: str | None,
    scope: str = CONSOLIDATION_SCOPE,
) -> Outcome | None:
    """Run one batch over the oldest ``size`` unconsolidated episodes. ``None`` with none to run.

    The batch row is written ``running`` before the call (MUST rule 6). A provider error, a
    timeout or an unparseable reply settles it ``failed`` with that word and writes **0** facts
    and marks **0** episodes. Otherwise each new fact is written through ``semantic.put`` with
    source ``consolidation:<batch_id>``, a contradicted fact is archived and linked from its
    replacement (never deleted), every episode is marked, and the batch settles ``done``.

    ``after_facts`` is the test seam between those two steps: called once the facts are committed
    and before the episodes are marked, so a test can crash exactly there. Facts already written
    under this batch's source mean a prior pass crashed there; they are marked and settled with
    no model call.
    """
    chosen = plan(conn, size)
    if chosen is None:
        return None
    episode_ids = tuple(episode.episode_id for episode in chosen.episodes)
    found = conn.execute(_HELD, (chosen.batch_id,)).fetchone()
    if found is not None and (found[0] == "done" or (found[0] == "running" and _held(found))):
        return Outcome(chosen.batch_id, "skipped", episode_ids)

    model = settings.small_model or settings.model or ""
    stamp = clock.isoformat(now)
    me = proc.identity(os.getpid())
    db.write(
        conn,
        (
            (_OPEN, (chosen.batch_id, len(episode_ids), model, stamp, session_id)),
            (_HOLDER, (os.getpid(), me.started_at if me else None, chosen.batch_id)),
        ),
    )

    source = f"{SOURCE_PREFIX}{chosen.batch_id}"
    earlier = semantic.by_source(conn, source)
    if earlier:
        return _settle(
            conn, chosen.batch_id, episode_ids, earlier, archived=_archived(conn, chosen.batch_id)
        )

    hits = _hits(conn, chosen.episodes)
    request = ChatRequest(
        model=model,
        system=INSTRUCTION,
        messages=(
            Message(
                role="user",
                content=(
                    user_turn(chosen.episodes, hits, _work(conn, chosen.episodes))
                    + "\nAdmissible paths (use null when none applies): "
                    + json.dumps(redact.scrub(_places(conn, chosen.episodes)))
                ),
            ),
        ),
        max_tokens=MAX_TOKENS,
        metadata={"scope": scope},
        response_schema=ResponseSchema(SCHEMA_NAME, REPLY_SCHEMA),
    )
    failure: str | None = None
    response: ChatResponse | None = None
    facts: tuple[ConsolidatedFact, ...] = ()
    try:
        response = _call(provider, request)
    except TimeoutError:
        failure = "timeout"
    except ProviderError:
        failure = "provider_error"
    if response is not None:
        try:
            facts = translate.consolidation_facts(
                response.content, known={fact.mem_id for fact in hits}
            )
        except TranslationError:
            failure = "unparseable"
    if failure is not None:
        db.write(conn, ((_FAIL, (failure, clock.isoformat(clock.now()), chosen.batch_id)),))
        return Outcome(chosen.batch_id, "failed", episode_ids, failure=failure, calls=1)

    assert response is not None
    rejected: list[dict[str, object]] = []
    facts, skipped = _fresh(
        conn,
        facts,
        _places(conn, chosen.episodes),
        _entries(conn, chosen.episodes),
        rejected=rejected,
    )
    # Record the evidence before any fact is written. A resumed attempt replaces its own record.
    diagnostic = {
        "request": {"model": model, "system": request.system, "user": request.messages[0].content},
        "response": response.content,
        "rejected": rejected,
    }
    db.write(
        conn,
        (
            (
                "INSERT INTO consolidation_diagnostics (batch_id, payload) VALUES (?, ?) "
                "ON CONFLICT (batch_id) DO UPDATE SET payload = excluded.payload",
                (chosen.batch_id, json.dumps(redact.scrub(diagnostic), ensure_ascii=False)),
            ),
        ),
    )
    written = semantic.put_replacing(
        conn,
        [(fact.text, fact.replaces) for fact in facts],
        source,
        tags=(TAG,),
        entry_tags=[_tags(fact) for fact in facts],
        reason=chosen.batch_id,
    )
    if after_facts is not None:
        after_facts()
    return _settle(
        conn,
        chosen.batch_id,
        episode_ids,
        written,
        archived=sum(1 for fact in facts if fact.replaces is not None),
        usage=(response.usage.input_tokens, response.usage.output_tokens),
        cost=_cost(conn, settings.provider, model, response.usage),
        calls=1,
        skipped=skipped,
    )


def _held(row: sqlite3.Row | tuple[object, ...]) -> bool:
    """Whether a ``running`` batch's holder is still a live process (a pid-less row is not)."""
    pid = row[1]
    return isinstance(pid, int) and proc.holds(pid, row[2] if isinstance(row[2], str) else None)


def _archived(conn: sqlite3.Connection, batch: str) -> int:
    """How many facts the batch archived, read back from its link rows."""
    return int(conn.execute(_ARCHIVED, (batch,)).fetchone()[0])


def _cost(
    conn: sqlite3.Connection, provider: str | None, model: str, usage: Usage
) -> tuple[str, int] | None:
    """A batch's cost from the ``prices`` row for ``(provider, model)``, or ``None`` unpriced.

    Priced as ``loop/runs.py::spend`` prices a turn (decision 53): per-million USD rates times the
    counts, cached tokens at the cached rate, which a row without one cannot price. Money is
    ``Decimal`` throughout; the string is rounded half-up to six places.
    """
    row = conn.execute(_PRICE, (provider, model)).fetchone()
    if row is None:
        return None
    rate_in, rate_out, rate_cached = Decimal(row[0]), Decimal(row[1]), row[2]
    cached = usage.cached_input_tokens
    if cached and rate_cached is None:
        return None
    total = (
        (usage.input_tokens - cached) * rate_in
        + cached * (Decimal(rate_cached) if rate_cached is not None else Decimal(0))
        + usage.output_tokens * rate_out
    ) / _PER_MILLION
    return (
        str(total.quantize(_SIX_PLACES, rounding=ROUND_HALF_UP)),
        int((total * _PER_BILLION).quantize(Decimal(1), rounding=ROUND_HALF_UP)),
    )


def _settle(
    conn: sqlite3.Connection,
    batch: str,
    episode_ids: tuple[str, ...],
    written: tuple[semantic.Fact, ...],
    *,
    archived: int,
    usage: tuple[int | None, int | None] = (None, None),
    calls: int = 0,
    skipped: int = 0,
    cost: tuple[str, int] | None = None,
) -> Outcome:
    """Step five: mark every episode and settle the batch ``done``, in one transaction.

    ``cost`` is the six-decimal string and the nanodollars the usage priced at, or ``None``.
    """
    finished = clock.isoformat(clock.now())
    db.write(
        conn,
        (
            *((_MARK, (episode_id, batch, finished)) for episode_id in episode_ids),
            (
                _DONE,
                (
                    len(written),
                    archived,
                    skipped,
                    usage[0],
                    usage[1],
                    None if cost is None else cost[0],
                    None if cost is None else cost[1],
                    finished,
                    batch,
                ),
            ),
        ),
    )
    return Outcome(
        batch,
        "done",
        episode_ids,
        facts=written,
        archived=archived,
        calls=calls,
        skipped=skipped,
    )


def maybe_consolidate(
    conn: sqlite3.Connection,
    *,
    provider: Model,
    settings: Settings,
    bus_factory: Callable[[], Bus],
    now: datetime,
    session_id: str | None = None,
) -> Outcome | None:
    """The trigger the three doors call after a message: one batch once enough episodes wait.

    Unconsolidated episodes reaching ``settings.consolidate_after`` run one batch over the oldest
    ``consolidate_after``; below it nothing runs and nothing is read beyond a count. The batch's
    own event publishes on a session of its own; ``session_id`` is the message that triggered it.
    """
    if episodic.count_unconsolidated(conn) < settings.consolidate_after:
        return None
    return consolidate(
        conn,
        provider=provider,
        settings=settings,
        now=now,
        size=settings.consolidate_after,
        bus_factory=bus_factory,
        session_id=session_id,
    )


#: Each served column of ``consolidation_batches``, in the order the read serves them. The holder's
#: pid and start time are the ledger's own bookkeeping and are never served.
_COLUMNS: Final = (
    "batch_id",
    "session_id",
    "status",
    "failure",
    "attempts",
    "episode_count",
    "facts_written",
    "facts_archived",
    "facts_skipped",
    "model",
    "input_tokens",
    "output_tokens",
    "cost",
    "cost_nanos",
    "started_at",
    "finished_at",
)

#: Why a column is ``null``, served beside it as ``<column>_reason`` and ``null`` when it holds a
#: value. A batch row never lacks a column for any other cause.
_REASONS: Final = {
    "session_id": "run by hand, not triggered by a session",
    "failure": "the batch did not fail",
    "facts_skipped": "counted from v7 on",
    "model": "no model was recorded",
    "input_tokens": "no call finished, or the provider reported none",
    "output_tokens": "no call finished, or the provider reported none",
    "cost": "the provider reported no cost",
    "cost_nanos": "the provider reported no cost",
    "finished_at": "the batch is still running",
}

_BATCHES: Final = (
    f"SELECT {', '.join(_COLUMNS)} FROM consolidation_batches "
    "ORDER BY started_at DESC, rowid DESC"
)


@dataclass(frozen=True, slots=True)
class BatchRow:
    """One batch as the ledger holds it, every column a string or ``None``, plus its facts."""

    columns: dict[str, str | None]
    facts: tuple[tuple[str, str], ...]
    diagnostics: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Listing:
    """The read: the trigger's threshold, the waiting count and every batch, newest first."""

    consolidate_after: int
    unconsolidated: int
    batches: tuple[BatchRow, ...]


def diagnostics(conn: sqlite3.Connection, batch: str) -> dict[str, Any] | None:
    """Redacted model evidence, or None for batches predating diagnostic capture."""
    row = conn.execute(
        "SELECT payload FROM consolidation_diagnostics WHERE batch_id = ?", (batch,)
    ).fetchone()
    return None if row is None else json.loads(row[0])


def listing(conn: sqlite3.Connection, consolidate_after: int) -> Listing:
    """Every batch newest first, each with the facts written under its exact source.

    A failed batch wrote none, so its ``facts`` is empty; a running one may hold none yet.
    """
    rows = []
    for row in conn.execute(_BATCHES).fetchall():
        columns = {
            name: None if value is None else str(value)
            for name, value in zip(_COLUMNS, row, strict=False)
        }
        facts = tuple(
            (fact.mem_id, fact.text)
            for fact in semantic.by_source(conn, f"{SOURCE_PREFIX}{columns['batch_id']}")
        )
        rows.append(BatchRow(columns, facts, diagnostics(conn, str(columns["batch_id"]))))
    return Listing(consolidate_after, episodic.count_unconsolidated(conn), tuple(rows))


def listing_payload(found: Listing) -> dict[str, object]:
    """The one wire shape of :func:`listing`, printed by the CLI and served by the gateway.

    Every figure is a **string** (MUST rule 29); a ``null`` column carries its reason beside it.
    """
    batches: list[dict[str, object]] = []
    for row in found.batches:
        served: dict[str, object] = {}
        for name in _COLUMNS:
            served[name] = row.columns[name]
            if name in _REASONS:
                served[f"{name}_reason"] = _REASONS[name] if row.columns[name] is None else None
        served["facts"] = [{"mem_id": mem_id, "text": text} for mem_id, text in row.facts]
        served["diagnostics"] = row.diagnostics
        batches.append(served)
    return {
        "consolidate_after": str(found.consolidate_after),
        "unconsolidated": str(found.unconsolidated),
        "batches": batches,
    }
