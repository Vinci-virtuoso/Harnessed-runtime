"""``harnessed eval run <suite>`` - score every case of a suite and report what it scored.

Three lines of composition and one call into ``evals/runner.py``. The runner owns resumption,
the ledger and the arithmetic; this module owns the flags and hands what comes back to
``render``. The suite is loaded before the session is opened so a mistyped name fails before a
database is created for it.
"""

from __future__ import annotations

import dataclasses
import itertools
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from harnessed import config
from harnessed.acp.config import DEFAULT_PATH as AGENTS_DEFAULT_PATH
from harnessed.acp.config import AgentSpec, load_agents
from harnessed.cli import render, routed
from harnessed.cli.evidence import Capture
from harnessed.cli.run_cmd import Session, open_session
from harnessed.errors import (
    ConfigError,
    DecisionDropped,
    DelegationError,
    EntryMissing,
    EvidenceIncomplete,
    SuiteLoadError,
    SurfaceNotFound,
)
from harnessed.evals import record as recording
from harnessed.evals import runner
from harnessed.evals import suite as suite_store
from harnessed.evals.suite import KIND_MEMORY, KIND_WORK, Case, load_named_suite, seed_episodes
from harnessed.loop.engine import LoopResult
from harnessed.loop.prompt import build_user_turn
from harnessed.memory import db, retrieval
from harnessed.providers import registry
from harnessed.providers.fake_decider import FakeDecider
from harnessed.providers.jev import Decider, build_decider
from harnessed.providers.transport import RetryingDecider, RetryingProvider
from harnessed.providers.types import ChatRequest, Message
from harnessed.router import route as route_module
from harnessed.router import suite as routing
from harnessed.router.questions import FIXED_ROUTE_OPTIONS, route_options

__all__ = ["compare_runs", "eval_app", "run"]

eval_app = typer.Typer(
    name="eval",
    help="Score an eval suite against the configured provider.",
    no_args_is_help=True,
)


@eval_app.callback()
def _group() -> None:
    """Keep ``eval`` a command group.

    Typer collapses a single-command app into a bare command, which would make
    ``harnessed eval run smoke`` an unexpected extra argument (ticket 001's lesson, applied to a
    sub-app). The callback pins the group shape.
    """


@eval_app.command("run")
def run(
    suite: Annotated[str, typer.Argument(help="The suite name under corpus/suites.")],
    fresh: Annotated[
        bool,
        typer.Option("--fresh", help="Start a new run instead of resuming the most recent one."),
    ] = False,
    label: Annotated[
        str, typer.Option("--label", help="What to file this run under in the ledger.")
    ] = runner.DEFAULT_LABEL,
    samples: Annotated[
        int,
        typer.Option(
            "--samples",
            help=(
                "Draw every case this many times and report the mean. One is the default and is "
                "exactly the truth against the replaying provider; against a live model one draw "
                "is a coin, and the promoter will refuse to compare it."
            ),
        ),
    ] = 1,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            help="Re-open this eval run (from `eval list`) and score only the cases it has no "
            "row for.",
        ),
    ] = None,
    evidence: Annotated[
        Path | None,
        typer.Option(
            "--evidence",
            help="Write the output here, whole: the file is replaced only by a run that scored "
            "every case, otherwise it is left as it was and the partial goes to <path>.incomplete.",
        ),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            config.PROVIDER_FLAG,
            help="Use this provider for this run only. Beats HARNESSED_PROVIDER and .env.",
        ),
    ] = None,
) -> None:
    """Score every case of a suite, resuming from the ledger unless --fresh is given.

    ``routing`` is the router's suite (FR-118), not an eval suite: it is scored by
    :mod:`harnessed.router.suite`, files nothing in the eval ledger, and so is always fresh.

    A live run retries a dropped connection at most twice (ticket 231, FR-160) and leaves a
    case that still fails unscored rather than voiding the run.
    """
    try:
        with Capture(evidence) as capture:
            failed, complete = _score(
                suite,
                fresh=fresh,
                label=label,
                samples=samples,
                as_json=as_json,
                resume=resume,
                provider=provider,
            )
            capture.complete = complete
    except EvidenceIncomplete as exc:
        render.failure(str(exc))
        raise typer.Exit(code=1) from exc
    if failed:
        raise typer.Exit(code=1)


def live_session(session: Session, log: runner.RetryLog) -> Session:
    """The session a live ``eval run`` drives: a client with no retries of its own, wrapped in
    the one bounded, recorded retry (ticket 231). The offline provider is returned untouched, and
    nothing outside ``eval run`` calls this, so a routed console message is not wrapped."""
    if session.provider_name in registry.OFFLINE_FACTORIES:
        return session
    client = registry.create(session.settings, max_retries=0)
    return dataclasses.replace(session, provider=RetryingProvider(client, on_retry=log.on_retry))


def pinned_serve(session: Session) -> runner.Serve:
    """A work suite's composition (ticket 236, decision 83): each case through
    :func:`routed.dispatch` with the verdict pinned from its recorded handler, never decided.

    The session is the one ``eval run`` opened without ``routed``, so it holds no Decider and
    FR-119's spy stays at 0. A Groq handler is the loop on the case's own request; an agent handler
    delegates to its installed entry, then one Groq synthesis pass over the result is the reply.
    A hop that fails is a failed case reply, not a failed run.
    """
    agents = _agents()
    served = itertools.count(1)

    def serve(case: Case, request: ChatRequest, *, run_key: str) -> runner.Served:
        verdict = routed.dispatch(
            session.conn,
            None,
            case.prompt,
            announced=None,
            model=session.model,
            pinned=route_module.Verdict.pinned(case.handler),
        )
        if verdict.handler != "agent":
            return runner.Served(text=session.solve(request, run_key=run_key).reply)
        assert verdict.route is not None
        spec = agents.get(verdict.route)
        if spec is None:
            raise EntryMissing(
                f"case {case.id!r} is pinned to the agent {verdict.route!r}, which is not installed"
            )
        try:
            delegation = routed.run_agent(
                spec,
                routed.build_task(case.prompt),
                routed.build_acceptance(),
                home=session.settings.home,
                bus=session.bus,
                store=session.conn,
                # One message per case, so each case's outputs key apart on the one session.
                message_seq=next(served),
                hop=0,
                openrouter_key=session.settings.api_key_for("openrouter"),
            )
        except (DelegationError, OSError):
            return runner.Served(failed=True)
        handed = retrieval.handed(
            delegation.summary, budget=session.settings.retrieval_token_budget
        )
        synthesis = dataclasses.replace(
            request,
            system=session.synthesis_system(),
            messages=(Message(role="user", content=build_user_turn(handed.text, case.prompt)),),
        )
        return runner.Served(text=session.solve(synthesis, run_key=run_key).reply)

    return serve


def with_memory(session: Session) -> runner.Solve:
    """A memory suite's solve (ticket 249, FR-152): the case's request with the facts retrieved
    for its prompt in the user turn, as ``harnessed run`` sends them, then the session's loop.

    The retrieval is the facts alone: procedures ride the system prompt already and the seeded
    episodes belong to no run of this one's. Before a consolidation batch the store holds no fact,
    the context is empty and the request is the bare prompt; after it, the batch's facts ride.
    """

    def solve(request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        prompt = request.messages[-1].content
        assembled = retrieval.assemble(
            session.conn,
            prompt,
            budget=session.settings.retrieval_token_budget,
            kinds=("fact",),
        )
        turn = Message(role="user", content=build_user_turn(assembled.text, prompt))
        return session.solve(dataclasses.replace(request, messages=(turn,)), run_key=run_key)

    return solve


def _agents() -> Mapping[str, AgentSpec]:
    """The installed entries, or none for a missing or malformed registry (as ``harnessed run``)."""
    try:
        return load_agents(AGENTS_DEFAULT_PATH)
    except ConfigError:
        return {}


def _score(
    suite: str,
    *,
    fresh: bool,
    label: str,
    samples: int,
    as_json: bool,
    resume: str | None,
    provider: str | None = None,
) -> tuple[bool, bool]:
    """Run and print one suite; answer ``(failed, complete)``: whether the command exits 1 on its
    score, and whether every case was scored."""
    if suite == routing.SUITE_NAME:
        return _run_routing(as_json=as_json)
    loaded = load_named_suite(suite)
    log = runner.RetryLog()
    session = live_session(open_session(provider=provider), log)
    solve: runner.Solve = session.solve
    if loaded.kind == KIND_MEMORY:
        seed_episodes(session.conn, loaded)
        solve = with_memory(session)
    scored = runner.run_suite(
        loaded,
        solve,
        session.conn,
        model=session.model,
        provider=session.provider_name,
        system=session.system(),
        label=label,
        fresh=fresh,
        samples=samples,
        bus=session.bus,
        resume=resume,
        retries=log,
        serve=pinned_serve(session) if loaded.kind == KIND_WORK else None,
    )
    if as_json:
        render.json_object(runner.json_payload(scored))
    else:
        render.eval_run(scored)
    return False, not scored.unscored


def _run_routing(*, as_json: bool) -> tuple[bool, bool]:
    """Score the routing suite, naming every task it failed by handler on stderr (FR-118); the
    caller exits 1 when there is one.

    The only eval door that builds a Decider, and it builds one for this suite alone: the eval
    suites still go through :func:`open_session` without ``routed``, so FR-119's spy stays at 0.
    The replaying Decider answers from the draft registry its fixtures were recorded against; a
    live one is asked with the operator's own ``agents.toml``.
    """
    loaded = routing.load_routing_suite()
    settings = config.load()
    decider = build_decider(settings)
    if decider is None:  # the routing suite scores the router: no key, nothing to score
        settings.require_api_key("openrouter")
    assert decider is not None
    agents = None if isinstance(decider, FakeDecider) else load_agents(AGENTS_DEFAULT_PATH)
    retried: list[str] = []
    if not isinstance(decider, FakeDecider):
        decider = RetryingDecider(
            decider,
            on_retry=lambda call, attempt, error_class, backoff: retried.append(error_class),
        )
    with closing(db.connect()) as conn:
        scored, unscored = _run_routing_tolerant(loaded, decider, conn, agents)
    payload = routing.json_payload(scored)
    payload["unscored"] = [
        {"case_id": task_id, "reason": "transport", "attempts": attempts, "error_class": name}
        for task_id, attempts, name in unscored
    ]
    payload["retried_calls"] = len(retried)
    if as_json:
        render.json_object(payload)
    else:
        summary = (
            (
                render.unscored_line(
                    scored.total, scored.total + len(unscored), ["transport"] * len(unscored)
                ),
            )
            if unscored
            else ()
        )
        render.rows(
            f"routing ({scored.registry} registry)  by handler {scored.by_handler}/{scored.total}"
            f"  exact route {scored.exact}/{scored.total}",
            (
                f"p50 {payload['latency_p50_seconds']} s  p95 {payload['latency_p95_seconds']} s"
                f"  cost per decision {payload['cost_per_decision']}",
                *summary,
                *(
                    f"{_mark(result.by_handler, result.exact)}  {result.id}  {result.route} "
                    f"({result.handler}, {result.confidence})  accepted "
                    f"{_accepted_row(result.accepted, agents)}"
                    for result in scored.results
                ),
                *(
                    f"unscored  {task_id}  {name} after {attempts} attempt(s)"
                    for task_id, attempts, name in unscored
                ),
            ),
        )
    if scored.failed:
        render.failure(routing.failure_message(scored))
    return bool(scored.failed), not unscored


def _run_routing_tolerant(
    loaded: routing.RoutingSuite,
    decider: Decider,
    conn: sqlite3.Connection,
    agents: Mapping[str, AgentSpec] | None,
) -> tuple[routing.RoutingScore, list[tuple[str, int, str]]]:
    """:func:`routing.run_routing` one task at a time, so a task whose Decisions call dropped on
    every attempt is named unscored and the others are still scored (ticket 231, FR-160).

    ``run_routing`` lets a ``DecisionError`` propagate, rightly: a router that cannot answer has no
    score. A drop is the one fault that says nothing about the router, so only it is caught here,
    and the totals are rebuilt from the answered tasks the way ``run_routing`` builds them.
    """
    results: list[routing.TaskResult] = []
    unscored: list[tuple[str, int, str]] = []
    for task in loaded.tasks:
        one = dataclasses.replace(loaded, tasks=(task,))
        try:
            results.extend(routing.run_routing(one, decider, conn, agents=agents).results)
        except DecisionDropped as dropped:
            unscored.append((task.id, dropped.attempts, dropped.error_class))
    costs = sum((Decimal(result.cost) for result in results), Decimal(0))
    timings = [result.seconds for result in results]
    return (
        routing.RoutingScore(
            registry="draft" if agents is None else "operator",
            results=tuple(results),
            exact=sum(result.exact for result in results),
            by_handler=sum(result.by_handler for result in results),
            total=len(results),
            p50_seconds=routing.percentile(timings, 0.50),
            p95_seconds=routing.percentile(timings, 0.95),
            cost_total=costs,
            cost_per_decision=costs / len(results) if results else None,
        ),
        unscored,
    )


def _accepted_row(accepted: tuple[str, ...], agents: Mapping[str, AgentSpec] | None) -> str:
    """``result.accepted``, joined, with a route the operator's registry does not offer marked
    ``(not installed)`` (ticket 196's AC2 example: ``email_writer (not installed), groq_chat``).
    ``agents=None`` (the draft registry, or no agent-only route to mark) leaves every name plain."""
    if agents is None:
        return ", ".join(accepted)
    offered = set(route_options(agents))
    return ", ".join(
        route if route in FIXED_ROUTE_OPTIONS or route in offered else f"{route} (not installed)"
        for route in accepted
    )


def _mark(by_handler: bool, exact: bool) -> str:
    """``pass`` for an exact route, ``handler`` for the right handler by another route, ``fail``."""
    if exact:
        return "pass   "
    return "handler" if by_handler else "fail   "


@eval_app.command("list")
def list_runs(
    limit: Annotated[int, typer.Option("--limit", help="How many runs to show.")] = 20,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print recent eval runs, newest first.

    This exists so ``eval show`` has a way to learn its own argument. Until ticket 045 the group
    had exactly one command, and a run scored an hour ago was in the ledger and reachable only by
    opening SQLite by hand.

    Opens the database directly rather than through ``open_session``, like the memory group:
    reading a score back has no business constructing a provider, and constructing one is what
    would make this fail on a checkout configured for a live model with no key.
    """
    found = runner.recent(db.connect(), limit=limit)
    if as_json:
        render.json_object({"runs": [runner.summary_payload(item) for item in found]})
        return
    render.rows(
        f"{len(found)} eval run(s), newest first",
        tuple(
            f"{item.eval_run_id}  {item.suite} ({item.label})  "
            f"score {item.score}  {item.passes}/{item.case_count} passed  "
            f"x{item.samples}  {item.started_at}"
            for item in found
        ),
    )


@eval_app.command("suites")
def suites(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print every suite with its cases awaiting a verdict and its latest score (FR-139)."""
    try:
        found = suite_store.listing(db.connect())
    except SuiteLoadError as exc:
        render.failure(str(exc))
        raise typer.Exit(code=1) from exc
    if as_json:
        render.json_object(suite_store.listing_payload(found))
        return
    render.rows(
        f"{len(found)} suite(s)",
        tuple(
            f"{item.suite} ({item.kind})  {item.cases_total} cases, "
            f"{item.cases_pending} awaiting a verdict  latest "
            f"{'never run' if item.latest_score is None else item.latest_score}"
            for item in found
        ),
    )


@eval_app.command("show")
def show(
    eval_run_id: Annotated[str, typer.Argument(help="The run to print, from `eval list`.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print one scored run: its aggregate, and every case with the graders' own details.

    Rebuilt from the ledger rather than re-executed. Re-running the suite would report what it
    scores *now*, against a model and a store that have moved on, which is a different claim than
    the one an operator asking about a past run is making.
    """
    found = runner.scored_run(db.connect(), eval_run_id)
    if found is None:
        render.failure(
            f"no eval run with id {eval_run_id!r}. `harnessed eval list` prints the ids it knows."
        )
        raise typer.Exit(code=1)
    if as_json:
        render.json_object(runner.json_payload(found))
    else:
        render.eval_run(found)


#: The command is ``compare``; the function is not, because ``compare`` is a name
#: ``evals/scoring.py`` owns and no other module may define (``tests/test_scoring.py``).
#: The same split ``list``/``list_runs`` already makes above.
@eval_app.command("compare")
def compare_runs(
    before: Annotated[str, typer.Argument(help="The baseline eval run id, from `eval list`.")],
    after: Annotated[str, typer.Argument(help="The candidate eval run id, from `eval list`.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print how one eval run differs from another: what broke, what slipped, what moved.

    The byte-identical twin of ``GET /v1/eval/compare`` (FR-62, decision 37). Both arguments are
    required, because half a comparison is not a smaller question - it is a different one - and
    both sides must name the same suite.

    The comparison itself is ``evals/scoring.py``'s, applied inside ``runner.comparison``. This
    command composes nothing and decides nothing: there is no verdict here, because whether the
    candidate is better is the promoter's call and ``harnessed improve`` is where it is made.
    """
    compared = runner.comparison(db.connect(), before, after)
    if as_json:
        render.json_object(runner.comparison_payload(compared))
        return
    render.rows(
        f"{compared.before.eval_run_id} -> {compared.after.eval_run_id}  ({compared.before.suite})",
        (
            f"score {compared.before.score} -> {compared.after.score}",
            f"passed {compared.before.passes}/{compared.before.case_count} -> "
            f"{compared.after.passes}/{compared.after.case_count}",
            f"regressions: {_named(d.case_id for d in compared.comparison.regressions)}",
            f"soft drops:  {_named(d.case_id for d in compared.comparison.soft_drops)}",
            f"new cases:   {_named(compared.comparison.new_cases)}",
            f"missing:     {_named(compared.comparison.missing_cases)}",
        ),
    )


def _named(case_ids: Iterable[str]) -> str:
    """The case ids on one line, or the word for an empty list. Never a count standing alone."""
    return ", ".join(case_ids) or "none"


def _target(to: Path | None) -> tuple[Path | None, Path | None]:
    """The suites and fixtures roots ``--to`` names, or ``None`` twice for the home."""
    return (None, None) if to is None else recording.corpus_roots(to)


@eval_app.command("record")
def record_suite(
    suite: Annotated[str, typer.Argument(help="The work suite to write under <home>/suites.")],
    session: Annotated[
        str | None, typer.Option("--session", help="The session to record from.")
    ] = None,
    seq: Annotated[
        list[int] | None,
        typer.Option("--seq", help="Record only this message (repeatable); default every one."),
    ] = None,
    remove: Annotated[
        bool,
        typer.Option("--remove", help="Delete the recorded suite and its fixtures instead."),
    ] = False,
    to: Annotated[
        Path | None,
        typer.Option("--to", help="A corpus directory to write to instead of the home."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Record one case per operator message of a session into a work suite (FR-136, FR-172).

    Appends only: a case already recorded keeps its id, its verdict and its fixtures. Every case
    starts as ``verdict: pending``; ``harnessed eval verdict`` sets the operator's own. A task's
    own messages are not recorded. ``--remove`` deletes a recorded suite and its fixtures.
    """
    if remove:
        try:
            suites_root, fixtures_root = _target(to)
            gone = recording.remove(suite, suites_root=suites_root, fixtures_root=fixtures_root)
        except SuiteLoadError as exc:
            render.failure(str(exc))
            raise typer.Exit(code=1) from exc
        if as_json:
            render.json_object(recording.remove_payload(gone))
            return
        render.rows(
            f"{suite}: removed, {len(gone.fixtures)} fixture dir(s) deleted",
            tuple(f"removed  {case_id}" for case_id in gone.fixtures),
        )
        return
    if session is None:
        render.failure("Give --session to record from, or --remove to delete the suite.")
        raise typer.Exit(code=1)
    try:
        suites_root, fixtures_root = _target(to)
        with closing(db.connect()) as conn:
            done = recording.record(
                conn,
                suite,
                session,
                seqs=seq or (),
                suites_root=suites_root,
                fixtures_root=fixtures_root,
            )
    except (SuiteLoadError, SurfaceNotFound) as exc:
        render.failure(str(exc))
        raise typer.Exit(code=1) from exc
    if as_json:
        render.json_object(recording.record_payload(done))
        return
    render.rows(
        f"{suite}: {len(done.added)} case(s) recorded, {len(done.kept)} already there",
        tuple(f"recorded  {case_id}" for case_id in done.added),
    )


@eval_app.command("verdict")
def verdict(
    suite: Annotated[str, typer.Argument(help="The work suite.")],
    case: Annotated[str | None, typer.Argument(help="The case id.")] = None,
    answer: Annotated[str | None, typer.Argument(help="accept or reject.")] = None,
    trait: Annotated[
        list[str] | None,
        typer.Option("--trait", help="The reply must contain this (repeatable)."),
    ] = None,
    pending: Annotated[
        bool, typer.Option("--pending", help="List the cases still awaiting a verdict.")
    ] = False,
    to: Annotated[
        Path | None,
        typer.Option("--to", help="A corpus directory holding the suite instead of the home."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Set the verdict on a recorded case, or list the cases awaiting one (FR-136)."""
    try:
        if pending:
            _list_pending(suite, to, as_json=as_json)
            return
        if case is None or (answer is None and not trait):
            render.failure("name a case and one of: accept, reject, --trait <text>.")
            raise typer.Exit(code=1)
        if answer is not None and answer not in ("accept", "reject"):
            render.failure(f"the verdict is accept or reject, not {answer!r}.")
            raise typer.Exit(code=1)
        suites_root, _ = _target(to)
        recording.set_verdict(
            suite, case, verdict=answer, traits=trait or (), suites_root=suites_root
        )
    except SuiteLoadError as exc:
        render.failure(str(exc))
        raise typer.Exit(code=1) from exc
    render.rows(f"{suite}: {case} set", ())


def _list_pending(suite: str, to: Path | None, *, as_json: bool) -> None:
    waiting = recording.pending_cases(suite, None if to is None else to / "suites")
    if as_json:
        render.json_object(recording.pending_payload(waiting))
        return
    if not waiting:
        render.rows("no case awaits a verdict", ())
        return
    render.rows(
        f"{len(waiting)} case(s) await a verdict",
        tuple(
            f"{case.id}\n  input: {case.prompt}\n  reference: {case.reference}\n"
            for case in waiting
        ),
    )
