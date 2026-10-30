"""FR-71: no handler calls two packages, and no route returns two packages' payloads (ticket 130).

Decision 41 keeps decision 38 true. An owning package may count, sum and group over the tables
it owns; what is forbidden is a **handler** composing. This file makes that a test rather than a
habit, and it is generic: it walks every route the app registers under ``/v1``, reads each
handler's source by AST, and resolves every call against the handler's own module globals. A
route that phases C and H add (``/v1/spend``, ``/v1/prices``, ``/v1/providers``, ``/v1/agents``)
is scanned the day it is registered, without an edit here.

A call's **package** is the second segment of its module under ``harnessed`` (``harnessed.events.
read`` is ``events``). Three kinds of call are not a package: the gateway's own helpers (``found``,
``parse_limit``, ``required``), the standard library and builtins (``asdict``, ``list``), and the
shared infrastructure every package stands on, which owns no payload (the top-level modules such
as ``harnessed.serialize`` and ``harnessed.errors``, and ``memory/db.py``'s connection layer). A
call into another gateway module is followed into that module's own calls, so the stream handler,
which decision 24 routes through ``gateway/sse.py``, is charged with the ``events`` package that
``sse.py`` reads and nothing is hidden behind a gateway indirection.

The rule is shown able to fire, the pattern ticket 114 set: a seeded ``/v1/overview`` handler that
calls ``events`` and ``memory`` is registered on a real app and the same scan reports it on both
counts. Decision 37's arithmetic scan runs over the same handlers, with annotations and decorators
lifted off first (a ``str | None`` is an ``ast.BinOp`` and ``@app.get(...)`` is an ``ast.Call``,
neither of which is the handler doing anything, ticket 126).
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import textwrap
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI

from harnessed.events import read
from harnessed.gateway import create_app, deps
from harnessed.memory import db, totals

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "130"

PREFIX = "/v1"
ROUTES_MODULE = "harnessed.gateway.routes"
GATEWAY_MODULE = "harnessed.gateway"

#: Infrastructure every package stands on, owning no table's payload: the connection layer.
INFRASTRUCTURE_MODULES = frozenset({"harnessed.memory.db"})

#: The one handler decision 24 routes through a gateway module rather than a store callable.
DELEGATING_PATHS = frozenset({"/v1/sessions/{session_id}/stream"})

#: Decision 41's phase B handlers, each one store read plus its emitter. ``/v1/spend``, the sixth
#: callable decision 41 names, is phase C's; the generic walk takes it when it is registered.
PHASE_B_PATHS = frozenset(
    {
        "/v1/runs/totals",
        "/v1/runs/turn_seconds",
        "/v1/events/gate",
        "/v1/events/totals",
        "/v1/memory/totals",
    }
)


@dataclass(frozen=True)
class Call:
    """One call a handler makes, resolved: its spelling, its module and its package, if any."""

    spelled: str
    module: str
    package: str | None
    returned: bool

    @property
    def is_emitter(self) -> bool:
        return self.spelled.rsplit(".", 1)[-1].endswith("_payload")


@dataclass(frozen=True)
class Report:
    """What one handler does, and every way it breaks FR-71."""

    path: str
    handler: str
    calls: tuple[Call, ...]
    followed: tuple[Call, ...]
    violations: tuple[str, ...]

    @property
    def packages(self) -> frozenset[str]:
        return frozenset(c.package for c in (*self.calls, *self.followed) if c.package)

    @property
    def returned_packages(self) -> frozenset[str]:
        every = (*self.calls, *self.followed)
        return frozenset(c.package for c in every if c.package and c.returned)

    def line(self) -> str:
        spelled = ", ".join(c.spelled for c in self.calls if c.package) or "-"
        via = ", ".join(sorted({c.spelled for c in self.followed if c.package}))
        packages = ",".join(sorted(self.packages)) or "-"
        verdict = "ok" if not self.violations else "; ".join(self.violations)
        tail = f" via gateway: {via}" if via else ""
        head = f"{self.path}  [{self.handler}]  package={packages}"
        return f"{head}  calls={spelled}{tail}  {verdict}"


def package_of(module: str) -> str | None:
    """The owning package of ``module``, or ``None`` for anything that owns no payload."""
    parts = module.split(".")
    if parts[0] != "harnessed" or len(parts) < 3:
        return None
    if module in INFRASTRUCTURE_MODULES or parts[1] == "gateway":
        return None
    return parts[1]


def scanned(func: Callable[..., Any]) -> ast.AST:
    """The tree of what ``func`` *does*: annotations, returns and decorators lifted off."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node.returns = None
            node.decorator_list = []
        elif isinstance(node, ast.arg | ast.AnnAssign):
            node.annotation = None  # type: ignore[assignment]
    return tree


def resolve(func_node: ast.expr, scope: dict[str, Any]) -> tuple[str, Any] | None:
    """The module a called expression lives in, and the object itself; ``None`` for a local."""
    if isinstance(func_node, ast.Name):
        target = scope.get(func_node.id, getattr(__builtins__, func_node.id, None))
        if target is None and isinstance(__builtins__, dict):
            target = __builtins__.get(func_node.id)
        if target is None:
            return None
        return getattr(target, "__module__", None) or "builtins", target
    if isinstance(func_node, ast.Attribute) and isinstance(func_node.value, ast.Name):
        base = scope.get(func_node.value.id)
        if isinstance(base, types.ModuleType):
            return base.__name__, getattr(base, func_node.attr, None)
    return None


def returned_names(tree: ast.AST) -> set[str]:
    """Every name a ``return`` reads, plus the names those names were assigned from."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None:
            names |= {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets
            ):
                fresh = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)} - names
                if fresh:
                    names |= fresh
                    grew = True
    return names


def calls_of(func: Callable[..., Any], seen: set[str]) -> Iterator[Call]:
    """Every call ``func`` makes, resolved, and the calls of any gateway module it reaches."""
    tree = scanned(func)
    scope = dict(func.__globals__)
    feeding = returned_names(tree)
    returned_calls = {
        id(n)
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and node.value is not None
        for n in ast.walk(node.value)
        if isinstance(n, ast.Call)
    }
    returned_calls |= {
        id(n)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id in feeding for t in node.targets)
        for n in ast.walk(node.value)
        if isinstance(n, ast.Call)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = resolve(node.func, scope)
        if resolved is None:
            continue
        module, target = resolved
        yield Call(ast.unparse(node.func), module, package_of(module), id(node) in returned_calls)
        followable = (
            module.startswith(GATEWAY_MODULE)
            and not module.startswith(ROUTES_MODULE)
            and inspect.isfunction(target)
        )
        if followable and target.__qualname__ not in seen:
            seen.add(target.__qualname__)
            yield from calls_of(target, seen)


def report_for(path: str, endpoint: Callable[..., Any]) -> Report:
    tree = scanned(endpoint)
    direct: list[Call] = []
    followed: list[Call] = []
    seen: set[str] = {endpoint.__qualname__}
    own = list(calls_of(endpoint, seen))
    direct_spellings = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for call in own:
        (direct if call.spelled in direct_spellings else followed).append(call)

    violations: list[str] = []
    owned = [c for c in direct if c.package]
    packages = {c.package for c in (*direct, *followed) if c.package}
    if len(packages) != 1:
        violations.append(f"calls {len(packages)} packages {sorted(p for p in packages if p)}")
    if path not in DELEGATING_PATHS:
        stores = {c.spelled for c in owned if not c.is_emitter}
        emitters = {c.spelled for c in owned if c.is_emitter}
        if not owned:
            violations.append("calls no store callable")
        if len(stores) > 1:
            violations.append(f"calls {len(stores)} store callables {sorted(stores)}")
        if len(emitters) > 1:
            violations.append(f"calls {len(emitters)} emitters {sorted(emitters)}")
    returned = {c.package for c in (*direct, *followed) if c.package and c.returned}
    if len(returned) > 1:
        violations.append(f"returns {len(returned)} packages' payloads {sorted(returned)}")
    return Report(path, endpoint.__name__, tuple(direct), tuple(followed), tuple(violations))


def arithmetic_in(endpoint: Callable[..., Any]) -> list[str]:
    """Decision 37's scan: every comparison and every arithmetic operation the handler spells."""
    return [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(scanned(endpoint))
        if isinstance(node, ast.BinOp | ast.AugAssign | ast.UnaryOp | ast.Compare)
    ]


def handlers(application: FastAPI) -> list[tuple[str, Callable[..., Any]]]:
    """Every ``/v1`` route whose handler lives under ``gateway/routes/**``, in registered order."""
    found = []
    for route in application.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if not path.startswith(PREFIX) or endpoint is None:
            continue
        if endpoint.__module__.startswith(ROUTES_MODULE + ".") or path == f"{PREFIX}/overview":
            found.append((path, endpoint))
    return found


def v1_paths(application: FastAPI) -> list[str]:
    return [p for r in application.routes if (p := getattr(r, "path", "")).startswith(PREFIX)]


@pytest.fixture
def app(tmp_harnessed_home: Path) -> FastAPI:
    return create_app(tmp_harnessed_home / db.DATABASE_FILENAME)


def write_evidence(name: str, lines: list[str]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- AC 1: every handler calls one callable of one package, plus its emitter -------------------


def test_every_handler_under_routes_calls_exactly_one_package(app: FastAPI) -> None:
    reports = [report_for(path, endpoint) for path, endpoint in handlers(app)]
    write_evidence("handler-scan.txt", [r.line() for r in reports])
    assert len(reports) >= 20, f"the walk found only {len(reports)} handlers"
    broken = [r.line() for r in reports if r.violations]
    assert broken == [], "\n".join(broken)
    assert all(len(r.packages) == 1 for r in reports)


def test_the_walk_takes_every_v1_route_but_health(app: FastAPI) -> None:
    """Nothing escapes the scan by living outside ``gateway/routes/**`` except health."""
    scanned_paths = {path for path, _ in handlers(app)}
    assert set(v1_paths(app)) - scanned_paths == {f"{PREFIX}/health"}


def test_the_stream_is_charged_with_the_package_sse_reads(app: FastAPI) -> None:
    """The one delegating handler hides nothing: ``sse.py`` is followed into ``events``."""
    (endpoint,) = (e for p, e in handlers(app) if p in DELEGATING_PATHS)
    report = report_for("/v1/sessions/{session_id}/stream", endpoint)
    assert report.packages == {"events"}
    assert {c.spelled for c in report.followed if c.package} >= {"read.after", "read.latest"}


def test_the_phase_b_handlers_are_one_store_read_and_its_emitter(app: FastAPI) -> None:
    by_path = dict(handlers(app))
    assert set(by_path) >= PHASE_B_PATHS
    for path in sorted(PHASE_B_PATHS):
        report = report_for(path, by_path[path])
        owned = [c for c in report.calls if c.package]
        assert len(owned) == 2, f"{path}: {[c.spelled for c in owned]}"
        assert sum(c.is_emitter for c in owned) == 1, path
        assert report.violations == (), f"{path}: {report.violations}"


# ---- AC 2: a seeded two-package handler fails --------------------------------------------------


def seeded_overview(
    conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
) -> dict[str, object]:
    """The composition decision 41 refuses: two packages' payloads under one route."""
    events = read.totals_payload(read.totals(conn))
    memory = totals.totals_payload(totals.totals(conn))
    return {"events": events, "memory": memory}


def test_a_seeded_two_package_handler_fails_the_scan(app: FastAPI) -> None:
    app.get(f"{PREFIX}/overview")(seeded_overview)
    reports = {path: report_for(path, e) for path, e in handlers(app)}
    report = reports[f"{PREFIX}/overview"]
    write_evidence("seeded-two-package-handler.txt", [report.line()])
    assert report.packages == {"events", "memory"}
    assert any(v.startswith("calls 2 packages") for v in report.violations)
    assert any(v.startswith("calls 2 store callables") for v in report.violations)
    assert any(v.startswith("returns 2 packages' payloads") for v in report.violations)
    assert [p for p, r in reports.items() if r.violations] == [f"{PREFIX}/overview"]


def test_a_seeded_handler_calling_two_callables_of_one_package_fails(app: FastAPI) -> None:
    """One package is not enough: two store reads composed is a handler composing."""

    def doubled(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
    ) -> dict[str, object]:
        return {"a": read.totals_payload(read.totals(conn)), "b": read.gate_split(conn)}

    report = report_for(f"{PREFIX}/doubled", doubled)
    assert report.packages == {"events"}
    assert any(v.startswith("calls 2 store callables") for v in report.violations)


# ---- AC 3: no route returns two packages' payloads, and there is no /v1/overview ---------------


def test_no_v1_route_returns_more_than_one_package_payload(app: FastAPI) -> None:
    reports = [report_for(path, endpoint) for path, endpoint in handlers(app)]
    over = [r.path for r in reports if len(r.returned_packages) > 1]
    assert over == []
    assert all(len(r.returned_packages) == 1 for r in reports), [
        (r.path, sorted(r.returned_packages)) for r in reports if len(r.returned_packages) != 1
    ]


def test_there_is_no_overview_route(app: FastAPI) -> None:
    """Decision 34: the Overview is six panels over six routes, never one composed route."""
    assert not [p for p in v1_paths(app) if "overview" in p]


# ---- AC 4: decision 37's arithmetic scan covers every handler, the new ones included -----------


def test_no_handler_spells_arithmetic_or_a_comparison(app: FastAPI) -> None:
    found = {path: arithmetic_in(endpoint) for path, endpoint in handlers(app)}
    assert set(found) >= PHASE_B_PATHS
    write_evidence(
        "arithmetic-scan.txt", [f"{p}  {len(ops)} operations" for p, ops in found.items()]
    )
    assert {p: ops for p, ops in found.items() if ops} == {}


def test_the_arithmetic_scan_is_not_vacuous() -> None:
    def computing(
        conn: Annotated[sqlite3.Connection, Depends(deps.connection)],
        limit: str | None = None,
    ) -> dict[str, object]:
        counted = read.totals(conn)
        return {"ratio": counted.events / (counted.sessions or 1)}

    assert len(arithmetic_in(computing)) == 1
