"""The routing suite (FR-118, UJ-21): score the router by handler and by exact route.

``corpus/routing/tasks.yaml`` names each task, the route or routes accepted for it, and where the
task came from (research.md, Appendix A: 20 real operator prompts and 8 written). This module asks
the Decider the same three questions :func:`~harnessed.router.route.decide` asks, over the same
state :func:`~harnessed.router.state.build_state` builds, and scores the answered route twice:
**exact** (the route is one of the accepted routes) and **by handler** (it lands on the same
handler - Groq, an agent or the human - as one of them). Only by handler decides pass or fail;
exact is reported beside it, never folded into it.

**It never reaches the promoter.** Nothing here writes ``eval_runs`` or ``eval_results``, the suite
is not under ``corpus/suites/`` (so neither the runner nor ``improve/`` can load it by name), and
``router/`` is imported by ``cli/`` alone. No ``decisions`` row is written either: a scored task is
not a message anyone sent.

**Which registry** the ``route`` question offers is the caller's: ``agents=None`` offers the
suite's own draft registry (``routes:``), the vocabulary the committed FakeDecider fixtures were
recorded against; the live tier hands in the operator's ``agents.toml`` and asks
:func:`~harnessed.router.questions.build_route_question` for it, exactly as a routed message does.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

# No stub package is pinned by architecture.md, and adding one is a gate decision (MUST rule 9);
# `evals/suite.py` acknowledges the same untyped import.
import yaml  # type: ignore[import-untyped]

from harnessed import shipped
from harnessed.acp.config import AgentSpec
from harnessed.errors import SuiteLoadError
from harnessed.providers.jev import Choice, Decider, Question
from harnessed.router.questions import (
    FIXED_ROUTE_OPTIONS,
    FLOORS,
    MEMORY_KIND_QUESTION,
    NEEDS_MEMORY_QUESTION,
    ROUTE_QUESTION,
    build_route_question,
    route_options,
)
from harnessed.router.state import build_state

__all__ = [
    "DEFAULT_TASKS_PATH",
    "SUITE_NAME",
    "RoutingScore",
    "RoutingSuite",
    "RoutingTask",
    "SuiteLoadError",
    "TaskResult",
    "failure_message",
    "handler_of",
    "json_payload",
    "load_routing_suite",
    "percentile",
    "run_routing",
]

SUITE_NAME: Final = "routing"

#: Found through ``shipped`` (decision 88), never from the working directory.
DEFAULT_TASKS_PATH: Final[Path] = shipped.routing_tasks()

Source = Literal["operator", "written"]
_SOURCES: Final[tuple[str, ...]] = ("operator", "written")

Handler = Literal["groq", "human", "agent"]


@dataclass(frozen=True, slots=True)
class RoutingTask:
    """One task: its authored id, the prompt, every accepted route, and where it came from."""

    id: str
    task: str
    accepted: tuple[str, ...]
    source: Source


@dataclass(frozen=True, slots=True)
class RoutingSuite:
    """The loaded tasks and the draft registry (route name to its ``use_for``) they were labelled
    against, in file order."""

    name: str
    routes: dict[str, str]
    tasks: tuple[RoutingTask, ...]


@dataclass(frozen=True, slots=True)
class TaskResult:
    """One task as scored: the answered route, its handler, both verdicts, time and cost."""

    id: str
    task: str
    accepted: tuple[str, ...]
    route: str
    confidence: Decimal
    handler: Handler
    exact: bool
    by_handler: bool
    seconds: float
    cost: str


@dataclass(frozen=True, slots=True)
class RoutingScore:
    """The whole pass: exact and by-handler counts kept apart, p50/p95 and the cost per decision."""

    registry: Literal["draft", "operator"]
    results: tuple[TaskResult, ...]
    exact: int
    by_handler: int
    total: int
    p50_seconds: float | None
    p95_seconds: float | None
    cost_total: Decimal
    cost_per_decision: Decimal | None

    @property
    def failed(self) -> tuple[TaskResult, ...]:
        """Every task whose answered route landed on a handler no accepted route has."""
        return tuple(result for result in self.results if not result.by_handler)


def handler_of(route: str) -> Handler:
    """The handler a route runs on, as :func:`~harnessed.router.route.decide` assigns it: the
    fixed Groq pair is ``groq``, ``needs_human`` is the ``human`` stop, anything else an agent."""
    if route in FLOORS:
        return "groq"
    if route == FIXED_ROUTE_OPTIONS[-1]:
        return "human"
    return "agent"


# ---- loading ---------------------------------------------------------------------------------


def _fail(path: Path, problem: str) -> SuiteLoadError:
    return SuiteLoadError(f"{path}: {problem}")


def _strings(path: Path, where: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise _fail(path, f"{where}: 'accepted' needs a non-empty list of route names")
    return tuple(value)


def _task(path: Path, entry: object, vocabulary: frozenset[str]) -> RoutingTask:
    if not isinstance(entry, dict):
        raise _fail(path, f"tasks: {entry!r} must be a mapping with id, task, accepted, source")
    task_id, text, source = entry.get("id"), entry.get("task"), entry.get("source")
    if not isinstance(task_id, str) or not task_id.strip():
        raise _fail(path, f"tasks: {entry!r} needs a non-empty string 'id'")
    if not isinstance(text, str) or not text.strip():
        raise _fail(path, f"tasks: {task_id!r} needs a non-empty string 'task'")
    accepted = _strings(path, f"tasks: {task_id!r}", entry.get("accepted"))
    unknown = [route for route in accepted if route not in vocabulary]
    if unknown:
        raise _fail(
            path,
            f"tasks: {task_id!r} accepts {', '.join(unknown)}, which is no route this suite "
            f"offers ({', '.join(sorted(vocabulary))})",
        )
    if source not in _SOURCES:
        raise _fail(path, f"tasks: {task_id!r} 'source' must be one of {', '.join(_SOURCES)}")
    return RoutingTask(id=task_id, task=text, accepted=accepted, source=source)


def load_routing_suite(path: Path = DEFAULT_TASKS_PATH) -> RoutingSuite:
    """``tasks.yaml`` into a :class:`RoutingSuite`, validated eagerly: a malformed file raises
    :class:`~harnessed.errors.SuiteLoadError` naming the file and the field."""
    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _fail(path, f"cannot be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise _fail(path, f"is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise _fail(path, "must be a mapping with suite, routes and tasks")
    name = document.get("suite")
    if name != SUITE_NAME:
        raise _fail(path, f"suite: must be {SUITE_NAME!r}, not {name!r}")
    routes = document.get("routes")
    if not isinstance(routes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in routes.items()
    ):
        raise _fail(path, "routes: must map each draft route name to its use_for text")
    raw_tasks = document.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise _fail(path, "tasks: must be a non-empty list")
    vocabulary = frozenset((*FIXED_ROUTE_OPTIONS, *routes))
    tasks = tuple(_task(path, entry, vocabulary) for entry in raw_tasks)
    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise _fail(path, f"tasks: duplicate id {', '.join(duplicates)}")
    return RoutingSuite(name=SUITE_NAME, routes=dict(routes), tasks=tasks)


# ---- scoring ---------------------------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """The nearest-rank percentile of ``values``, or ``None`` for none."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _route_question(suite: RoutingSuite, agents: Mapping[str, AgentSpec] | None) -> Question:
    if agents is not None:
        return build_route_question(agents)
    options = (*FIXED_ROUTE_OPTIONS[:-1], *suite.routes, FIXED_ROUTE_OPTIONS[-1])
    return Question(
        name="route",
        kind="choice",
        options=options,
        instructions=ROUTE_QUESTION.instructions,
        criteria={**ROUTE_QUESTION.criteria, **suite.routes},
    )


def _widen_accepted(
    accepted: tuple[str, ...], agents: Mapping[str, AgentSpec] | None
) -> tuple[str, ...]:
    """Ticket 196 (FR-102): with the operator's registry, a task whose accepted routes name only
    agent entries and none of them is offered also accepts `groq_chat` - the operator's registry
    may have neither `email_writer` nor `wireframe_designer` (ticket 178). ``agents=None`` (the
    draft registry) never widens: the offline scores stay as recorded."""
    if agents is None:
        return accepted
    if any(route in FIXED_ROUTE_OPTIONS for route in accepted):
        return accepted
    if any(route in route_options(agents) for route in accepted):
        return accepted
    return (*accepted, "groq_chat")


def run_routing(
    suite: RoutingSuite,
    decider: Decider,
    conn: sqlite3.Connection,
    *,
    agents: Mapping[str, AgentSpec] | None = None,
    timer: Callable[[], float] = time.perf_counter,
) -> RoutingScore:
    """Ask every task once, in file order, and score the answers (FR-118).

    ``conn`` is read, never written: the state's operator and memory digest comes from it exactly
    as a routed message's does. A :class:`~harnessed.errors.DecisionError` propagates - a router
    that cannot answer has no score to report, and a fallback is not a route.
    """
    questions = (_route_question(suite, agents), NEEDS_MEMORY_QUESTION, MEMORY_KIND_QUESTION)
    results = []
    for task in suite.tasks:
        state = build_state(conn, task.task)
        started = timer()
        answered = decider.decide(state.text, questions)
        seconds = timer() - started
        route_answer = answered.answers["route"]
        assert isinstance(route_answer, Choice)
        handler = handler_of(route_answer.value)
        accepted = _widen_accepted(task.accepted, agents)
        results.append(
            TaskResult(
                id=task.id,
                task=task.task,
                accepted=accepted,
                route=route_answer.value,
                confidence=route_answer.confidence,
                handler=handler,
                exact=route_answer.value in accepted,
                by_handler=handler in {handler_of(route) for route in accepted},
                seconds=seconds,
                cost=answered.cost,
            )
        )
    costs = sum((Decimal(result.cost) for result in results), Decimal(0))
    timings = [result.seconds for result in results]
    return RoutingScore(
        registry="draft" if agents is None else "operator",
        results=tuple(results),
        exact=sum(result.exact for result in results),
        by_handler=sum(result.by_handler for result in results),
        total=len(results),
        p50_seconds=percentile(timings, 0.50),
        p95_seconds=percentile(timings, 0.95),
        cost_total=costs,
        cost_per_decision=costs / len(results) if results else None,
    )


def failure_message(score: RoutingScore) -> str:
    """Every by-handler miss, named by task id and text, with the route answered and accepted."""
    named = "; ".join(
        f"{result.id} ({result.task!r}) answered {result.route}, "
        f"accepted {', '.join(result.accepted)}"
        for result in score.failed
    )
    return (
        f"the routing suite failed by handler on {len(score.failed)} of {score.total} "
        f"task(s): {named}"
    )


def _seconds(value: float | None) -> str | None:
    return None if value is None else f"{value:.3f}"


def json_payload(score: RoutingScore) -> dict[str, object]:
    """The one payload the CLI prints, as JSON or as rows: exact and by handler kept apart."""
    return {
        "suite": SUITE_NAME,
        "registry": score.registry,
        "by_handler": {"passed": score.by_handler, "total": score.total},
        "exact": {"passed": score.exact, "total": score.total},
        "latency_p50_seconds": _seconds(score.p50_seconds),
        "latency_p95_seconds": _seconds(score.p95_seconds),
        "cost_per_decision": (
            None if score.cost_per_decision is None else f"{score.cost_per_decision:.9f}"
        ),
        "cost_total": str(score.cost_total),
        "failed": [result.id for result in score.failed],
        "results": [
            {
                "id": result.id,
                "task": result.task,
                "accepted": list(result.accepted),
                "route": result.route,
                "handler": result.handler,
                "confidence": str(result.confidence),
                "exact": result.exact,
                "by_handler": result.by_handler,
                "seconds": f"{result.seconds:.3f}",
                "cost": result.cost,
            }
            for result in score.results
        ],
    }
