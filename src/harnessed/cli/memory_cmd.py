"""``harnessed memory`` - read what the harness remembers, and write down a fact by hand.

Three stores, three ways to read them, and one way to write. Each read takes ``--json`` and then
prints the dataclass projection of exactly what the store returned, wrapped in one collection key
(``facts``, ``procedures``, ``episodes``): field names as the wire carries them, an empty list for
an empty store, and nothing the gateway's twin route would have to reproduce (FR-35).

The write exists because ``semantic.put`` had no caller anywhere in ``src/``: the table, the FTS5
index and ``harnessed memory search`` all shipped, and nothing an operator could type put a fact
into them, which left one of retrieval's three sources permanently empty.

Facts are the only kind written here, and that is deliberate. A procedure arrives by promotion,
because FR-20 says no model and no hand decides one outside the gate; an episode is written by a
finished run, because a record of what happened is not something to type. Neither belongs on a
command line.

The whole group opens the database directly rather than through
:func:`~harnessed.cli.run_cmd.open_session`, because remembering a fact has no business
constructing a provider - and constructing one is what would make these commands fail on a
checkout configured for a live model with no key set.
"""

from __future__ import annotations

import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated, Any, Final

import typer
import yaml  # type: ignore[import-untyped]

from harnessed import clock, config
from harnessed.cli import render
from harnessed.errors import SuiteLoadError, SurfaceNotFound
from harnessed.evals import runner as eval_runner
from harnessed.evals.graders import Answer, grade
from harnessed.evals.scoring import mean_score
from harnessed.evals.suite import GraderSpec
from harnessed.events.bus import EventBus
from harnessed.memory import consolidation, db, episodic, procedural, semantic, totals
from harnessed.providers import build_provider, registry
from harnessed.providers.fake import FakeProvider

__all__ = [
    "JSON_HELP",
    "OPERATOR_SOURCE",
    "add",
    "archive",
    "consolidate",
    "episodes",
    "fact",
    "memory_app",
    "memory_totals",
    "procedures",
    "search",
]

#: One help string for the three read-shaped subcommands, spelled as every other command spells it.
JSON_HELP: Final = "Emit a single JSON object on stdout and nothing else."

#: The source recorded for a fact nobody named a source for. One literal, so that when something
#: starts extracting facts automatically the two kinds are already told apart in the store.
OPERATOR_SOURCE: Final = "operator"

memory_app = typer.Typer(
    name="memory",
    help="Inspect semantic, episodic and procedural memory, and write down a fact.",
    no_args_is_help=True,
)


@memory_app.callback()
def _group() -> None:
    """Keep ``memory`` a command group (see ``eval_cmd._group`` for why)."""


@memory_app.command("add")
def add(
    text: Annotated[
        str,
        typer.Argument(
            help=(
                "One fact, in plain language. Stored as written, after redaction. "
                "Adding the same sentence twice stores it twice - there is no deduplication."
            )
        ),
    ],
    source: Annotated[
        str,
        typer.Option("--source", help="Where the fact came from. Recorded with it."),
    ] = OPERATOR_SOURCE,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Label this fact. Repeatable. Narrows retrieval later."),
    ] = None,
    link: Annotated[
        list[str] | None,
        typer.Option(
            "--link",
            help="Connect this fact to another by mem_id. Repeatable. Retrieval follows it.",
        ),
    ] = None,
) -> None:
    """Write one fact into semantic memory and print the id it was stored under.

    ``--link`` is what lets retrieval find a fact that shares no words with the task. Search finds
    what matches; a link finds what a person said belongs together, which is the half keyword
    search cannot do and the half embeddings will do differently in v2.

    Publishes nothing: ``memory.write.committed`` is a finished run's own event (ticket 232), and
    this door opens the database with no session and no bus.
    """
    if not text.strip():
        render.failure(
            "a fact needs some text. An empty one is retrievable noise: it would be a "
            "candidate in every future assembly and say nothing when it got there."
        )
        raise typer.Exit(code=1)

    # Through ``semantic.put``, never a direct insert: that function is the FR-13 entry point
    # that scrubs before it writes, and a secret reaching semantic memory is replayed into
    # every future system prompt.
    fact = semantic.put(
        db.connect(), text, source=source, tags=tuple(tag or ()), links=tuple(link or ())
    )
    render.rows("1 fact written", (_describe(fact),))


def _describe(fact: semantic.Fact) -> str:
    """One fact on one line: what it says, where it came from, and what it is attached to."""
    parts = [f"{fact.mem_id}  {fact.text}  ({fact.source})"]
    if fact.tags:
        parts.append(f"tags: {', '.join(fact.tags)}")
    if fact.links:
        parts.append(f"links: {', '.join(fact.links)}")
    if fact.status != semantic.ACTIVE:
        parts.append(fact.status)
    return "  ".join(parts)


@memory_app.command("archive")
def archive(
    mem_id: Annotated[str, typer.Argument(help="The fact to retire from retrieval.")],
) -> None:
    """Retire one fact from retrieval without deleting it.

    Since the run path started assembling retrieval, a fact that has become false is not merely
    untidy: it is carried into every future prompt and charged to the token budget, forever. This
    is how it stops, and the row stays - nothing in memory is destroyed, the way a procedure is
    deactivated rather than dropped.

    Idempotent: archiving an already-archived fact reports the state it is already in.
    """
    fact = semantic.archive(db.connect(), mem_id)
    if fact is None:
        render.failure(
            f"no fact with id {mem_id!r}. `harnessed memory search` lists the ids it knows."
        )
        raise typer.Exit(code=1)
    render.rows("1 fact archived", (_describe(fact),))


@memory_app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="Words to look for in semantic memory.")],
    limit: Annotated[int, typer.Option("--limit", help="How many facts to show.")] = 10,
    archived: Annotated[
        bool, typer.Option("--archived", help="Include facts that have been retired.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Search semantic memory over FTS5 and print what matched.

    Archived facts are left out unless ``--archived`` asks for them: they are retired from
    retrieval, and a search that still offered them would be offering what no run will use.

    **Every typed word counts, and ticket 048 deliberately decided that.** Retrieval rebuilds a
    query from a *task* - prose the operator aimed at the model, not at the index - and drops the
    stopwords that would otherwise match every note. This command takes a search query, aimed at
    the index on purpose. Somebody who types ``the`` here means notes containing ``the``, and
    silently discarding their word would make the results unexplainable.

    The two therefore answer differently for the same string, and that is the correct behaviour
    rather than a drift: one is inferring what to look for, the other is being told.

    **What the query is no longer passed through into is FTS5 itself** (ticket 121). ``semantic
    .searched`` quotes each typed word as its own phrase before the index sees it, so a question
    mark or a hyphen is something to look for rather than an operator - and when the text holds
    no word at all, that is said out loud instead of read as "nothing matched".
    """
    conn = db.connect()
    answer = semantic.searched(conn, query, limit=limit, include_archived=archived)
    if as_json:
        render.json_object(semantic.json_payload(answer))
        return
    headline = f"{len(answer.facts)} fact(s) matching {query!r}"
    render.rows(
        headline if answer.note is None else f"{headline} - {answer.note}",
        tuple(_describe(fact) for fact in answer.facts),
    )


@memory_app.command("procedures")
def procedures(
    every: Annotated[
        bool, typer.Option("--all", help="Show every procedure, not just the active set.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Print the active procedure set, or every procedure ever written."""
    conn = db.connect()
    found = procedural.all(conn) if every else procedural.active(conn)
    if as_json:
        render.json_object({"procedures": [asdict(item) for item in found]})
        return
    render.rows(
        f"{len(found)} procedure(s), {'all' if every else 'active'}",
        tuple(f"{item.proc_id}  {item.text}  ({item.source})" for item in found),
    )


@memory_app.command("fact")
def fact(
    mem_id: Annotated[str, typer.Argument(help="The mem_id `memory add` or `search` printed.")],
    as_json: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Print one fact by id, archived or not (ticket 232).

    The twin of ``GET /v1/memory/facts/{mem_id}``: the store's own row, so a miss is refused here
    as the route answers 404.
    """
    found = semantic.get(db.connect(), mem_id)
    if found is None:
        raise SurfaceNotFound(f"no fact {mem_id!r}: the ledgers hold nothing under that id.")
    if as_json:
        render.json_object(asdict(found))
        return
    render.rows("1 fact", (_describe(found),))


@memory_app.command("episodes")
def episodes(
    run_id: Annotated[str, typer.Argument(help="The run whose episodes to print.")],
    as_json: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Print every episode recorded against one run.

    An id no session was ever recorded under is refused, not printed as an empty list: the
    gateway's twin answers 404 for it, and a CLI that said "nothing" where the route says "no such
    thing" would be a second source of truth (ticket 079). A session that exists and wrote no
    episode is the empty collection, as the route's 200 is.
    """
    conn = db.connect()
    found = episodic.for_session(conn, run_id)
    if found is None:
        render.failure(
            f"no session with id {run_id!r}. `harnessed run` prints the id of the session it ran."
        )
        raise typer.Exit(code=1)
    if as_json:
        render.json_object({"episodes": [asdict(item) for item in found]})
        return
    render.rows(
        f"{len(found)} episode(s) for run {run_id}",
        tuple(f"{item.episode_id}  {item.kind}  {item.text}" for item in found),
    )


@memory_app.command("totals")
def memory_totals(
    as_json: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Count what the three stores hold, all time: facts active and archived, every episode,
    and the active procedure set.

    ``--json`` prints ``memory/totals.py::totals_payload`` for ``totals.totals``, the same two
    callables ``GET /v1/memory/totals`` serves. Archived facts are counted apart from the
    active ones, never inside them. An empty store prints ``0`` four times.
    """
    payload = totals.totals_payload(totals.totals(db.connect()))
    if as_json:
        render.json_object(payload)
        return
    render.rows(
        "memory, all time",
        (
            f"facts active       {payload['facts_active']}",
            f"facts archived     {payload['facts_archived']}",
            f"episodes           {payload['episodes']}",
            f"procedures active  {payload['procedures_active']}",
        ),
    )


@memory_app.command("consolidate")
def consolidate(
    batch_size: Annotated[
        int | None,
        typer.Option(
            "--batch-size",
            min=1,
            help="Episodes in the batch. Default: HARNESSED_CONSOLIDATE_AFTER.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Print the selection and the batch id only; make 0 model calls."
        ),
    ] = False,
    list_batches: Annotated[
        bool,
        typer.Option(
            "--list", help="Print the batches, newest first, and the episodes waiting; run nothing."
        ),
    ] = False,
    quality: Annotated[
        Path | None,
        typer.Option(
            "--quality",
            help="Run the consolidation quality set at this path (decision 98); nothing else runs.",
        ),
    ] = None,
    fresh: Annotated[
        bool,
        typer.Option("--fresh", help="With --quality: score under a new run, not the latest one."),
    ] = False,
    evidence: Annotated[
        Path | None,
        typer.Option("--evidence", help="With --quality: also write the printed score here."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Run one consolidation pass by hand: distill the oldest unconsolidated episodes into facts.

    The same pass the three doors run once ``HARNESSED_CONSOLIDATE_AFTER`` episodes wait
    (``memory/consolidation.py::consolidate``), over ``--batch-size`` episodes however few are
    waiting. ``--dry-run`` prints the selection and the batch id and builds no provider.
    ``--quality`` runs the quality set instead (:func:`_run_quality`).
    """
    settings = config.load()
    if quality is not None:
        _run_quality(settings, quality, fresh=fresh, evidence=evidence, as_json=as_json)
        return
    if list_batches:
        _list_batches(settings.consolidate_after, as_json)
        return
    size = settings.consolidate_after if batch_size is None else batch_size
    conn = db.connect()
    chosen = consolidation.plan(conn, size)
    if chosen is None:
        payload: dict[str, object] = {"batch_id": None, "status": "empty", "episodes": []}
    elif dry_run:
        payload = {
            "batch_id": chosen.batch_id,
            "status": "dry_run",
            "episodes": [item.episode_id for item in chosen.episodes],
        }
    else:
        registry.credential_for(settings)
        small = settings.small_model or registry.model_for(settings)
        outcome = consolidation.consolidate(
            conn,
            provider=build_provider(settings),
            settings=replace(settings, small_model=small),
            now=clock.now(),
            size=size,
            bus_factory=lambda: EventBus(conn=conn),
        )
        assert outcome is not None
        payload = {
            "batch_id": outcome.batch_id,
            "status": outcome.status,
            "failure": outcome.failure,
            "episodes": list(outcome.episode_ids),
            "facts": [{"mem_id": item.mem_id, "text": item.text} for item in outcome.facts],
            "facts_archived": str(outcome.archived),
            "facts_skipped": str(outcome.skipped),
            "diagnostics": consolidation.diagnostics(conn, outcome.batch_id),
        }
    if as_json:
        render.json_object(payload)
        return
    episode_ids = payload["episodes"]
    assert isinstance(episode_ids, list)
    lines = [f"batch   {payload['batch_id']}", f"status  {payload['status']}"]
    lines.extend(f"episode {episode_id}" for episode_id in episode_ids)
    facts = payload.get("facts")
    if isinstance(facts, list):
        lines.extend(f"fact    {item['mem_id']}  {item['text']}" for item in facts)
    diagnostic = payload.get("diagnostics")
    if isinstance(diagnostic, dict):
        for item in diagnostic["rejected"]:
            lines.append(f"skipped candidate {item['index']}: {item['reason']}")
    render.rows(f"consolidation, {len(episode_ids)} episode(s)", tuple(lines))


def _list_batches(consolidate_after: int, as_json: bool) -> None:
    """``memory consolidate --list``: the ledger's read, the same bytes as the gateway's."""
    payload = consolidation.listing_payload(consolidation.listing(db.connect(), consolidate_after))
    if as_json:
        render.json_object(payload)
        return
    batches = payload["batches"]
    assert isinstance(batches, list)
    lines = [
        f"after   {payload['consolidate_after']}",
        f"waiting {payload['unconsolidated']}",
    ]
    for batch in batches:
        facts = batch["facts"]
        lines.append(f"{batch['batch_id']}  {batch['status']}  {len(facts)} fact(s)")
    render.rows(f"consolidation, {len(batches)} batch(es)", tuple(lines))


# ---- the quality set (ticket 297, FR-169, decision 98) ---------------------------------------

QUALITY_SUITE: Final = "consolidation-quality"


def _quality_cases(path: Path) -> list[dict[str, Any]]:
    """The set's cases, or a ``SuiteLoadError`` saying what is wrong with the file."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SuiteLoadError(f"cannot read the quality set {str(path)!r}: {exc}") from exc
    cases = document.get("cases") if isinstance(document, dict) else None
    if not isinstance(cases, list) or not cases:
        raise SuiteLoadError(f"the quality set {str(path)!r} holds no 'cases' list")
    for case in cases:
        if not isinstance(case, dict) or not all(
            case.get(key) for key in ("id", "episodes", "must_write", "must_not_write")
        ):
            raise SuiteLoadError(
                f"a case in {str(path)!r} needs an id, episodes, must_write and must_not_write"
            )
    return cases


def _quality_facts(
    case: dict[str, Any], provider: consolidation.Model, settings: config.Settings
) -> tuple[list[str], str | None]:
    """One batch over the case's episodes in a scratch home: the fact texts and a failure word."""
    with tempfile.TemporaryDirectory(prefix="harnessed-quality-") as scratch:
        conn = db.connect(Path(scratch) / db.DATABASE_FILENAME)
        try:
            for episode in case["episodes"]:
                episodic.put(conn, f"seed:{case['id']}", episode["kind"], episode["text"])
            outcome = consolidation.consolidate(
                conn,
                provider=provider,
                settings=settings,
                now=clock.now(),
                size=len(case["episodes"]),
                scope=f"{consolidation.CONSOLIDATION_SCOPE}-{case['id']}",
            )
        finally:
            conn.close()
    assert outcome is not None
    return [fact.text for fact in outcome.facts], outcome.failure


def _quality_grade(case: dict[str, Any], facts: list[str], failure: str | None) -> dict[str, Any]:
    """Grade a case's facts with ``evals/graders.py``: traits written, forbidden facts counted."""
    written = grade(
        GraderSpec("contains_all", {"values": case["must_write"]}),
        Answer(text="\n".join(facts)),
    )
    banned = [
        text
        for text in facts
        if any(
            grade(GraderSpec("regex_match", {"pattern": pattern}), Answer(text=text)).score == 1.0
            for pattern in case["must_not_write"]
        )
    ]
    clean = 1.0 if not banned and failure is None else 0.0
    return {
        "id": case["id"],
        "score": mean_score((written.score, clean)),
        "passed": written.passed and clean == 1.0,
        "facts": len(facts),
        "forbidden": len(banned),
        "failure": failure,
        "detail": written.detail,
    }


def _run_quality(
    settings: config.Settings, path: Path, *, fresh: bool, evidence: Path | None, as_json: bool
) -> None:
    """``memory consolidate --quality``: per case a scratch home, one batch, graded, recorded.

    Offline (``fake``) the provider replays the set's own ``wire/`` directory; otherwise it is the
    configured provider, which is the operator's live record. The rows land in the operator's
    ``eval_runs`` / ``eval_results`` under suite ``consolidation-quality``; a case already scored
    under the run is read back, not re-run (the eval ledger's resume rule).
    """
    try:
        cases = _quality_cases(path)
    except SuiteLoadError as exc:
        render.failure(str(exc))
        raise typer.Exit(code=1) from exc
    small = settings.small_model or registry.model_for(settings)
    settings = replace(settings, small_model=small)
    provider: consolidation.Model
    if settings.provider in registry.OFFLINE_FACTORIES:
        provider = FakeProvider(settings, root=path.parent / "wire")
    else:
        registry.credential_for(settings)
        provider = build_provider(settings)

    conn = db.connect()
    try:
        run_id, _ = eval_runner._settle_run(
            conn,
            QUALITY_SUITE,
            eval_runner.DEFAULT_LABEL,
            fresh=fresh,
            provider=settings.provider or "",
            model=small,
        )
        graded: list[dict[str, Any]] = []
        for case in cases:
            recorded = eval_runner._recorded(conn, run_id, case["id"], eval_runner.DEFAULT_ATTEMPT)
            if recorded is None:
                facts, failure = _quality_facts(case, provider, settings)
                row = _quality_grade(case, facts, failure)
                eval_runner._persist(
                    conn,
                    run_id,
                    eval_runner.CaseResult(
                        case_id=case["id"],
                        score=row["score"],
                        passed=row["passed"],
                        detail=f"facts={row['facts']};forbidden={row['forbidden']};{row['detail']}",
                        attempt=eval_runner.DEFAULT_ATTEMPT,
                    ),
                )
            else:
                counts = dict(part.split("=", 1) for part in recorded.detail.split(";")[:2])
                row = {
                    "id": case["id"],
                    "score": recorded.score,
                    "passed": recorded.passed,
                    "facts": int(counts["facts"]),
                    "forbidden": int(counts["forbidden"]),
                }
            graded.append(
                {key: row[key] for key in ("id", "score", "passed", "facts", "forbidden")}
            )
        eval_runner._finish(conn, run_id)
    finally:
        conn.close()

    payload: dict[str, Any] = {
        "suite": QUALITY_SUITE,
        "eval_run_id": run_id,
        "score": mean_score(item["score"] for item in graded),
        "forbidden": sum(item["forbidden"] for item in graded),
        "cases": graded,
    }
    lines = [
        f"{item['id']}  score {item['score']:.4f}  facts {item['facts']}  "
        f"forbidden {item['forbidden']}"
        for item in graded
    ]
    lines.append(f"score {payload['score']:.4f}  forbidden {payload['forbidden']}")
    if evidence is not None:
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if as_json:
        render.json_object(payload)
        return
    render.rows(f"{QUALITY_SUITE}, {len(graded)} case(s), run {run_id}", tuple(lines))
