"""The control app's one worker, and the only state the app holds between requests (decision 49).

One ``ThreadPoolExecutor(max_workers=1)``, built by ``create_app`` and held on ``app.state``. Not
``BackgroundTasks``, which runs after the response is sent; not a process, because the connection
and the bus are in-process; not unbounded threads, because one run at a time is the rule, and a
one-worker executor is that rule as a type rather than as a check.

**In flight is the future.** :class:`Worker` holds the ``Future`` of the run it last accepted and
that run's session id, and nothing else (MUST rule 17 as amended): ``RunInFlight`` is ``run is not
None and not run.done()``, decided and submitted under one ``threading.Lock`` so two simultaneous
POSTs cannot both win. After a restart nothing is in flight, because a future does not survive a
process.

The session is opened **on the worker thread**, where every ledger write of the run then happens:
``db.connect()`` hands back a connection bound to the thread that opened it, and the run's
connection, bus and journal are the CLI's own (``run_cmd.open_session``). Its ``opened`` seam fires
as soon as the bus exists: :func:`_carry` publishes ``run.started`` there and hands the session id
back to the request thread through a hand-off future, and only then is the provider built. So the
handler answers before the run's first model call, and the stream its 202 names already holds its
seq 1 (FR-79). Decision 49 places the open and the publish on the request thread; the thread-bound
connection is why they happen here instead, with the ordering the decision asks for: both are done
before the handler returns. An unconfigured provider or a missing key is refused before the seam
fires, so the handler answers that error and nothing is held.

**A run that raises frees the worker and ends its stream.** :func:`_carry` catches whatever the
run raises and calls :func:`run_cmd.settle_raised`, which publishes ``error.raised`` (an existing
name, MUST rule 4) and then ``run.finished`` with ``guardrail: null`` and the stop reason
:data:`RAISED`, on that run's own bus (decision 49 as amended, FR-92): the same helper
``harnessed run`` calls before exiting 1, so the two doors cannot diverge (MUST rule 16). The bus
scrubs every payload before it is stored, so the message is the scrubbed one. The future always
completes, so the worker is never stuck busy; decision 17's paired finish closes the browser's
stream at once rather than at ``IDLE_CEILING_S``.

The lifespan calls ``executor.shutdown(wait=True)``, so a run in flight finishes its current turn
and the journal closes consistent. It is a sync context manager lifted into the threadpool by
FastAPI's own helper, because ``async def`` lives in exactly two modules and this is neither.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field

from fastapi.concurrency import contextmanager_in_threadpool

from harnessed.acp.pending import PARKED_MAX
from harnessed.cli import run_cmd, task_run
from harnessed.cli.run_cmd import ERROR_RAISED, RAISED, Event, Opened, Opening, Session
from harnessed.errors import RunInFlight
from harnessed.tasks import store as task_store

__all__ = ["ERROR_RAISED", "RAISED", "Opener", "Work", "Worker"]

#: Opens the run's session on the worker thread: the CLI's ``open_session``, handed its seam.
Opener = Callable[[Opened], Session]

#: The run itself, handed the open session and the ``run.started`` :func:`opened` already
#: published (decision 61): the CLI's composition, called directly, passes it on as ``announced``
#: so the router's row carries the same ``message_seq`` the browser's stream opened on.
Work = Callable[[Session, Event], object]


def _in_flight(session_id: str) -> RunInFlight:
    """The refusal for ``session_id``: it names the task when the session is a task's."""
    try:
        row = task_store.for_session_text(session_id)
    except Exception:  # the 409 is still right when the ledger cannot be read
        row = None
    if row is None:
        return RunInFlight(session_id)
    return RunInFlight(session_id, str(row["task_id"]), str(row["text"] or ""))


def _one_worker() -> ThreadPoolExecutor:
    """One active run plus the runs parked on a permission request (decision 75): a parked run
    holds a blocked child, not the run, so it needs a thread but not the run's place."""
    return ThreadPoolExecutor(max_workers=1 + PARKED_MAX, thread_name_prefix="harnessed-control")


@dataclass(slots=True)
class Worker:
    """One executor, one lock, and the run in flight with its session id. Mutable on purpose."""

    executor: ThreadPoolExecutor = field(default_factory=_one_worker)
    lock: threading.Lock = field(default_factory=threading.Lock)
    run: Future[None] | None = None
    session_id: str | None = None
    #: Every run this process accepted and the sessions among them waiting on the operator.
    futures: dict[str, Future[None]] = field(default_factory=dict)
    parked: set[str] = field(default_factory=set)
    #: The last consolidation job submitted (ticket 247): it is its own job, never ``run``.
    consolidating: Future[object] | None = None
    #: Set when the app is going down: every open request is then denied (decision 75).
    stopped: threading.Event = field(default_factory=threading.Event)

    def start(self, opener: Opener, work: Work) -> str:
        """Submit one run and return its session id once the session exists.

        Raises :class:`RunInFlight` naming the session id in flight when the last run is not done
        and not parked on a permission request (a parked run does not hold the place, decision 75),
        or naming a held one when every thread of the pool is taken, and whatever ``opener``
        raised (an unconfigured provider, a missing key) when the session could not be opened; in
        that case the future has already completed and nothing is held.
        """
        with self.lock:
            if self.run is not None and not self.run.done() and self.session_id not in self.parked:
                raise _in_flight(self.session_id or "unknown")
            live = [sid for sid, future in self.futures.items() if not future.done()]
            if len(live) >= 1 + PARKED_MAX:
                # Every thread is taken: name a parked run, since the active one was refused above.
                raise _in_flight(sorted(live)[0])
            handoff: Future[str] = Future()
            carried: list[Session] = []
            self.run = self.executor.submit(_carry, opener, work, handoff, carried)
            self.run.add_done_callback(lambda _done: self._consolidate(carried))
            self.session_id = handoff.result()
            self.futures[self.session_id] = self.run
            return self.session_id

    def resume_with_message(self, task: dict[str, str | None], prompt: str) -> str:
        """Resume a task paused ``uncertain`` with the operator's ``prompt`` on the worker.

        The one place the control app runs a task (ticket 240, decision 49): ``task_run.run_task``
        with ``author="operator"``, on the session the task already holds, so the message is the
        operator's and ``human_messages`` goes up by one. Refused exactly as :meth:`start` refuses
        any run while another is in flight.
        """
        task_id = str(task["task_id"])
        return self.start(
            lambda opened: task_run.open_task_session(
                task, opened=opened, stopping=self.stopped.is_set, parked=self.park
            ),
            lambda session, event: task_run.run_task(
                task_id,
                first_message=prompt,
                author="operator",
                session=session,
                announced=event,
                reraise=False,
                consolidate=False,
            ),
        )

    def _consolidate(self, carried: list[Session]) -> None:
        """Consolidation's trigger as its own executor job, submitted once the run's future has
        completed (ticket 247, FR-148): the message never waits on it, and it never holds the
        run's place, because ``run`` is the future of the run alone."""
        if not carried:
            return
        with contextlib.suppress(RuntimeError):  # the executor is shutting down: no batch now
            self.consolidating = self.executor.submit(
                run_cmd.consolidate_after_message, carried[0], own_connection=True
            )

    def park(self, session_id: str, parked: bool) -> None:
        """A run's hook: it is waiting on the operator (``True``) or running again (``False``)."""
        if parked:
            self.parked.add(session_id)
        else:
            self.parked.discard(session_id)

    def lifespan(self, _app: object) -> AbstractAsyncContextManager[None]:
        """The app's lifespan: nothing on the way in, ``shutdown(wait=True)`` on the way out."""
        return contextmanager_in_threadpool(self._serving())

    @contextmanager
    def _serving(self) -> Iterator[None]:
        # Leaving the executor's own context is ``executor.shutdown(wait=True)``, on the error
        # path as well as the clean one.
        with self.executor:
            try:
                yield
            finally:
                # Wake every parked run with a deny before waiting on it: a run blocked on the
                # operator would otherwise hold ``shutdown(wait=True)`` for its whole ceiling.
                self.stopped.set()


def _carry(opener: Opener, work: Work, handoff: Future[str], carried: list[Session]) -> None:
    """Open the session, announce it, hand its id back, then run.

    Always settles ``handoff`` exactly once: with the session id from the ``opened`` seam, or with
    the exception that refused the run before it had one. Anything raised after the id was handed
    out is reported on the run's own bus and returns, so the future completes normally.
    """
    announced: list[tuple[Opening, Event]] = []
    with ExitStack() as stack:

        def opened(opening: Opening) -> None:
            stack.callback(opening.conn.close)
            event = run_cmd.announce(opening.bus, opening.settings)
            announced.append((opening, event))
            handoff.set_result(opening.bus.run_id)

        try:
            session = opener(opened)
            carried.append(session)
            _, event = announced[-1]
            work(session, event)
        except Exception as exc:
            if not announced:
                handoff.set_exception(exc)  # the handler re-raises it; nothing is in flight
                return
            run_cmd.settle_raised(announced[0][0].bus, exc)
        except BaseException as exc:
            if not handoff.done():
                handoff.set_exception(exc)
            raise
