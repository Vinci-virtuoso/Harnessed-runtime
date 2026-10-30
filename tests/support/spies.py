"""Spies the assertions read: two for the event bus (FR-21), one for the provider seam (FR-7).

``RecordingObserver`` is the observer FR-21's ordered-sequence assertion reads: it keeps every
event it was handed, in order, and exposes the subsequence of names because the assertion is
about names and never about payloads. ``RaisingObserver`` is its counterpart for the isolation
claim - it fails on demand so a test can prove the run is unaffected.

``CountingAssembler`` is FR-11's counterpart: "assembled once per run" is a claim about a
number, so the spy wraps ``memory.retrieval.assemble`` and counts, leaving the assembled context
itself untouched so the run under measurement is the real one.

``SpawnSpy`` is FR-23's counter. "The agent is spawned exactly once for a task hash" is a
claim about *process creations*, so the spy wraps ``subprocess.Popen`` and records the command
line of every real spawn - a spy that counted method calls would go green against a client that
never started a process at all. ``watch_spawns`` is the same spy pointed at every path
``asyncio`` might take to make a process, which is not one path (see its docstring).

``ForbiddenProvider`` is FR-20's counterpart, and it is the inverse of a counter: the promotion
path must never reach a model, so the spy fails the test on the first call rather than recording
one. It refuses to be *constructed* as well, because a promotion path that built a provider and
then did not call it would still have crossed the boundary the promoter exists to keep.

``CountingProvider`` is what FR-7's resume claim is measured with. "Resuming issues zero provider
calls for the recorded iterations" is a claim about a number, so the spy counts, and its ``probe``
hook is where a test either inspects the world at the top of a turn or interrupts the run.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

from harnessed.events.bus import Event
from harnessed.providers.types import ChatRequest, ChatResponse

__all__ = [
    "CountingAssembler",
    "CountingProvider",
    "ForbiddenProvider",
    "RaisingObserver",
    "RecordingObserver",
    "SpawnSpy",
    "is_subsequence",
    "watch_spawns",
]


@dataclass(slots=True)
class RecordingObserver:
    """Keep every event, in publication order. The spy FR-21's sequence assertion reads."""

    events: list[Event] = field(default_factory=list)

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    @property
    def names(self) -> list[str]:
        """The subsequence of event names, in order."""
        return [event.name for event in self.events]

    def named(self, name: str) -> list[Event]:
        """Every recorded event published under ``name``."""
        return [event for event in self.events if event.name == name]


@dataclass(slots=True)
class RaisingObserver:
    """Fail on every invocation, counting the calls so isolation can be told from silence."""

    message: str = "observer exploded"
    calls: int = 0

    def __call__(self, event: Event) -> None:
        self.calls += 1
        raise RuntimeError(self.message)


def is_subsequence(expected: Sequence[str], actual: Iterable[str]) -> bool:
    """True when ``expected`` appears inside ``actual`` in order, gaps allowed."""
    remaining = iter(actual)
    return all(any(candidate == name for candidate in remaining) for name in expected)


@dataclass
class CountingProvider:
    """Replay a script, count the calls, and let a test act at the top of every turn.

    The script repeats its tail, so a one-entry script says "and from here on the model always
    answers this way". ``probe`` is handed the number of this call, one-based, before the
    response is returned: a test that asserts the journal already holds the previous turn does it
    there, and a test that needs the run to die mid-flight raises there.
    """

    script: list[ChatResponse]
    seen: list[ChatRequest] = field(default_factory=list)
    probe: Callable[[int], None] | None = None

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.seen.append(request)
        if self.probe is not None:
            self.probe(len(self.seen))
        return self.script[min(len(self.seen) - 1, len(self.script) - 1)]

    @property
    def calls(self) -> int:
        """How many times the model was actually asked. FR-7's resume claim reads this."""
        return len(self.seen)


@dataclass
class CountingAssembler:
    """Call ``inner`` through, counting the calls. FR-11's once-per-run assertion reads this.

    It wraps rather than replaces, so the run it measures assembles the same context it would
    have assembled unwatched: a spy that returned a stand-in would be measuring itself.
    """

    inner: Callable[..., Any]
    calls: int = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.inner(*args, **kwargs)


@dataclass
class SpawnSpy:
    """Wrap ``subprocess.Popen``, count the real spawns, and remember each command line.

    It wraps rather than replaces, so the process under measurement is the real one: a spy that
    returned a stand-in would be counting itself. ``count`` is every spawn that happened while
    it was installed, including any the teardown made; ``matching`` is the filter a test uses
    when it means "how many times was *this* agent started".
    """

    inner: Callable[..., Any] = subprocess.Popen
    commands: list[list[str]] = field(default_factory=list)

    def __call__(self, args: Any, *rest: Any, **kwargs: Any) -> Any:
        self.commands.append([str(part) for part in args])
        return self.inner(args, *rest, **kwargs)

    @property
    def count(self) -> int:
        """How many processes were actually created. FR-23's spawn-once claim reads this."""
        return len(self.commands)

    def matching(self, fragment: str) -> list[list[str]]:
        """Every recorded command line containing ``fragment`` in one of its parts."""
        return [command for command in self.commands if any(fragment in part for part in command)]

    @contextmanager
    def installed(self, target: Any, name: str = "Popen") -> Iterator[SpawnSpy]:
        """Stand in for ``target.name`` for the duration of the block, then put it back."""
        original = getattr(target, name)
        setattr(target, name, self)
        try:
            yield self
        finally:
            setattr(target, name, original)


@contextmanager
def watch_spawns() -> Iterator[SpawnSpy]:
    """Count real process creations on every path ``asyncio`` might take to make one.

    ``asyncio.create_subprocess_exec`` does not reach ``subprocess.Popen`` through the
    ``subprocess`` module on Windows: the proactor transport builds an
    ``asyncio.windows_utils.Popen``, a subclass bound at import time, so a spy installed on
    ``subprocess`` alone records zero spawns and every "spawned exactly once" claim passes
    vacuously. One spy is installed per platform target, all of them sharing the returned spy's
    command list, so ``count`` and ``matching`` read the same total whichever path was taken.
    """
    watcher = SpawnSpy()
    targets: list[tuple[Any, str]] = [(subprocess, "Popen")]
    if sys.platform == "win32":
        from asyncio import windows_utils

        targets.append((windows_utils, "Popen"))
    with ExitStack() as stack:
        for module, name in targets:
            spy = SpawnSpy(inner=getattr(module, name), commands=watcher.commands)
            stack.enter_context(spy.installed(module, name))
        yield watcher


@dataclass
class ForbiddenProvider:
    """Fail the test on any call, and on any attempt to build one. FR-20's spy.

    It satisfies the same one-method Protocol every adapter satisfies, so the code under test
    cannot tell it apart from a real provider until it calls it - and calling it is the failure.
    ``AssertionError`` rather than a ``HarnessError`` on purpose: a provider call from the
    promotion path is not a runtime condition to handle, it is a broken invariant, and dressing
    it as a handled error would let a caller catch it.
    """

    seen: list[ChatRequest] = field(default_factory=list)
    builds: int = 0

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.seen.append(request)
        raise AssertionError(
            "the promotion path reached a provider. FR-20: the promoter contains zero model "
            "calls, forever."
        )

    def refuse_to_build(self, *args: Any, **kwargs: Any) -> ForbiddenProvider:
        """Stand in for ``providers.registry.create``. Constructing one is already too far."""
        self.builds += 1
        raise AssertionError(
            "the promotion path built a provider. FR-20: the promoter has nothing to reach a "
            "model with, so nothing on this path may construct one."
        )

    @property
    def calls(self) -> int:
        """How many times a model was asked. The promotion path's answer is always zero."""
        return len(self.seen)
