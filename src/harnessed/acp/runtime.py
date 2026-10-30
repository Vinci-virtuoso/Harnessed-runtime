"""The only asyncio in the repository: one loop, one daemon thread, one :func:`submit`.

``acp.md`` puts async here and nowhere else, and
``tests/test_acp.py::test_no_module_outside_acp_defines_a_coroutine`` proves it stayed. The
protocol SDK is async and the harness is not, so this module is the whole of the adaptation: a
caller hands over a coroutine factory and a wall-clock ceiling and gets a value or a
:class:`DelegationTimeout` back, on its own thread, with no ``await`` anywhere above it.

The loop is started once, lazily, and reused. One loop rather than one per delegation is what
lets ticket 022's ledger and a future concurrent caller share a single asyncio context instead of
racing several. The thread is a daemon, so a loop with nothing left to do can never hold the
process open at exit.

**The timeout is a ceiling on the caller, not a cancellation of the child.** ``future.cancel()``
cannot interrupt a coroutine blocked on a pipe read from a process that has stopped answering, so
ending the process is what ends the coroutine - and the process belongs to
:mod:`~harnessed.acp.delegate`, which knows its pid.

**The ceiling then waits for the coroutine to stop before it releases the caller** (ticket 060).
``delegate`` hands its kill in as ``on_timeout``; :func:`submit` calls it, waits up to
:data:`SETTLE_GRACE_S`, and only then raises. Killing and reading immediately afterwards is not
enough: ``kill_tree`` returns when the signal is sent, not when the read has failed, so the caller
was reading the delegation's collected state while the loop thread was still writing it - and
persisting the result. If the grace expires the caller is released anyway, with
``DelegationTimeout.settled`` false, because a delegation that will not stop must not become a
hang.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Final, TypeVar

from harnessed.errors import DelegationTimeout

__all__ = ["SETTLE_GRACE_S", "loop_thread", "submit"]

T = TypeVar("T")

#: The one loop and the one thread running it, created on first use under :data:`_LOCK`.
_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_THREAD: threading.Thread | None = None


def loop_thread() -> threading.Thread | None:
    """The thread the background loop runs on, or ``None`` before the first :func:`submit`."""
    return _THREAD


#: How long the ceiling waits for the coroutine to stop after ``on_timeout`` has been called
#: (ticket 060). Killing the child closes the pipe, so a read blocked on it fails almost at once
#: and this bound only binds when something is genuinely wedged - a backstop, not a budget. Five
#: seconds is long enough for a process tree to die on a loaded Windows box and short enough that
#: a caller is never held for a noticeable fraction of a delegation.
SETTLE_GRACE_S: Final = 5.0

#: How often a waiting caller with an ``on_wait`` wakes to do its own work. A quarter second is
#: fast enough that a step reads as live and slow enough that the wakes cost nothing.
POLL_S: Final = 0.25


def submit(
    factory: Callable[[], Coroutine[Any, Any, T]],
    timeout: float,
    *,
    on_timeout: Callable[[], None] | None = None,
    on_wait: Callable[[], None] | None = None,
    pausing: Callable[[], bool] | None = None,
) -> T:
    """Run ``factory()`` on the background loop and return its value within ``timeout`` seconds.

    Anything the coroutine raises reaches the caller unchanged, because a version mismatch and a
    missing agent are errors the tool seam already knows how to render. Only the ceiling itself
    is translated, into :class:`DelegationTimeout`.

    **The ceiling settles the coroutine before it releases the caller** (ticket 060, review-gate
    finding 3). ``future.cancel()`` cannot interrupt a coroutine blocked on a pipe read, so what
    ends it is the caller killing the child - and ``kill_tree`` returns when the signal is sent,
    not when the read has failed. The caller then read the delegation's collected state on its own
    thread while the loop thread was still appending, and persisted the result: that partial
    reaches both the raised error and ``ledger.settle``. A record that differs between identical
    failures is not a record.

    So the ceiling and the settle live together rather than leaving the caller a join it would
    eventually forget. ``on_timeout`` fires the moment the ceiling expires - it is where the caller
    kills the child - and only then is the future waited on, for at most :data:`SETTLE_GRACE_S`.
    Waiting also retrieves the future's exception, which is what stops asyncio logging "exception
    was never retrieved" on every timeout.

    If the grace expires the caller is still released, because a delegation that will not stop must
    not become a hang. :attr:`DelegationTimeout.settled` is ``False`` there and the message says
    so: state read after an unsettled timeout may be torn, and presenting it as final would be this
    function's own defect one level up.

    ``on_wait``, when given, runs on the calling thread every :data:`POLL_S` seconds while the
    coroutine is still going. It is how the delegation publishes each step as it happens without
    ever publishing from the loop thread. The ceiling is the same total either way: the slices are
    subtracted from it rather than read off a clock.

    ``pausing`` stops that subtraction while it answers ``True`` (ticket 234): a delegation waiting
    on the operator's answer is not an agent that produced nothing, so its own ceiling holds still
    for the pending seconds, and the permission ceiling (``Settings.permission_ceiling_s``) bounds
    the pause instead. It is read once per slice, after ``on_wait`` has had its chance to settle it.
    """
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(factory(), loop)
    remaining = timeout
    while True:
        wait = remaining if on_wait is None else min(POLL_S, remaining)
        try:
            return future.result(wait)
        except FutureTimeout as exc:
            if on_wait is not None:
                on_wait()
            if pausing is None or not pausing():
                remaining -= wait
            if on_wait is not None and remaining > 0:
                continue
            raise _ceiling(future, timeout, on_timeout) from exc


def _ceiling(
    future: ConcurrentFuture[T], timeout: float, on_timeout: Callable[[], None] | None
) -> DelegationTimeout:
    """Stop and settle a coroutine that reached its ceiling, and name what happened."""
    if on_timeout is not None:
        on_timeout()
        settled = _settle(future)
    else:
        # No callback means nothing is going to stop the coroutine, so the settle wait would be
        # pure delay - up to SETTLE_GRACE_S of hanging a caller that used to raise immediately.
        # Report unsettled and move on.
        future.cancel()
        settled = False
    message = f"the delegated agent produced nothing within its {timeout}s ceiling"
    if not settled:
        message += (
            f", and did not settle within {SETTLE_GRACE_S}s of being stopped - anything read "
            "from it after this may be incomplete"
        )
    return DelegationTimeout(message, settled=settled)


def _settle(future: ConcurrentFuture[T]) -> bool:
    """Wait out the grace bound for ``future`` to finish. ``True`` when it did.

    The value is discarded and every exception swallowed deliberately: the caller is already
    raising :class:`DelegationTimeout`, and this exists for two side effects - the coroutine has
    stopped writing, and the future's exception has been retrieved rather than left for asyncio to
    complain about at collection.
    """
    try:
        future.exception(SETTLE_GRACE_S)
    except FutureTimeout:
        future.cancel()
        return False
    except BaseException:
        # Retrieval is the point; the value is not. Whatever it raised, it has stopped.
        return True
    return True


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """The shared loop, started on its daemon thread the first time anyone needs it."""
    global _LOOP, _THREAD
    with _LOCK:
        if _LOOP is None:
            _LOOP = asyncio.new_event_loop()
            _THREAD = threading.Thread(target=_LOOP.run_forever, name="harnessed-acp", daemon=True)
            _THREAD.start()
        return _LOOP
