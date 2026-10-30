"""``harnessed run`` - one ad-hoc prompt through the loop - and the composition every other
command reuses.

The subsystems are all built for injection: ``evals/`` takes a ``Solve`` callable because it may
not reach ``tools/``, ``improve/`` takes a ``Build`` because it may not reach ``loop/``, and
``tools/`` takes its sandbox root because it may not read the environment. Somewhere that has to
be assembled, and the CLI is that somewhere - it is the one package the import table lets reach
everything.

:class:`Session` is that assembly and nothing more: settings, one connection, one bus, the tool
registry and the provider. It holds no policy. ``system()`` is the same system prompt the
flywheel's baseline measurement builds, which is what makes a score taken by ``harnessed eval
run`` comparable with one taken inside ``harnessed improve run``.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Annotated, Final

import typer

from harnessed import clock, config
from harnessed.acp import pending
from harnessed.acp.config import DEFAULT_PATH as AGENTS_DEFAULT_PATH
from harnessed.acp.config import AgentSpec, load_agents
from harnessed.cli import render, routed
from harnessed.config import Settings
from harnessed.errors import (
    ConfigError,
    HarnessError,
    RunInFlight,
    SessionNotContinued,
    SurfaceNotFound,
)
from harnessed.evals import trace
from harnessed.events.bus import Event, EventBus
from harnessed.loop import runs
from harnessed.loop.engine import LoopResult, run_loop
from harnessed.loop.guardrails import ITERATION_LIMIT
from harnessed.loop.journal import (
    NO_JOURNAL,
    Journal,
    Ledger,
    last_iteration,
    messages_for,
)
from harnessed.loop.prompt import (
    BASE_INSTRUCTION,
    SYNTHESIS_INSTRUCTION,
    build_system_prompt,
    build_user_turn,
)
from harnessed.memory import consolidation, db, episodic, gate, procedural, retrieval
from harnessed.providers import build_provider, registry
from harnessed.providers.base import Provider
from harnessed.providers.jev import Decider, build_decider
from harnessed.providers.translate import journal_message, tool_call_payload
from harnessed.providers.types import ChatRequest, Message
from harnessed.router import route as route_module
from harnessed.router.questions import DONE, NEEDS_MEMORY_THRESHOLD, retrieval_kinds_for
from harnessed.tasks import lease
from harnessed.tasks import store as tasks_store
from harnessed.tools import ToolRegistry, default_registry
from harnessed.tools.builtin import delegate
from harnessed.tools.builtin import tools as builtin_tools

__all__ = [
    "EPISODE_KIND",
    "ERROR_RAISED",
    "Event",
    "MemoryRun",
    "Opened",
    "Opening",
    "solve_with_memory",
    "RAISED",
    "RETRIEVED_KINDS",
    "RUN_FINISHED",
    "RUN_STARTED",
    "Session",
    "Settled",
    "TaskHooks",
    "announce",
    "continue_session",
    "open_session",
    "refuse_uncontinued",
    "routed_registry",
    "run",
    "runs_app",
    "settle_raised",
    "totals",
    "turn_seconds",
]

#: The episode kind an ordinary run writes. It pairs with ``acp.delegate.EPISODE_KIND``:
#: ``episodic.py`` describes exactly that split, a loop transcript against a delegation
#: transcript, and there is no third kind.
EPISODE_KIND: Final = "loop"

#: What a run retrieves. **Procedural memory is not in this list**, and that is the point: the
#: active procedures are already loaded wholesale into the system prompt by :meth:`Session.system`,
#: so assembling them again would put the same sentence in the prompt twice and charge the token
#: budget for the second copy.
RETRIEVED_KINDS: Final[tuple[str, ...]] = ("fact", "episode")

#: The two run-scoped names from the frozen taxonomy. Held as constants because this module is
#: their only publisher, and a typo would be a silent hole in the contract whose ``event:`` field
#: v2's SSE gateway reads verbatim.
RUN_STARTED: Final = "run.started"
RUN_FINISHED: Final = "run.finished"

#: The taxonomy's cross-cutting failure name, published when a run's composition raises
#: (decision 49 as amended, FR-92). An existing name (MUST rule 4): no new event name for this.
ERROR_RAISED: Final = "error.raised"

#: The stop reason a raising run records: it ended on an exception, not on a model's stop.
RAISED: Final = "raised"


@dataclass(frozen=True, slots=True)
class Session:
    """Everything one command needs, built once. The composition root's whole product."""

    settings: Settings
    conn: sqlite3.Connection
    bus: EventBus
    tools: ToolRegistry
    provider: Provider
    decider: Decider | None = None
    #: Every agent the router may route to (ticket 167, FR-106). Empty for every caller but
    #: ``run_cmd.run`` and the control app's two openers - the same three that ever build a
    #: ``decider`` - and for either of those when ``agents.toml`` is missing or malformed, which
    #: is a router with two routes rather than a command that cannot start (``doctor_cmd.py``'s
    #: own ``_registry_warnings`` treats a broken registry the same way).
    agents: Mapping[str, AgentSpec] = field(default_factory=dict)
    #: How a permission request reaches a person, and the run lease it gives up while it waits
    #: (ticket 234, FR-130). ``None`` for every caller that is not a door that starts a message:
    #: ``eval run`` and ``improve run`` never take the lease.
    door: pending.Door | None = None
    #: The operator said a dirty working tree may be delegated into: ``harnessed run
    #: --allow-dirty``, ``task run --allow-dirty``, or the task's own stored flag (FR-146).
    allow_dirty: bool = False

    @property
    def model(self) -> str:
        """The model id that rides on every request, resolved by the package that owns the table.

        The CLI knows no vendor literal and no default. It once fell back to the provider's own
        name here, which reads as honest and is a 404 at every vendor - ``groq`` is not a model
        id anywhere. ``registry.model_for`` reads ``HARNESSED_MODEL``, then the confirmed table,
        then raises naming both, and nothing is decided in this file.
        """
        return registry.model_for(self.settings)

    @property
    def provider_name(self) -> str:
        """The configured provider's name, as every ledger row from migration 010 records it.

        Read through ``Settings.require_provider`` like every other caller about to *do*
        something with a provider (FR-27): a session was built from it, so it is always set.
        """
        return self.settings.require_provider()

    def system(self) -> str:
        """The active procedures, enveloped - the prompt FR-17 says a promotion changes.

        Read at call time rather than cached, because a promotion between two commands in the
        same shell must be visible to the second one.

        It takes no retrieval, on purpose (ticket 083): this is the cacheable prefix, and it
        changes only when a candidate is promoted. A run's retrieved memory goes in the user turn
        through ``build_user_turn`` instead, so every path - run, eval, gate, improve - sends the
        same system prompt for the same active procedures.

        The base is always ``BASE_INSTRUCTION`` (ticket 179 deleted the fallback flag, its v4 text
        and the branch that chose between them).
        """
        return build_system_prompt(
            BASE_INSTRUCTION, tuple(p.text for p in procedural.active(self.conn))
        )

    def synthesis_system(self) -> str:
        """A chain's closing pass (ticket 199): exactly ``SYNTHESIS_INSTRUCTION``, 0 procedures
        (MUST rule 34, ticket 218). A second method rather than a parameter, so ``system()``
        keeps taking nothing and every non-chain caller is untouched."""
        return SYNTHESIS_INSTRUCTION

    def solve(
        self,
        request: ChatRequest,
        *,
        run_key: str | None = None,
        start_iteration: int = 0,
        message_seq: int | None = None,
        limit: int | None = None,
    ) -> LoopResult:
        """The loop with its provider, its registry and its ceiling already bound.

        This is the ``Solve`` the eval runner and the flywheel are handed.

        ``run_key`` is the run journal's key (FR-7), and **it is a parameter rather than
        ``self.bus.run_id`` for a reason that cost a red four-command proof to find.** One session
        drives many runs: an eval scores five cases through this method, and the bus's run id is
        the same for all of them. Keying the journal on it made case two replay case one's
        recorded turn, and the smoke score fell from 0.8 to 0.2 - which is precisely the
        "replaying into the wrong key" this ticket's own criterion names.

        So the caller says what a run *is*. ``harnessed run`` passes its run id; the eval runner
        passes something unique per case. Naming none leaves ``NO_JOURNAL``, which is a run that
        always starts from the beginning - the same default ``run_loop`` has always had.

        ``tool_call_payload`` is injected rather than imported by ``loop/``, which may not reach
        ``providers.translate`` - the same seam ticket 013 built for exactly this.

        ``start_iteration`` is where a continued session's turns are keyed from (decision 52).

        ``message_seq`` is the message's own ``run.started`` ``seq`` (decision 61): every turn
        this call journals carries it, ``NULL`` where a caller names none - an eval or improve
        run, which never routes, and so never has one to give.

        ``limit`` is what is left of the message's ceiling when a chain's agent hops already
        spent some of it (decision 52, ticket 176); ``None`` is the whole ``max_iterations``.
        """
        journal: Ledger = (
            NO_JOURNAL
            if run_key is None
            else Journal(
                self.conn,
                run_key,
                serialize_call=tool_call_payload,
                provider=self.provider_name,
                message_seq=message_seq,
            )
        )
        return run_loop(
            self.provider,
            self.tools,
            request,
            limit=self.settings.max_iterations if limit is None else limit,
            bus=self.bus,
            journal=journal,
            start_iteration=start_iteration,
        )


@dataclass(frozen=True, slots=True)
class Opening:
    """A session half open: its settings, connection and bus exist, its provider does not yet."""

    settings: Settings
    conn: sqlite3.Connection
    bus: EventBus


#: Called by :func:`open_session` once the run's bus exists and before its provider is built.
Opened = Callable[[Opening], None]


def open_session(
    *,
    provider: str | None = None,
    token_budget: int | None = None,
    agents_path: Path | None = None,
    opened: Opened | None = None,
    session_id: str | None = None,
    routed: bool = False,
    answer_door: bool = False,
    stopping: Callable[[], bool] | None = None,
    parked: Callable[[str, bool], None] | None = None,
) -> Session:
    """Resolve settings, open the one database, and build the bus, the tools and the provider.

    ``store`` is handed to the tool registry deliberately: without it a delegation has no
    done-state and a retried run would hand the same destructive task to the agent twice
    (FR-23).

    ``provider`` is the ``--provider`` override, and it is threaded through rather than read
    here: ``config.py`` owns the whole resolution order, this module owns none of it.

    The credential is checked before anything is opened, so an unconfigured provider or a missing
    key fails with nothing to close. ``opened`` is the control app's seam (FR-79): it is handed the
    settings, the connection and the bus as soon as the run id exists, and before the provider is
    built, which is the slow part of opening a session and involves no model call.

    ``session_id`` opens the bus on a run that already exists, for :func:`continue_session`: the
    bus picks up that run's ``seq``, and every tool event lands on the same id (decision 50). A
    key that cannot be continued is refused by :func:`refuse_uncontinued` before the bus exists,
    so neither caller publishes anything for it, and the control app answers the refusal rather
    than a 202 (FR-82).

    ``routed=True`` builds a :class:`~harnessed.providers.jev.Decider` (decision 56). Only
    ``run_cmd.run`` and the control app's two openers pass it; ``evals/helpers.py``, ``eval_cmd``
    and ``improve_cmd`` never do, which is what keeps every eval, reflection and promotion path at
    0 Decider calls (FR-119).

    ``routed`` is also what makes the session a door that starts a message: it carries a
    :class:`~harnessed.acp.pending.Door` and :func:`_converse` takes the run lease. ``answer_door``
    says a person can answer a pending permission request (the control app, ticket 234);
    ``stopping`` denies every open one when the process is going down, and ``parked`` is told when
    a run starts and stops waiting on one, which is how the control app counts its parked runs.
    """
    settings = config.load(provider=provider, token_budget=token_budget)
    registry.credential_for(settings)
    conn = db.connect()
    if session_id is not None:
        with ExitStack() as refused:
            refused.callback(conn.close)  # a refused session hands no connection to anyone
            refuse_uncontinued(conn, session_id)
            refused.pop_all()
    bus = EventBus(run_id=session_id, conn=conn)
    if opened is not None:
        opened(Opening(settings=settings, conn=conn, bus=bus))
    routing = routed
    tools = (
        routed_registry(settings.home, bus=bus, agents_path=agents_path, store=conn)
        if routing
        else default_registry(settings.home, bus=bus, agents_path=agents_path, store=conn)
    )
    return Session(
        settings=settings,
        conn=conn,
        bus=bus,
        tools=tools,
        provider=build_provider(settings),
        decider=build_decider(settings) if routing else None,
        agents=_load_agents(agents_path) if routing else {},
        door=_door(settings, conn, bus.run_id, answer_door, stopping, parked) if routing else None,
    )


#: What the terminal answers a permission question with when it says yes, and no.
_YES: Final = "allow_once"
_NO: Final = "reject_once"


def terminal_interactive() -> bool:
    """Whether a person is at this terminal to be asked: stdin and stdout both a tty."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def terminal_prompter(question: str, kinds: tuple[str, ...], timeout_s: float) -> str | None:
    """Ask on the terminal and wait at most ``timeout_s``; ``None`` when nobody answered in time.

    The read happens on a daemon thread so the ceiling holds on every platform without a
    platform-specific ``select``. Only an explicit yes is an allow (``acp/permissions.py``).
    """
    answers: list[str] = []

    def read() -> None:
        try:
            answers.append(input(f"{question} [y/N] "))
        except EOFError:
            return

    reader = threading.Thread(target=read, name="harnessed-prompt", daemon=True)
    reader.start()
    reader.join(timeout_s)
    if not answers:
        return None
    return _YES if answers[0].strip().lower() in ("y", "yes") else _NO


def _door(
    settings: Settings,
    conn: sqlite3.Connection,
    run_id: str,
    answer_door: bool,
    stopping: Callable[[], bool] | None,
    parked: Callable[[str, bool], None] | None,
) -> pending.Door:
    """The hooks a delegation calls while it waits on a person (decisions 75 and 78).

    A pending request gives the run lease up; the answer takes it back, and answers ``False`` -
    ask me again next tick - while another run holds it. Both run on the calling thread, which owns
    ``conn``.
    """

    def on_pending(_permission_id: str) -> None:
        lease.release(conn, run_id)
        if parked is not None:
            parked(run_id, True)

    def on_answered(_permission_id: str) -> bool:
        try:
            lease.acquire(conn, run_id)
        except RunInFlight:
            return False
        if parked is not None:
            parked(run_id, False)
        return True

    interactive = terminal_interactive()
    return pending.Door(
        answer_door=answer_door,
        ceiling_s=float(settings.permission_ceiling_s),
        on_pending=on_pending,
        on_answered=on_answered,
        stopping=stopping,
        interactive=interactive and not answer_door,
        prompter=terminal_prompter if interactive and not answer_door else None,
    )


def routed_registry(
    root: Path,
    *,
    bus: EventBus | None = None,
    agents_path: Path | None = None,
    store: sqlite3.Connection | None = None,
) -> ToolRegistry:
    """Every builtin tool minus ``delegate_to_agent`` (decision 60, FR-107).

    With routing on, a Groq route delegates from code (``cli/routed.py``), never by the model
    asking for it, so Groq never sees the hand-off tool's schema at all. Composed here, from
    :func:`harnessed.tools.builtin.tools`, rather than in ``tools/`` itself: ``tools/`` is
    ``scope_out`` for the ticket that added this function, and an unrouted caller (``routed=False``)
    still gets every builtin tool, unfiltered, through :func:`default_registry`.
    """
    return ToolRegistry(
        tuple(
            tool
            for tool in builtin_tools(root, agents_path=agents_path, bus=bus, store=store)
            if tool.name != delegate.TOOL_NAME
        ),
        bus=bus,
    )


def _load_agents(agents_path: Path | None) -> dict[str, AgentSpec]:
    """Every agent the router may widen its ``route`` question with, or none for a missing or
    malformed registry (ticket 167): a router does not fail a run over a file only one of its
    routes needs, it just offers two routes instead of three - the same read
    ``doctor_cmd.py::_registry_warnings`` gives a broken registry.
    """
    path = AGENTS_DEFAULT_PATH if agents_path is None else agents_path
    try:
        return load_agents(path)
    except ConfigError:
        return {}


def run(
    prompt: Annotated[str, typer.Argument(help="What to ask. One ad-hoc run, one answer.")],
    provider: Annotated[
        str | None,
        typer.Option(
            config.PROVIDER_FLAG,
            help="Use this provider for this run only. Beats HARNESSED_PROVIDER and .env.",
        ),
    ] = None,
    token_budget: Annotated[
        int | None,
        typer.Option(
            config.TOKEN_BUDGET_FLAG,
            help="Override the retrieval token budget for this run only.",
        ),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session",
            help="Continue this session: the prompt is one more message of the same run.",
        ),
    ] = None,
    allow_dirty: Annotated[
        bool,
        typer.Option(
            "--allow-dirty", help="Delegate into a working tree that has uncommitted changes."
        ),
    ] = False,
) -> None:
    """Run one prompt through the loop and print the reply.

    ``--json`` prints ``loop/runs.py::run_payload`` for the run just finished - the same dict the
    gateway returns for that run key - read back off the journal rather than assembled from the
    in-memory result, so the CLI and the wire cannot describe one run two ways (MUST rule 16).

    ``--session <id>`` continues that run through :func:`continue_session`, the composition the
    control app calls too (FR-80).

    A composition that raises settles through :func:`settle_raised` before this door re-raises it
    for ``main`` to render at exit 1 - the same pair the control app's worker publishes on the
    same raise, so the two doors cannot diverge (MUST rule 16, FR-92).
    """
    if session_id is not None and _resumes_task(session_id, prompt, provider, as_json, allow_dirty):
        return
    session = open_session(
        provider=provider, token_budget=token_budget, session_id=session_id, routed=True
    )
    if allow_dirty:
        session = replace(session, allow_dirty=True)
    try:
        memory_run = (
            solve_with_memory(session, prompt)
            if session_id is None
            else continue_session(session, session_id, prompt)
        )
    except HarnessError as exc:
        settle_raised(session.bus, exc)
        raise
    if memory_run.verdict is not None:
        payload = routed.uncertain_payload(memory_run.verdict)
        if as_json:
            render.json_object(payload)
        else:
            render.uncertain_stop(payload)
    elif as_json:
        render.json_object(runs.run_payload(session.conn, memory_run.run_id))
    else:
        assert memory_run.result is not None
        render.reply(memory_run.result, run_id=memory_run.run_id)
    # After the reply is printed, the run finished and its lease released: 0 latency to the message.
    consolidate_after_message(session)


def consolidate_after_message(
    session: Session, *, own_connection: bool = False
) -> consolidation.Outcome | None:
    """The three doors' trigger (ticket 247, FR-148, decision 81): called once a message has
    finished - ``run.finished`` published, the run lease released - never inside the loop.

    The small model is the gate's own: ``Settings.small_model``, or the session's model when none
    is named. ``own_connection`` is the control worker's: its job runs on a thread of its own, and
    a connection belongs to the thread that opened it, so the pass opens and closes its own.
    """
    settings = replace(session.settings, small_model=session.settings.small_model or session.model)
    conn = db.connect() if own_connection else session.conn
    try:
        return consolidation.maybe_consolidate(
            conn,
            provider=session.provider,
            settings=settings,
            bus_factory=lambda: EventBus(conn=conn),
            now=clock.now(),
            session_id=session.bus.run_id,
        )
    finally:
        if own_connection:
            conn.close()


def _resumes_task(
    session_id: str, prompt: str, provider: str | None, as_json: bool, allow_dirty: bool = False
) -> bool:
    """A message on a task's session (ticket 240, FR-141): paused ``uncertain`` it resumes the task
    with the operator's words, waiting on a permission answer or running it is refused, and for a
    finished task it is an ordinary follow-up (``False``: the caller goes on as before)."""
    from harnessed.cli import task_run  # imported here: ``task_run`` composes this module

    task = tasks_store.for_session_text(session_id)
    if task is None or not task_run.route_message(task):
        return False
    row = task_run.run_task(
        str(task["task_id"]),
        first_message=prompt,
        author="operator",
        provider=provider,
        allow_dirty=allow_dirty,
    )
    if as_json:
        render.json_object(tasks_store.payload(row))
    else:
        render.line(f"{row['state']}: {row['task_id']}")
    return True


runs_app = typer.Typer(
    name="runs",
    help="What the run journal holds: loop runs, turns, tool calls and the mean turn.",
    no_args_is_help=True,
)


@runs_app.callback()
def _runs_group() -> None:
    """Keep ``runs`` a command group (see ``eval_cmd._group`` for why)."""


@runs_app.command("totals")
def totals(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Count loop runs, turns and tool calls over the whole journal, all time.

    FR-67's CLI twin. ``--json`` prints ``loop/runs.py::totals_payload`` for ``runs.totals``,
    the same two callables ``GET /v1/runs/totals`` serves, so the line and the body are the
    same bytes (decision 38). The database is opened directly, like ``sessions list``: a count
    has no business constructing a provider.
    """
    found = runs.totals(db.connect())
    if as_json:
        render.json_object(runs.totals_payload(found))
        return
    render.rows(
        f"run journal, all time, counted {found.counted_at}",
        (
            f"runs        {found.runs}  (session and case_run keys)",
            f"turns       {found.turns}",
            f"tool calls  {found.tool_calls}",
        ),
    )


#: How many loop runs ``runs list`` shows when ``--limit`` is absent: ``GET /v1/runs``'s own.
RUNS_LIST_LIMIT: Final = 20


@runs_app.command("list")
def list_runs(
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many runs to show.")] = (
        RUNS_LIST_LIMIT
    ),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print loop runs, newest first, each labelled with its key kind (ticket 232).

    The twin of ``GET /v1/runs``: the same ``runs.listing`` rows under ``runs``, so the ``--json``
    line and the body are the same bytes (decision 19).
    """
    found = runs.listing(db.connect(), limit)
    if as_json:
        render.json_object({"runs": [asdict(row) for row in found]})
        return
    render.rows(
        f"{len(found)} run(s), newest first",
        tuple(f"{row.run_key}  {row.key_kind}" for row in found),
    )


@runs_app.command("trace")
def trace_run(
    run_key: Annotated[str, typer.Argument(help="The session id or case run key to trace.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print one run's trace, one object per iteration (ticket 232).

    The twin of ``GET /v1/runs/{run_key}/trace``: ``evals/trace.py::assemble`` and nothing else.
    A key the journal holds nothing under is refused, as the route's 404 is.
    """
    objects = trace.assemble(db.connect(), run_key)
    if not objects:
        raise SurfaceNotFound(f"no loop run {run_key!r}: the ledgers hold nothing under that id.")
    if as_json:
        render.json_object(list(objects))
        return
    render.rows(
        f"{len(objects)} iteration(s) for run {run_key}",
        tuple(f"iteration {item.get('iteration')}" for item in objects),
    )


@runs_app.command("turn-seconds")
def turn_seconds(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """The mean turn in seconds over the whole journal, and how many turns it could not time.

    FR-68's CLI twin. ``--json`` prints ``loop/runs.py::turn_seconds_payload`` for
    ``runs.turn_seconds``, the two callables ``GET /v1/runs/turn_seconds`` serves, so the line
    and the body are the same bytes (decision 38). The prose form says ``unknown`` when no
    turn carries both timestamps, and names the pre-007 rows it left out when there are any.
    """
    found = runs.turn_seconds(db.connect())
    payload = runs.turn_seconds_payload(found)
    if as_json:
        render.json_object(payload)
        return
    average = payload["average_seconds"]
    render.rows(
        "mean turn, all time",
        (
            f"average seconds  {average}"
            if average is not None
            else "average seconds  unknown (no turn carries both timestamps)",
            f"turns counted    {payload['turns_counted']}",
            f"turns excluded   {payload['turns_excluded']}  ({payload['excluded_reason']})",
        ),
    )


@dataclass(frozen=True, slots=True)
class Settled:
    """How a task's message ended, told to ``on_settle`` before ``run.finished`` (ticket 240).

    ``ended`` is ``ok`` (the message ran), ``task_done`` (a continuation the router answered
    ``done`` at its floor: nothing ran) or ``uncertain`` (an uncertain stop, or on a continuation
    a router that could not be asked); ``verdict`` is the router's, for the last two.
    """

    ended: str
    verdict: route_module.Verdict | None = None


@dataclass(frozen=True, slots=True)
class TaskHooks:
    """The two hooks a task's message is composed with, given only by ``cli/task_run.py``.

    ``on_announced`` runs right after the message's ``run.started`` and before the router is
    asked, so the author is written as the message starts (MUST rule 40). ``on_settle`` runs once
    the message has ended and before ``run.finished`` is published (decision 77). ``continuation``
    says the message is the harness's own continuation, whose router question also offers ``done``;
    ``the_task`` is the state's ``The task:`` section.
    """

    on_announced: Callable[[Event], None]
    on_settle: Callable[[Event, Settled], None]
    continuation: bool = False
    the_task: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRun:
    """What one memory-aware run produced: the context it was given, and what came back - or, for
    an uncertain stop (FR-103), ``verdict`` alone: no gate ran, no loop ran, no episode was
    written, so ``assembled`` and ``result`` are ``None``."""

    run_id: str
    assembled: retrieval.Assembled | None = None
    result: LoopResult | None = None
    verdict: route_module.Verdict | None = None


def announce(bus: EventBus, settings: Settings, *, routing: bool | None = None) -> Event:
    """Publish ``run.started`` on the run's bus: seq 1, the event its stream opens on.

    Its own function because it has two callers. :func:`solve_with_memory` calls it first thing;
    the control app's worker calls it from :func:`open_session`'s ``opened`` seam, before handing
    the session id back to the request thread, so the stream a ``202`` names already exists when
    the browser opens it (FR-79). The worker then runs :func:`solve_with_memory` with
    ``announced=`` this same returned :class:`~harnessed.events.bus.Event` so the run is started
    once, not twice, and the router's row carries the ``message_seq`` it named (decision 61).

    Returns the published :class:`~harnessed.events.bus.Event`: its ``seq`` is the message's
    ``message_seq`` (decision 61), which ticket 161 carries onto the ``decisions`` row this
    message's router call writes.

    The payload's ``routing`` is ``"on"`` or ``"off"`` (ticket 316, decision 105): ``routing`` says
    whether the caller holds a Decider, and a door that has none yet (the control app announces
    before it composes) leaves it ``None`` to read the settings, whose answer is the same one
    ``build_decider`` acts on. A payload field, never an event name.
    """
    if routing is None:
        routing = settings.routing_on()
    return bus.publish(
        RUN_STARTED, {"provider": settings.provider, "routing": "on" if routing else "off"}
    )


def settle_raised(
    bus: EventBus, exc: Exception, *, before_finish: Callable[[], object] | None = None
) -> None:
    """Publish a raising run's finish pair on its own bus: ``error.raised``, then ``run.finished``.

    **The one helper both doors call** (MUST rule 16, FR-92 closing Known defect (a)): the control
    app's worker on ``HarnessError`` "or anything else" the composition raises after the ``202``,
    and :func:`run` here on a ``HarnessError``, before re-raising it for ``main`` to render at
    exit 1. Without the second publish, decision 17's paired-finish close never fires and a
    stream stays open until ``IDLE_CEILING_S``.

    ``run.finished`` always carries ``guardrail: null``: a raise never reached the loop's own
    guardrail decision, so there is none to report.

    ``before_finish`` is a task's checkpoint (ticket 240): it runs between the two publishes, so
    the task's end is kept before ``run.finished`` as it is for a message that did not raise.
    """
    bus.publish(
        ERROR_RAISED,
        {"class": type(exc).__name__, "message": str(exc), "stop_reason": RAISED},
    )
    if before_finish is not None:
        before_finish()
    bus.publish(RUN_FINISHED, {"guardrail": None, "stop_reason": RAISED})


def solve_with_memory(
    session: Session,
    prompt: str,
    *,
    max_tokens: int | None = None,
    announced: Event | None = None,
    task: TaskHooks | None = None,
) -> MemoryRun:
    """One prompt through the loop, with all three kinds of memory attached. No rendering.

    **This is the run the product does, and it is a function so that it can be the run the evals
    do too.** It lived inside the Typer command until ticket 044, which meant the only way to
    reuse it was to copy it - and ``evals/helpers.py::ask`` had copied the half that existed
    before retrieval was wired, so from ticket 041 onward every eval measured a run no operator
    could trigger. A composition that has two implementations has no contract.

    The order is the whole of it. Procedures are loaded wholesale by :meth:`Session.system`;
    facts and this run's episodes come through the gate and the token ceiling, once; the reply is
    written back as an episode so a later run can retrieve it.

    The retrieval goes in the user turn, never the system prompt (architecture section 7). The
    system prompt is then the same bytes on every run, which is the exact prefix a caching
    provider can reuse; and ``FakeProvider`` selects its fixtures on system-prompt text, so
    retrieved context there would make the FR-25 proof depend on whatever is in the database.
    The episode written back records the bare ``prompt``, never the context block.

    ``max_tokens`` is threaded rather than fixed because the eval suite runs on a smaller ceiling
    than an operator's ad-hoc run. Left unset, the request's own default stands, which is what
    keeps this byte-identical to what the command sent before the extraction.

    ``announced`` is the :class:`~harnessed.events.bus.Event` for a caller that already
    published ``run.started`` through :func:`announce` (the control app's worker); every other
    caller leaves it ``None`` and this function announces it here.

    ``task`` is a task message's hooks (ticket 240); every other caller leaves it ``None``.
    """
    return _converse(session, prompt, (), max_tokens=max_tokens, announced=announced, task=task)


def refuse_uncontinued(conn: sqlite3.Connection, run_key: str) -> None:
    """Refuse a key :func:`continue_session` cannot continue; return quietly for one it can.

    A ``case_run`` key is :class:`SessionNotContinued`, because an eval case is scored once; a key
    the run journal holds no message for is :class:`SurfaceNotFound`. :func:`open_session` asks
    before it builds a bus, so a refused follow-up publishes nothing on either caller's path.
    """
    if runs.key_kind_of(run_key) == "case_run":
        raise SessionNotContinued(
            f"{run_key!r} is a case run key: an eval case is scored once and is not continued."
        )
    if not messages_for(conn, run_key, decode=journal_message) and not tasks_store.for_session(
        conn, run_key
    ):
        # A task whose first message paused at the router wrote no journal row, and is continued.
        raise SurfaceNotFound(
            f"no loop run under {run_key!r}: the run journal holds no message for that session "
            f"key, so there is nothing to continue."
        )


def continue_session(
    session: Session,
    run_key: str,
    prompt: str,
    *,
    announced: Event | None = None,
    task: TaskHooks | None = None,
) -> MemoryRun:
    """One more message of an existing run: the stored list, plus this prompt (FR-80).

    **The one composition both the CLI (``harnessed run --session``) and the control app call**
    (decision 50, MUST rule 16). The run's message list is read back through
    ``journal.messages_for`` with every ``[redacted:<kind>]`` marker carried as written, the new
    user message is appended, and the loop runs on the same ``run_id`` with its turns keyed after
    the last one on disk (decision 52). Everything else is :func:`solve_with_memory`'s order: the
    gate runs on this message too and publishes its own decision, and the message is bracketed by
    its own ``run.started`` / ``run.finished`` pair (decision 51).

    ``session`` must be opened on ``run_key`` (``open_session(session_id=run_key)``) so the bus
    and every tool event share the id. Refused before anything is published: a ``case_run`` key
    or a mismatched session is :class:`SessionNotContinued`, a key no journal holds is
    :class:`SurfaceNotFound`.
    """
    refuse_uncontinued(session.conn, run_key)
    if session.bus.run_id != run_key:
        raise SessionNotContinued(
            f"the session was opened on {session.bus.run_id!r}, not on {run_key!r}; open it with "
            f"open_session(session_id=...) to continue a run."
        )
    history = messages_for(session.conn, run_key, decode=journal_message)
    return _converse(
        session,
        prompt,
        history,
        start_iteration=last_iteration(session.conn, run_key),
        announced=announced,
        task=task,
    )


def _router_memory(verdict: route_module.Verdict) -> gate.RouterMemory:
    """A routed ``groq_chat``/``groq_memory`` verdict's own retrieval answer, turned into what
    the gate needs (decision 7 as amended, tickets 169-170): ``retrieve`` is the threshold
    comparison the router's own confidence checks already use the pattern of (decision 59), or
    ``None`` when the verdict carries no ``needs_memory`` probability at all - the gate fails open
    on that, never skips memory over a decision the router never made. ``kinds`` is
    ``questions.retrieval_kinds_for(verdict.memory_kind)`` (FR-111): a ``semantic`` answer
    searches facts, ``episodic`` searches this run's episodes, ``procedural`` retrieves nothing
    extra, and either branch below carries the same mapped kinds - ``memory_kind`` is answered
    alongside ``needs_memory`` in the one Decider call, not only when it is heeded.
    """
    kinds = retrieval_kinds_for(verdict.memory_kind)
    if verdict.needs_memory is None:
        return gate.RouterMemory(retrieve=None, answer="", kinds=kinds)
    return gate.RouterMemory(
        retrieve=verdict.needs_memory >= NEEDS_MEMORY_THRESHOLD,
        answer=str(verdict.needs_memory),
        kinds=kinds,
    )


def _converse(
    session: Session,
    prompt: str,
    history: tuple[Message, ...],
    *,
    start_iteration: int = 0,
    max_tokens: int | None = None,
    announced: Event | None = None,
    task: TaskHooks | None = None,
) -> MemoryRun:
    """The order a first message and a follow-up share: announce, route, gate, loop, write back.

    An uncertain stop (FR-103) breaks that order right after the router answers: no gate call, no
    loop, no episode. ``run.finished`` is published here, with the verdict's own ``stop_reason``
    and ``guardrail: null`` (decision 27), and :class:`MemoryRun` carries the verdict alone.

    A fallback (FR-104, decision 59) does not break the order: ``error.raised`` is published with
    the cause, then this function falls through into the same gate-then-loop path a routed
    ``groq_chat`` verdict runs - the v4 gate, unchanged, and no agent ever reached.

    An agent route (``handler == "agent"``, ticket 167, FR-106) breaks the order the same place
    an uncertain stop does, right after the router answers: no gate call (nothing decides an
    agent route needs memory yet, ticket 169), no loop, and :func:`_delegate` runs the whole rest
    of the message on its own - the delegation, the ``run_messages`` write-back and the episode.

    A routed ``groq_chat``/``groq_memory`` verdict (decision 7 as amended, ticket 169) also breaks
    with v4: the gate gets ``answered=`` built by :func:`_router_memory` from the verdict's own
    ``needs_memory``, so it makes **0** ``_gate`` calls. A fallback leaves ``answered`` ``None``,
    which is what keeps its v4 gate-then-loop path unchanged.
    """
    if session.door is None:
        return _converse_leased(
            session, prompt, history, start_iteration, max_tokens, announced, task
        )
    # A door that starts a message holds the run lease while it works (decision 78): a live holder
    # refuses this one before anything is announced, naming its session.
    lease.acquire(session.conn, session.bus.run_id)
    try:
        return _converse_leased(
            session, prompt, history, start_iteration, max_tokens, announced, task
        )
    finally:
        lease.release(session.conn, session.bus.run_id)


def _converse_leased(
    session: Session,
    prompt: str,
    history: tuple[Message, ...],
    start_iteration: int,
    max_tokens: int | None,
    announced: Event | None,
    task: TaskHooks | None = None,
) -> MemoryRun:
    """:func:`_converse`'s body, with the lease (when one is wanted) already held."""
    event = (
        announced
        if announced is not None
        else announce(session.bus, session.settings, routing=session.decider is not None)
    )
    if task is not None:
        task.on_announced(event)

    answered: gate.RouterMemory | None = None
    handoff: _Handoff | None = None
    if session.decider is not None:
        verdict = _ask_router(session, prompt, history, event, task)
        if verdict.outcome == "routed" and verdict.route == DONE:
            # Only a task's continuation offers ``done`` at hop 0 (decision 77): the message
            # settles with no handler run, and the checkpoint ends the task.
            return _settled_without_running(
                session,
                event,
                task,
                Settled("task_done", verdict),
                {"guardrail": None, "iterations": 0, "stop_reason": "task_done"},
            )
        if verdict.outcome == "fallback" and task is not None and task.continuation:
            # The router could not be asked whether the job is done; running the continuation
            # through Groq would spend the budget on a guess (decision 59 as amended).
            session.bus.publish(ERROR_RAISED, routed.fallback_payload(verdict))
            return _settled_without_running(
                session,
                event,
                task,
                Settled("uncertain", verdict),
                {"guardrail": None, "stop_reason": "needs_human"},
            )
        if verdict.outcome == "uncertain":
            return _settled_without_running(
                session,
                event,
                task,
                Settled("uncertain", verdict),
                {"guardrail": None, "stop_reason": verdict.stop_reason},
            )
        if verdict.handler == "agent":
            handoff = _chain(
                session,
                verdict,
                prompt,
                history,
                announced=event,
                the_task=None if task is None else task.the_task,
            )
        elif verdict.outcome == "fallback":
            session.bus.publish(ERROR_RAISED, routed.fallback_payload(verdict))
        else:
            answered = _router_memory(verdict)

    if handoff is None:
        _decision, assembled = gate.assemble_if_gated(
            session.provider,
            session.conn,
            prompt,
            budget=session.settings.retrieval_token_budget,
            small_model=session.settings.small_model or session.model,
            run_id=session.bus.run_id,
            kinds=RETRIEVED_KINDS,
            publish=session.bus.publish,
            answered=answered,
        )
    else:
        # A chain's Groq hop: the agent's result is the user turn's retrieved context, in place of
        # a retrieval (decision 60), so the gate is not asked.
        assembled = retrieval.handed(
            handoff.context, budget=session.settings.retrieval_token_budget
        )

    request = ChatRequest(
        model=session.model,
        system=session.system() if handoff is None else session.synthesis_system(),
        messages=(*history, Message(role="user", content=build_user_turn(assembled.text, prompt))),
    )
    if max_tokens is not None:
        request = replace(request, max_tokens=max_tokens)
    groq_hop = _groq_hop(handoff, verdict if session.decider is not None else None)
    try:
        result = session.solve(
            request,
            run_key=session.bus.run_id,
            start_iteration=start_iteration,
            message_seq=event.seq,
            limit=(
                None if handoff is None else max(1, session.settings.max_iterations - handoff.taken)
            ),
        )
    except BaseException:  # settle the hop's row, then re-raise: nothing is swallowed
        if groq_hop is not None:
            _settle(session, event, groq_hop, routed.FAILED, routed.RAISED)
        raise
    if groq_hop is not None:
        _settle(session, event, groq_hop, routed.SUCCEEDED)
    if handoff is not None:
        result = replace(
            result,
            iterations=handoff.taken + result.iterations,
            guardrail=handoff.guardrail or result.guardrail,
        )

    # Written before rendering, so a failure while printing cannot lose the record of a run
    # that actually happened. A run that raised inside the loop never reaches this line, which
    # is the whole of "an episode records what happened".
    episode = episodic.put(
        session.conn, session.bus.run_id, EPISODE_KIND, _transcript(prompt, result)
    )
    session.bus.publish(
        MEMORY_WRITE_COMMITTED, {"kind": "episode", "episode_id": episode.episode_id}
    )
    trace.write(session.conn, session.settings.home, session.bus.run_id, assembled=assembled)
    if task is not None:
        task.on_settle(event, Settled("ok"))
    session.bus.publish(
        RUN_FINISHED,
        {
            "iterations": result.iterations,
            "tool_calls": len(result.tool_calls),
            "guardrail": result.guardrail,
            "stop_reason": result.stop_reason,
            "retrieved_tokens": assembled.tokens,
        },
    )
    return MemoryRun(assembled=assembled, result=result, run_id=session.bus.run_id)


def _ask_router(
    session: Session,
    prompt: str,
    history: tuple[Message, ...],
    event: Event,
    task: TaskHooks | None,
) -> route_module.Verdict:
    """The message's hop-0 decision. A task message adds its ``The task:`` section and, on a
    continuation, the ``done`` option (ticket 240); any other message asks exactly as before."""
    assert session.decider is not None
    if task is None:
        return routed.dispatch(
            session.conn,
            session.decider,
            prompt,
            history,
            announced=event,
            model=session.settings.router_model,
            publish=session.bus.publish,
            agents=session.agents,
        )
    return routed.dispatch(
        session.conn,
        session.decider,
        prompt,
        history,
        announced=event,
        model=session.settings.router_model,
        publish=session.bus.publish,
        agents=session.agents,
        the_task=task.the_task,
        continuation=task.continuation,
    )


def _settled_without_running(
    session: Session,
    event: Event,
    task: TaskHooks | None,
    settled: Settled,
    finished: dict[str, object],
) -> MemoryRun:
    """A message the router ended before any handler ran: settle the task, then ``run.finished``."""
    if task is not None:
        task.on_settle(event, settled)
    session.bus.publish(RUN_FINISHED, finished)
    return MemoryRun(run_id=session.bus.run_id, verdict=settled.verdict)


#: The loop's own event name, published here only when a chain reaches the message's ceiling
#: (decisions 27 and 52): no model turn ended the chain, so its ``stop_reason`` is null.
GUARDRAIL_TRIPPED: Final = "loop.guardrail.tripped"

#: Published after the episode lands and before ``run.finished`` (ticket 232, decision 73): the
#: event the dashboard's Episodic row reads.
MEMORY_WRITE_COMMITTED: Final = "memory.write.committed"

#: Iterations a chain leaves for its closing Groq synthesis pass (ticket 198).
SYNTHESIS_RESERVE: Final = 1

#: The ``scope`` a chain's trip names, beside the loop's own ``"message"`` (decision 27).
CHAIN_SCOPE: Final = "chain"


@dataclass(frozen=True, slots=True)
class _Handoff:
    """How every chain ends (ticket 198): the last agent result, which becomes the user turn's
    retrieved context for one Groq synthesis pass, the agent hops already spent against the
    ceiling, and the guardrail the chain tripped, if any."""

    context: str
    taken: int
    guardrail: str | None = None
    #: The handler of the decision that ended the chain: ``groq`` means that decision's own hop
    #: is the synthesis pass, which settles its row.
    last_handler: str | None = None


def _chain(
    session: Session,
    verdict: route_module.Verdict,
    prompt: str,
    history: tuple[Message, ...],
    *,
    announced: Event,
    the_task: str | None = None,
) -> _Handoff:
    """An agent route and the chain after it (tickets 167 and 176; FR-106, FR-117).

    Each hop is decision 60's ``task`` and ``acceptance`` built in code with **0** provider calls,
    then the delegation. After an agent returns the router is asked once more, with ``done`` among
    the options and the result as ``Previous step:``: an agent route delegates again, handed that
    result; ``done``, an uncertain answer or a fallback ends the chain with the last output as the
    reply; a Groq route hands the result back to :func:`_converse` as a :class:`_Handoff`, which
    runs the loop on what is left of the ceiling.

    Every agent hop counts one against ``max_iterations`` for the message (decision 52). The
    ceiling is checked before the next decision is asked: once it is reached the chain stops,
    ``loop.guardrail.tripped`` names ``iteration_limit`` with ``scope: "chain"`` and a null
    ``stop_reason``. The chain delegates at most ``max(1, max_iterations - SYNTHESIS_RESERVE)``
    times, so the synthesis has an iteration left.
    """
    assert session.decider is not None
    limit = max(1, session.settings.max_iterations - SYNTHESIS_RESERVE)
    acceptance = routed.build_acceptance()
    previous: str | None = None
    guardrail: str | None = None
    taken = 0
    while True:
        assert verdict.route is not None
        answered_by = verdict.route
        try:
            delegation = routed.run_agent(
                session.agents[verdict.route],
                routed.build_task(prompt, history, previous),
                acceptance,
                home=session.settings.home,
                bus=session.bus,
                store=session.conn,
                message_seq=announced.seq,
                hop=taken,
                door=session.door,
                openrouter_key=session.settings.api_key_for("openrouter"),
                allow_dirty=session.allow_dirty,
            )
        except BaseException as exc:  # settle the hop's row, then re-raise: nothing is swallowed
            _settle(session, announced, taken, routed.FAILED, routed.failure_reason(exc))
            raise
        _settle(session, announced, taken, routed.SUCCEEDED)
        previous = delegation.summary
        taken += 1
        if taken >= limit:
            guardrail = ITERATION_LIMIT
            session.bus.publish(
                GUARDRAIL_TRIPPED,
                {
                    "guardrail": guardrail,
                    "iterations": taken,
                    "stop_reason": None,
                    "scope": CHAIN_SCOPE,
                },
            )
            break
        verdict = routed.dispatch(
            session.conn,
            session.decider,
            prompt,
            history,
            announced=announced,
            model=session.settings.router_model,
            publish=session.bus.publish,
            agents=session.agents,
            hop=taken,
            previous=previous,
            previous_by=answered_by,
            exclude=(answered_by,),
            the_task=the_task,
        )
        if verdict.outcome == "fallback":
            session.bus.publish(ERROR_RAISED, routed.fallback_payload(verdict))
        if verdict.outcome != "routed" or verdict.route == DONE or verdict.handler != "agent":
            break

    assert previous is not None
    return _Handoff(
        context=previous, taken=taken, guardrail=guardrail, last_handler=verdict.handler
    )


# The router's handler name (router/route.py::Handler), not a provider to branch on.
_GROQ_HANDLER = "groq"


def _groq_hop(handoff: _Handoff | None, verdict: route_module.Verdict | None) -> int | None:
    """The hop whose own decision sent the reply to Groq, when a decision was recorded for it."""
    if handoff is not None:
        return handoff.taken if handoff.last_handler == _GROQ_HANDLER else None
    if verdict is not None and verdict.handler == _GROQ_HANDLER:
        return 0
    return None


def _settle(
    session: Session, announced: Event, hop: int, result: str, reason: str | None = None
) -> None:
    """Settle one hop's own ``decisions`` row (migration 018), when the router wrote one."""
    if session.decider is not None:
        routed.settle(
            session.conn,
            run_id=session.bus.run_id,
            message_seq=announced.seq,
            hop=hop,
            result=result,
            reason=reason,
        )


def _transcript(prompt: str, result: LoopResult) -> str:
    """One episode's text: what was asked, then what came back.

    Two labelled lines rather than JSON, because this string is retrieved and put back into a
    later prompt verbatim - ``retrieval._render`` prints it as a bullet and the model reads it.
    """
    return f"prompt: {prompt}\nreply: {result.reply}"
