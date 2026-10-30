"""Boundaries: the one-way import graph of ``src/harnessed``, asserted from the AST.

The architecture's "Structure" section draws the import direction one way and names this file as
its enforcement. It supports FR-5, FR-8, FR-20 and FR-22 without owning any of them, which is why
it is a ticket of its own: an assertion that constrains eight packages cannot live inside any one
of them.

The scan is static. Every import statement in the package is resolved against the modules that
exist on disk, so an import inside a function body - the shape a module reaches for when it wants
an edge the top of the file would make obvious - is caught exactly like one in the header. Runtime
introspection would miss it, and would only see the edges a particular execution happened to walk.

Every rule here runs twice: once over the real package, where it must be silent, and once over a
synthetic package written to ``tmp_path`` that breaks it, where it must speak and name both ends of
the offending edge. An enforcement module that only runs against source which already obeys is a
row of assertions that would pass against anything (the lesson ticket 012 recorded).

Three edges below are carve-outs rather than the drawing, each one recorded by the ticket that made
it at a time when ``architecture.md`` sat in that ticket's ``scope_out``:

* ``events/bus.py -> memory.db, memory.redact``. The table draws ``events -> config``, but the same
  document maps FR-13 onto the bus (every publish is redacted) and FR-21 onto ``001_init.sql``
  (every publish is a row). Ticket 009 recorded the crossing and left the note that this file
  should copy its enumeration; ``tests/test_events.py`` pins the same two modules from the other
  side.
* ``memory/gate.py -> providers.types``. FR-12's gate consults a model, so it needs the normalized
  request and response types. Ticket 015 restated the one-method provider seam as its own
  ``Protocol`` rather than importing ``providers.base``, which is what keeps the crossing at
  exactly one module under ``providers/``.

A third, ``memory/consolidation.py -> providers.types, providers.translate``, was recorded by
ticket 247 (decision 81): the batch's request and the one parser of its reply.

Each is pinned to the single importing module and the exact target set, so neither can widen to
its package, and each is asserted to still be needed - a carve-out nothing uses is one to delete.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from harnessed.providers.jev import DECISIONS_URL

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"

#: The five leaves plus the root package. Every module may reach them; the direction test below
#: asserts they reach nothing themselves, so they cannot be a path back up the graph.
LEAF_MODULES = frozenset(
    {
        "harnessed",
        "harnessed.clock",
        "harnessed.config",
        "harnessed.errors",
        "harnessed.host",
        "harnessed.ids",
        "harnessed.proc",
        "harnessed.serialize",
        "harnessed.shipped",
    }
)

#: The command modules the control app calls directly (decision 49, MUST rule 16). The one edge
#: into `cli` from outside it, pinned to modules rather than the package.
CONTROL_REACHES_CLI: tuple[str, ...] = (
    "harnessed.cli.onboarding",
    "harnessed.cli.run_cmd",
    # Ticket 240 (FR-141): a message on a task's session resumes it through `run_task`.
    "harnessed.cli.task_run",
)

#: The import table from the architecture's "Structure" section, one row per area. An entry is a
#: module prefix: ``harnessed.memory`` admits the package, ``harnessed.providers.types`` admits
#: exactly that module. The leaves are implicit everywhere and are not restated per row.
IMPORT_TABLE: dict[str, tuple[str, ...]] = {
    # `cli` imports everything and is imported by nothing. Only the second half is assertable
    # today: the command surface lands in ticket 028, so "imports everything" would be a claim
    # about source that does not exist yet. `test_nothing_imports_cli` carries the direction.
    "cli": ("harnessed",),
    # The one process of decision 43: the export at `/`, the read gateway mounted at `/v1`. It
    # names the database file through `memory.db` and is imported only by `cli` (FR-65). Ticket
    # 136 mounts the control app beside the gateway, so it names both, and neither names the other.
    "dashboard_app": ("harnessed.control", "harnessed.gateway", "harnessed.memory.db"),
    # The write surface (FR-77, decision 49). It is a second caller of the CLI's composition, never
    # a second composition, so it reaches exactly the command modules that own one: `run_cmd`
    # today, `cli/onboarding.py` when ticket 145 creates it. Never `harnessed.gateway`: the read
    # surface stays literally unable to write, and nothing that writes is its neighbour's import.
    # Ticket 234 (FR-130): `POST /control/v1/permissions/{id}` is `acp/pending.py::operator_answer`,
    # the one call `harnessed permissions answer` also makes, and the worker reads its
    # `PARKED_MAX`. The module and nothing else under `acp/`; the run lease is `run_cmd`'s.
    # Ticket 239 (FR-140): `POST /control/v1/tasks` is `tasks/store.py::queue_text`, the door onto
    # the `queue` that `harnessed task add` also calls.
    "control": (*CONTROL_REACHES_CLI, "harnessed.acp.pending", "harnessed.tasks.store"),
    # The read surface (FR-31). It renders the stores and reads nothing above them; the arrow
    # points one way, into it: no row below names `harnessed.gateway`, and only `cli` may.
    "gateway": (
        "harnessed.evals",
        "harnessed.improve",
        "harnessed.memory",
        "harnessed.loop",
        "harnessed.events",
        "harnessed.config",
        # FR-73 (ticket 132): `GET /v1/prices` renders `providers/pricing.py::stored`, the module
        # that owns the prices table. The module and nothing else under `providers/`; that the
        # gateway never names its `fetch` is `tests/test_pricing.py`'s scan.
        "harnessed.providers.pricing",
        # FR-89 (ticket 147): `GET /v1/providers` renders `providers/registry.py::states`, the
        # module that owns the provider table (decision 55).
        "harnessed.providers.registry",
        # FR-90 (ticket 148): `GET /v1/agents` renders `acp/probes.py::states`, the module that
        # owns the `agent_probes` table. The module and nothing else under `acp/`: the gateway
        # reads a recorded probe and never reaches the runtime or the client that spawn one.
        "harnessed.acp.probes",
        # FR-120 (ticket 180): `GET /v1/sessions/{id}/decisions` renders
        # `router/decisions.py::for_session`, the module that owns `decisions` and
        # `decision_options`. The module and nothing else under `router/`: the gateway reads
        # recorded decisions and never builds one (MUST rule 30, decision 67).
        "harnessed.router.decisions",
        # FR-127 (ticket 181): `GET /v1/outputs/{output_id}` renders `acp/outputs.py::body`, the
        # module that owns the `outputs` table. The module and nothing else under `acp/`: the
        # gateway reads a stored output's bytes and never collects, scrubs or writes one
        # (decision 65's second named exception, `fastapi.md`).
        "harnessed.acp.outputs",
        # FR-130 (ticket 234): `GET /v1/permissions` renders `acp/pending.py::listing`, the module
        # that owns `permission_decisions`. The gateway reads decisions and never answers one.
        "harnessed.acp.pending",
        # FR-143 (ticket 241): `GET /v1/tasks` renders `tasks/store.py::listing`, the module that
        # owns `tasks`. The module and nothing else under `tasks/`: the gateway reads the ledger
        # and never queues, runs or cancels a task.
        "harnessed.tasks.store",
        # FR-142 (ticket 242): `GET /v1/schedules` renders `tasks/schedule.py::listing`, the module
        # that owns `schedules` and `schedule_installs`; it never reaches `tasks/installer.py`.
        "harnessed.tasks.schedule",
    ),
    "improve": (
        "harnessed.evals",
        "harnessed.memory",
        "harnessed.providers.types",
        "harnessed.events",
    ),
    "evals": (
        "harnessed.loop",
        "harnessed.memory",
        "harnessed.providers.types",
        "harnessed.events",
    ),
    "loop": (
        "harnessed.tools",
        "harnessed.providers.types",
        "harnessed.memory",
        "harnessed.events",
    ),
    "tools": ("harnessed.acp", "harnessed.providers.types", "harnessed.events"),
    # Decision 56: the router sits in the composition, before `solve`, and only the operator's
    # doors hold one. Reached from `cli/` alone (a scan below pins the direction); nothing under
    # `router/` but `decisions.py` exists yet (ticket 156, FR-97) - the rest of this row is
    # decision 56's whole plan, admitted ahead of the tickets that use it.
    "router": (
        "harnessed.providers.jev",
        "harnessed.acp.config",
        "harnessed.acp.ledger",
        "harnessed.memory",
        "harnessed.loop.prompt",
        "harnessed.events",
        "harnessed.config",
    ),
    "acp": ("harnessed.events", "harnessed.memory", "harnessed.config"),
    "memory": ("harnessed.events", "harnessed.config"),
    "providers": ("harnessed.config",),
    "events": ("harnessed.config",),
    # The leaves and the root package, which re-exports them and reaches nothing above.
    "clock": (),
    "config": (),
    "errors": (),
    "ids": (),
    "serialize": (),
    # The process seam (decision 87): pid identity and liveness, a leaf every package may reach.
    "proc": (),
    # The host seam (decision 95): OS and Python version, and the one `SeamLine` every seam returns.
    "host": (),
    # The run lease (decision 78) and, from ticket 240, tasks: today it reaches only leaves.
    # Ticket 239 (FR-140): the ledger reads and writes memory and publishes through events; it
    # never reaches loop, acp or router (cancel is handed the denial it needs).
    "tasks": ("harnessed.memory", "harnessed.events", "harnessed.config"),
    "harnessed": (),
}

#: The two recorded crossings, keyed by the single module that makes each one. See the module
#: docstring for the ticket that recorded it and the FR that forced it.
CARVE_OUTS: dict[str, frozenset[str]] = {
    "harnessed.events.bus": frozenset({"harnessed.memory.db", "harnessed.memory.redact"}),
    "harnessed.memory.gate": frozenset({"harnessed.providers.types"}),
    # Ticket 247 (FR-149, decision 81): consolidation builds the gate's kind of request and parses
    # its reply with ``translate.consolidation_facts``, the one module that may decode model output.
    "harnessed.memory.consolidation": frozenset(
        {"harnessed.providers.types", "harnessed.providers.translate"}
    ),
}

#: The vendor SDKs. `providers/` is the only package permitted to name either of them.
VENDOR_SDKS = frozenset({"anthropic", "openai"})

PACKAGES = frozenset(
    {
        "acp",
        "cli",
        "control",
        "evals",
        "events",
        "improve",
        "loop",
        "memory",
        "providers",
        "router",
        "tasks",
        "tools",
    }
)


@dataclass(frozen=True)
class Edge:
    """One import, named at both ends so a failure reads as a sentence."""

    module: str
    target: str
    lineno: int

    def __str__(self) -> str:
        return f"{self.module} imports {self.target} (line {self.lineno})"


def _module_index(root: Path) -> dict[str, Path]:
    """Every module the package actually contains, by dotted name."""
    index: dict[str, Path] = {}
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1:] == ["__init__"]:
            parts = parts[:-1]
        index[".".join([root.name, *parts])] = path
    return index


def _resolve(candidate: str, known: frozenset[str]) -> str | None:
    """Longest known module that ``candidate`` names, or ``None`` if it is external.

    ``from harnessed.providers.types import ChatRequest`` names a symbol inside a module and
    ``from harnessed import clock`` names a module inside a package; both arrive here as a dotted
    string, and only the module index can tell them apart. Matching on a prefix instead would let
    the bare package ``harnessed`` stand for everything underneath it.
    """
    parts = candidate.split(".")
    while parts:
        name = ".".join(parts)
        if name in known:
            return name
        parts.pop()
    return None


def _relative_base(module: str, path: Path, level: int, tail: str | None) -> str:
    owner = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    for _ in range(level - 1):
        owner = owner.rsplit(".", 1)[0] if "." in owner else owner
    return f"{owner}.{tail}" if tail else owner


def edges_of(root: Path) -> list[Edge]:
    """Every import in the package, internal ones resolved, external ones as their top name."""
    index = _module_index(root)
    known = frozenset(index)
    edges: list[Edge] = []
    for module, path in sorted(index.items()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = _relative_base(module, path, node.level, node.module)
                else:
                    base = node.module or ""
                candidates = [f"{base}.{alias.name}" for alias in node.names]
            for candidate in candidates:
                target = _resolve(candidate, known) or candidate.split(".")[0]
                if target != module:
                    edges.append(Edge(module, target, node.lineno))
    return edges


def _area(module: str) -> str:
    """The row of the import table a module answers to."""
    parts = module.split(".")
    if len(parts) == 1:
        return parts[0]
    head = parts[1]
    # `python -m harnessed` is the console entry point, so it belongs to the command surface for
    # both directions: it may import everything, and nothing imports it.
    return "cli" if head == "__main__" else head


def internal(edges: list[Edge], root_name: str = "harnessed") -> list[Edge]:
    return [edge for edge in edges if edge.target.split(".")[0] == root_name]


def _permitted(edge: Edge) -> bool:
    if edge.target in LEAF_MODULES:
        return True
    if _area(edge.module) == _area(edge.target):
        return True
    if edge.target in CARVE_OUTS.get(edge.module, frozenset()):
        return True
    return any(
        edge.target == prefix or edge.target.startswith(f"{prefix}.")
        for prefix in IMPORT_TABLE.get(_area(edge.module), ())
    )


def table_violations(edges: list[Edge]) -> list[Edge]:
    return [edge for edge in internal(edges) if not _permitted(edge)]


def vendor_sdk_violations(edges: list[Edge]) -> list[Edge]:
    return [
        edge for edge in edges if edge.target in VENDOR_SDKS and _area(edge.module) != "providers"
    ]


def reaching(edges: list[Edge], *, source: str, into: str) -> list[Edge]:
    """Edges from one area into another, by area name on both ends."""
    return [
        edge
        for edge in internal(edges)
        if _area(edge.module) == source and _area(edge.target) == into
    ]


def report(edges: list[Edge]) -> str:
    return "\n".join(str(edge) for edge in edges)


def write_package(root: Path, modules: dict[str, str]) -> Path:
    """Write a synthetic ``harnessed`` package so a rule can be run against source that breaks it.

    Every scan takes a root rather than reading ``PACKAGE_ROOT`` directly, which is the whole
    reason a violation can be staged at all: nothing here touches the real tree.
    """
    package = root / "harnessed"
    for dotted, source in modules.items():
        parts = dotted.split(".")
        path = package.joinpath(*parts[:-1]) / f"{parts[-1]}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    for directory in [package, *(p for p in package.rglob("*") if p.is_dir())]:
        init = directory / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
    return package


REAL_EDGES = edges_of(PACKAGE_ROOT)


# ---------------------------------------------------------------------------
# The declared direction
# ---------------------------------------------------------------------------


def test_the_import_graph_matches_the_declared_direction() -> None:
    """Every internal edge in the package is one the import table admits."""
    violations = table_violations(REAL_EDGES)
    assert violations == [], report(violations)


def test_the_scan_sees_the_edges_it_is_judging() -> None:
    """A guard on the guard: an empty graph would make every rule below pass vacuously."""
    edges = internal(REAL_EDGES)
    assert len(edges) > 100
    assert {_area(edge.module) for edge in edges} >= PACKAGES


def test_an_import_inside_a_function_body_is_caught_like_one_in_the_header(
    tmp_path: Path,
) -> None:
    """Why the scan is static: a deferred import is still an edge."""
    package = write_package(
        tmp_path,
        {
            "providers.base": "",
            "loop.engine": (
                "def run() -> None:\n"
                "    from harnessed.providers.base import Provider\n"
                "\n"
                "    _ = Provider\n"
            ),
        },
    )
    violations = table_violations(edges_of(package))
    assert [(edge.module, edge.target) for edge in violations] == [
        ("harnessed.loop.engine", "harnessed.providers.base")
    ]


def test_a_forbidden_edge_fails_naming_the_importing_module_and_the_target(
    tmp_path: Path,
) -> None:
    """The last acceptance criterion: the failure says who imported what."""
    package = write_package(
        tmp_path,
        {
            "loop.engine": "",
            "providers.registry": "from harnessed.loop.engine import run_loop\n",
        },
    )
    violations = table_violations(edges_of(package))
    assert len(violations) == 1
    text = report(violations)
    assert "harnessed.providers.registry" in text
    assert "harnessed.loop.engine" in text


def test_a_permitted_edge_is_not_reported(tmp_path: Path) -> None:
    """The negative control: the rule rejects the direction, not the pair of packages."""
    package = write_package(
        tmp_path,
        {
            "providers.types": "",
            "loop.engine": "from harnessed.providers.types import ChatRequest\n",
        },
    )
    assert table_violations(edges_of(package)) == []


def test_the_bare_package_form_of_an_import_is_expanded_before_matching(
    tmp_path: Path,
) -> None:
    """``from harnessed import clock`` records the package, and a prefix rule would then match
    every module underneath it. Resolving against the module index is what keeps that honest."""
    package = write_package(
        tmp_path,
        {
            "clock": "",
            "loop.engine": "from harnessed import clock\n",
            "memory.db": "from harnessed import clock\n",
        },
    )
    targets = {edge.target for edge in internal(edges_of(package))}
    assert "harnessed.clock" in targets
    assert "harnessed" not in targets


def test_nothing_imports_cli() -> None:
    """``cli`` is imported by nothing but the control app, and the control app only by module.

    Decision 49 makes ``control/`` a second caller of ``run_cmd``'s composition rather than a
    subprocess, which is an edge into ``cli``. It is admitted per module, so the control app can
    never reach the Typer surface of a command it has no business calling.
    """
    importers = [
        edge
        for edge in internal(REAL_EDGES)
        if _area(edge.target) == "cli"
        and _area(edge.module) != "cli"
        and not (_area(edge.module) == "control" and edge.target in CONTROL_REACHES_CLI)
    ]
    assert importers == [], report(importers)


def test_the_control_app_reaches_the_composition_it_calls() -> None:
    """Every admitted edge into ``cli`` is used: a carve-out nothing uses is one to delete."""
    reached = {edge.target for edge in reaching(REAL_EDGES, source="control", into="cli")}
    assert reached == set(CONTROL_REACHES_CLI)


def test_an_import_of_cli_from_below_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "cli.render": "",
            "loop.engine": "from harnessed.cli.render import render\n",
        },
    )
    importers = [edge for edge in internal(edges_of(package)) if _area(edge.target) == "cli"]
    assert [str(edge) for edge in importers] == [
        "harnessed.loop.engine imports harnessed.cli.render (line 1)"
    ]


# ---------------------------------------------------------------------------
# The leaves
# ---------------------------------------------------------------------------


def test_the_leaves_import_nothing_internal_but_the_terminal_leaf() -> None:
    """``errors``, ``ids``, ``clock``, ``config`` and ``serialize`` reach nothing above them.

    ``config`` raises ``ConfigError`` and ``MissingCredentialError``, so it names ``errors`` - the
    one leaf that imports nothing at all. That edge is enumerated rather than admitted as a rule,
    so it cannot become ``config -> anything``, and it stays a leaf pair: neither end can reach a
    package, so there is no path back up the graph through it.
    """
    reached = {
        (edge.module, edge.target)
        for edge in internal(REAL_EDGES)
        if _area(edge.module) in {"clock", "config", "errors", "host", "ids", "proc", "serialize"}
    }
    # Decision 95: `proc` names `host` for `SeamLine` alone; `host` reaches nothing.
    assert reached == {
        ("harnessed.config", "harnessed.errors"),
        ("harnessed.proc", "harnessed.host"),
    }


def test_a_leaf_that_reaches_a_package_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "memory.db": "",
            "config": "from harnessed.memory.db import connect\n",
        },
    )
    violations = table_violations(edges_of(package))
    assert [(edge.module, edge.target) for edge in violations] == [
        ("harnessed.config", "harnessed.memory.db")
    ]


# ---------------------------------------------------------------------------
# loop/ reaches exactly one module under providers/
# ---------------------------------------------------------------------------


def test_loop_reaches_no_module_under_providers_beyond_types() -> None:
    """FR-5: the loop knows the normalized types and nothing about how a provider speaks."""
    reached = {edge.target for edge in reaching(REAL_EDGES, source="loop", into="providers")}
    assert reached == {"harnessed.providers.types"}


def test_a_second_provider_module_reached_from_loop_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "providers.types": "",
            "providers.translate": "",
            "loop.engine": (
                "from harnessed.providers.types import ChatRequest\n"
                "from harnessed.providers.translate import to_wire\n"
            ),
        },
    )
    edges = edges_of(package)
    assert {edge.target for edge in reaching(edges, source="loop", into="providers")} != {
        "harnessed.providers.types"
    }
    violations = table_violations(edges)
    assert [(edge.module, edge.target) for edge in violations] == [
        ("harnessed.loop.engine", "harnessed.providers.translate")
    ]


# ---------------------------------------------------------------------------
# providers/ is below everything it serves
# ---------------------------------------------------------------------------


def test_providers_import_nothing_from_the_packages_they_serve() -> None:
    """FR-8: a provider cannot learn what the system did with its answer."""
    above = {"evals", "improve", "loop", "memory", "tools"}
    violations = [
        edge
        for edge in internal(REAL_EDGES)
        if _area(edge.module) == "providers" and _area(edge.target) in above
    ]
    assert violations == [], report(violations)


def test_the_fake_provider_imports_nothing_from_memory() -> None:
    """FR-8, sharpened: FakeProvider never learns anything about outcomes."""
    reached = [
        edge
        for edge in internal(REAL_EDGES)
        if edge.module == "harnessed.providers.fake" and _area(edge.target) == "memory"
    ]
    assert reached == [], report(reached)


def test_the_fake_decider_imports_nothing_from_evals_improve_or_memory() -> None:
    """FR-96, MUST rule 2's twin: FakeDecider never learns anything about outcomes either."""
    reached = [
        edge
        for edge in internal(REAL_EDGES)
        if edge.module == "harnessed.providers.fake_decider"
        and _area(edge.target) in {"evals", "improve", "memory"}
    ]
    assert reached == [], report(reached)


def test_a_provider_reaching_upward_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "memory.episodic": "",
            "providers.fake": "from harnessed.memory.episodic import recent\n",
        },
    )
    violations = table_violations(edges_of(package))
    assert [(edge.module, edge.target) for edge in violations] == [
        ("harnessed.providers.fake", "harnessed.memory.episodic")
    ]


# ---------------------------------------------------------------------------
# The promotion path has no model in it
# ---------------------------------------------------------------------------


def test_the_promoter_does_not_import_providers_at_all() -> None:
    """FR-20: "zero model calls, forever" is structural here, not a behavioural assertion."""
    reached = [
        edge
        for edge in internal(REAL_EDGES)
        if edge.module == "harnessed.improve.promoter" and _area(edge.target) == "providers"
    ]
    assert reached == [], report(reached)


def test_the_promoter_reaching_providers_is_reported(tmp_path: Path) -> None:
    """Even ``providers.types``, which the rest of ``improve/`` may name, is refused here."""
    package = write_package(
        tmp_path,
        {
            "providers.types": "",
            "improve.promoter": "from harnessed.providers.types import ChatRequest\n",
        },
    )
    reached = [
        edge
        for edge in internal(edges_of(package))
        if edge.module == "harnessed.improve.promoter" and _area(edge.target) == "providers"
    ]
    assert [str(edge) for edge in reached] == [
        "harnessed.improve.promoter imports harnessed.providers.types (line 1)"
    ]


# ---------------------------------------------------------------------------
# acp/ is below the loop, not beside it
# ---------------------------------------------------------------------------


def test_evals_import_neither_providers_transport_nor_a_vendor_exception() -> None:
    """Ticket 231 (FR-160): the runner catches ``errors.TransportDropped``; the retry itself lives
    in ``providers/transport.py``, which ``evals/`` never reaches."""
    violations = [
        edge
        for edge in REAL_EDGES
        if _area(edge.module) == "evals"
        and (edge.target == "harnessed.providers.transport" or edge.target in VENDOR_SDKS)
    ]
    assert violations == [], report(violations)


def test_acp_imports_neither_loop_nor_evals() -> None:
    """FR-22: delegation is a tool the loop calls, never a caller of the loop."""
    violations = [
        edge
        for edge in internal(REAL_EDGES)
        if _area(edge.module) == "acp" and _area(edge.target) in {"loop", "evals"}
    ]
    assert violations == [], report(violations)


def test_acp_reaching_the_loop_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "loop.engine": "",
            "acp.delegate": "from harnessed.loop.engine import run_loop\n",
        },
    )
    violations = table_violations(edges_of(package))
    assert [(edge.module, edge.target) for edge in violations] == [
        ("harnessed.acp.delegate", "harnessed.loop.engine")
    ]


# ---------------------------------------------------------------------------
# The vendor SDKs are quarantined
# ---------------------------------------------------------------------------


def test_no_module_outside_providers_imports_a_vendor_sdk() -> None:
    """Nothing else in the repo imports ``openai`` or ``anthropic``."""
    violations = vendor_sdk_violations(REAL_EDGES)
    assert violations == [], report(violations)


def test_the_vendor_scan_sees_the_sdks_where_they_are_allowed() -> None:
    """The quarantine is only meaningful if the scan finds the imports it quarantines."""
    sdk_edges = {(edge.module, edge.target) for edge in REAL_EDGES if edge.target in VENDOR_SDKS}
    assert sdk_edges == {
        ("harnessed.providers.anthropic", "anthropic"),
        ("harnessed.providers.openai_compat", "openai"),
        # Ticket 031: ``validate_credential`` catches the two vendor error trees through
        # function-local imports, because the broad catch is reserved to the two seams
        # ``tests/test_events.py`` enumerates. Both edges are inside the quarantine, so the rule
        # the scan above guards - nothing outside ``providers/`` names an SDK - is untouched.
        ("harnessed.providers", "anthropic"),
        ("harnessed.providers", "openai"),
        # Ticket 231 (FR-160): ``transport.is_transport`` recognises the two SDKs' connection and
        # status errors, through function-local imports so an offline run never loads either.
        ("harnessed.providers.transport", "anthropic"),
        ("harnessed.providers.transport", "openai"),
    }


def test_the_acp_sdk_is_not_mistaken_for_the_internal_package() -> None:
    """``acp/client.py`` imports the vendor SDK named ``acp``. The internal scan keys on the
    ``harnessed.`` prefix, so the two never collide."""
    assert {edge.target for edge in REAL_EDGES if edge.target == "acp"} == {"acp"}
    assert vendor_sdk_violations(REAL_EDGES) == []


def test_an_sdk_import_outside_providers_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "loop.engine": "import anthropic\n",
            "evals.runner": "from openai import OpenAI\n",
        },
    )
    violations = vendor_sdk_violations(edges_of(package))
    assert sorted((edge.module, edge.target) for edge in violations) == [
        ("harnessed.evals.runner", "openai"),
        ("harnessed.loop.engine", "anthropic"),
    ]


# ---------------------------------------------------------------------------
# The Decider seam: one module, and outbound HTTP quarantined to two (decision 57)
# ---------------------------------------------------------------------------

#: Ticket 153's evidence manifest: the two static scans' own output, regenerated on every verify
#: run and never committed (``evidence/`` is gitignored).
_EVIDENCE_153 = REPO_ROOT / "evidence" / "153"
_EVIDENCE_172 = REPO_ROOT / "evidence" / "172"


def _capture_153(name: str, text: str) -> None:
    _EVIDENCE_153.mkdir(parents=True, exist_ok=True)
    (_EVIDENCE_153 / name).write_text(text, encoding="utf-8")


#: The three modules the outbound-HTTP scan below permits (decision 57): the price catalog, the
#: Decisions call, and the metering proxy ``acp/metering.py`` that ticket 172 added (FR-113).
OUTBOUND_HTTP_HOMES = frozenset({"providers/pricing.py", "providers/jev.py", "acp/metering.py"})

#: The exact dotted modules that make a call outbound. Matched by full dotted name rather than
#: through ``edges_of``'s external fallback (which collapses to the top package, "urllib"), so
#: ``providers/quirks.py``'s unrelated ``from urllib.parse import urlsplit`` is not conflated
#: with an actual transport.
OUTBOUND_HTTP_MODULES = frozenset({"urllib.request", "http.client"})


def _outbound_http_imports(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name in OUTBOUND_HTTP_MODULES)
        elif isinstance(node, ast.ImportFrom) and node.module in OUTBOUND_HTTP_MODULES:
            found.add(node.module)
    return frozenset(found)


def outbound_http_homes(root: Path) -> dict[str, frozenset[str]]:
    """Every module under ``root`` that imports ``urllib.request`` or ``http.client``, and which."""
    homes = {
        path.relative_to(root).as_posix(): _outbound_http_imports(path)
        for path in sorted(root.rglob("*.py"))
    }
    return {name: modules for name, modules in homes.items() if modules}


#: Ticket 231 (FR-160): ``providers/transport.py`` imports ``http.client`` for its exception
#: classes (``RemoteDisconnected``, ``IncompleteRead``) and opens nothing; the test below proves it
#: never names a connection or an opener, so it is not a fourth home.
EXCEPTION_ONLY_HTTP_IMPORTS = {"providers/transport.py": frozenset({"http.client"})}
OPENERS = frozenset({"HTTPConnection", "HTTPSConnection", "urlopen", "build_opener"})


def real_outbound_http_homes() -> dict[str, frozenset[str]]:
    """:func:`outbound_http_homes` of the real tree, less the exception-only importers."""
    homes = outbound_http_homes(PACKAGE_ROOT)
    return {
        name: mods for name, mods in homes.items() if EXCEPTION_ONLY_HTTP_IMPORTS.get(name) != mods
    }


def test_the_exception_only_http_importer_opens_nothing() -> None:
    for name in EXCEPTION_ONLY_HTTP_IMPORTS:
        tree = ast.parse((PACKAGE_ROOT / name).read_text(encoding="utf-8"))
        named = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert not named & OPENERS, f"{name} names an opener: {sorted(named & OPENERS)}"


def test_outbound_http_is_quarantined_to_pricing_jev_and_metering() -> None:
    """FR-94, FR-113, decision 57: exactly three modules under ``src/`` may open a URL."""
    homes = real_outbound_http_homes()
    assert set(homes) == OUTBOUND_HTTP_HOMES, homes
    scan = (
        "modules opening outbound HTTP:\n"
        + "\n".join(f"  {name}: {sorted(mods)}" for name, mods in sorted(homes.items()))
        + "\n"
    )
    _capture_153("outbound-http-scan.txt", scan)
    _EVIDENCE_172.mkdir(parents=True, exist_ok=True)
    (_EVIDENCE_172 / "outbound-http-scan.txt").write_text(scan, encoding="utf-8")


def test_the_outbound_http_scan_sees_the_modules_it_permits() -> None:
    """Not vacuous: all three permitted modules actually open a connection."""
    homes = real_outbound_http_homes()
    assert homes == {
        "providers/pricing.py": frozenset({"urllib.request"}),
        "providers/jev.py": frozenset({"urllib.request"}),
        "acp/metering.py": frozenset({"http.client"}),
    }


def test_an_outbound_http_import_outside_the_three_homes_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "loop.engine": "import urllib.request\n",
            "acp.delegate": "from http.client import HTTPConnection\n",
        },
    )
    homes = outbound_http_homes(package)
    assert set(homes) == {"loop/engine.py", "acp/delegate.py"}


def test_urllib_parse_alone_is_not_mistaken_for_a_transport(tmp_path: Path) -> None:
    """``providers/quirks.py``'s real import: parsing a hostname is not an outbound call."""
    package = write_package(tmp_path, {"providers.quirks": "from urllib.parse import urlsplit\n"})
    assert outbound_http_homes(package) == {}


def modules_naming_the_decisions_url(root: Path) -> frozenset[str]:
    return frozenset(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if DECISIONS_URL in path.read_text(encoding="utf-8")
    )


def test_the_decisions_url_appears_only_in_jev() -> None:
    """FR-94: the Decisions URL and its ``alpha`` path exist in one module and nowhere else."""
    modules = modules_naming_the_decisions_url(PACKAGE_ROOT)
    assert modules == {"providers/jev.py"}
    _capture_153(
        "decisions-url-scan.txt",
        "modules naming the Decisions URL: " + ", ".join(sorted(modules)) + "\n",
    )


def test_the_decisions_url_scan_fires_on_a_planted_literal(tmp_path: Path) -> None:
    planted = tmp_path / "harnessed" / "loop"
    planted.mkdir(parents=True)
    (planted / "engine.py").write_text(f'URL = "{DECISIONS_URL}"\n', encoding="utf-8")
    assert modules_naming_the_decisions_url(tmp_path / "harnessed") == {"loop/engine.py"}


def test_loop_imports_nothing_from_providers_jev() -> None:
    """FR-94's other half: ``loop/`` never reaches the Decider seam directly."""
    reached = [
        edge
        for edge in internal(REAL_EDGES)
        if _area(edge.module) == "loop" and edge.target.startswith("harnessed.providers.jev")
    ]
    assert reached == [], report(reached)


def test_an_import_of_jev_from_loop_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "providers.jev": "",
            "loop.engine": "from harnessed.providers.jev import JevDecider\n",
        },
    )
    edges = edges_of(package)
    reached = [
        edge
        for edge in internal(edges)
        if _area(edge.module) == "loop" and edge.target.startswith("harnessed.providers.jev")
    ]
    assert [(edge.module, edge.target) for edge in reached] == [
        ("harnessed.loop.engine", "harnessed.providers.jev")
    ]


def test_jev_is_imported_only_by_cli_and_router() -> None:
    """FR-94, FR-119: the Decider seam is reached from the command surface and from the router
    package that builds the call for it - nowhere else, ``loop`` included (ticket 160).

    ``providers.fake_decider`` importing ``providers.jev`` is the package assembling itself (both
    area "providers") and is excluded, the same way a router module importing a sibling is
    excluded from the router package's own version of this scan.
    """
    reached = [
        edge
        for edge in internal(REAL_EDGES)
        if (
            edge.target == "harnessed.providers.jev"
            or edge.target.startswith("harnessed.providers.jev.")
        )
        and _area(edge.module) != "providers"
    ]
    assert reached, "no edge reaches harnessed.providers.jev; the scan looks at the wrong target"
    assert all(_area(edge.module) in ("cli", "router") for edge in reached), report(reached)


def test_the_router_row_holds_decision_56_exactly() -> None:
    """FR-94, FR-101: the row this ticket adds, pinned so a later ticket cannot narrow it back."""
    assert IMPORT_TABLE["router"] == (
        "harnessed.providers.jev",
        "harnessed.acp.config",
        "harnessed.acp.ledger",
        "harnessed.memory",
        "harnessed.loop.prompt",
        "harnessed.events",
        "harnessed.config",
    )


def test_router_is_imported_only_by_cli() -> None:
    """Decision 56's other half: nothing under ``loop``, ``evals``, ``improve``, ``tools``,
    ``memory`` or ``acp`` may reach the router package - only the command surface does, plus
    ticket 180's one gateway edge into ``router/decisions.py`` alone (decision 67, FR-120).

    A router module reaching a sibling (``route.py`` importing ``questions.py``) is excluded: that
    is the package assembling itself, not something outside it reaching in, and ticket 160 is the
    first to add such an edge.
    """
    reached = [
        edge
        for edge in internal(REAL_EDGES)
        if (edge.target == "harnessed.router" or edge.target.startswith("harnessed.router."))
        and _area(edge.module) != "router"
    ]
    assert reached, "no edge reaches harnessed.router; the scan is looking at the wrong target"
    assert all(
        _area(edge.module) == "cli"
        or (_area(edge.module) == "gateway" and edge.target == "harnessed.router.decisions")
        for edge in reached
    ), report(reached)


def test_an_import_of_router_from_loop_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "router.decisions": "",
            "loop.engine": "from harnessed.router.decisions import record_probe\n",
        },
    )
    edges = edges_of(package)
    reached = [
        edge
        for edge in internal(edges)
        if edge.target == "harnessed.router" or edge.target.startswith("harnessed.router.")
    ]
    assert [(edge.module, edge.target) for edge in reached] == [
        ("harnessed.loop.engine", "harnessed.router.decisions")
    ]


# ---------------------------------------------------------------------------
# The two recorded carve-outs
# ---------------------------------------------------------------------------


def test_each_carve_out_is_exactly_the_edge_set_its_ticket_recorded() -> None:
    """Pinned to the module and its targets, so a crossing cannot widen to its package."""
    for module, permitted in CARVE_OUTS.items():
        crossings = {
            edge.target
            for edge in internal(REAL_EDGES)
            if edge.module == module
            and edge.target not in LEAF_MODULES
            and _area(edge.target) != _area(module)
        }
        assert crossings == permitted, f"{module}: {sorted(crossings)}"


def test_no_carve_out_is_stale() -> None:
    """A carve-out nothing uses is one to delete, not one to keep for a future edit."""
    for module, permitted in CARVE_OUTS.items():
        used = {edge.target for edge in internal(REAL_EDGES) if edge.module == module} & permitted
        assert used == permitted, f"{module} no longer needs {sorted(permitted - used)}"


def test_the_sibling_of_a_carve_out_module_is_still_refused(tmp_path: Path) -> None:
    """``events/bus.py`` may reach those two modules. Another module under ``events/`` may not."""
    package = write_package(
        tmp_path,
        {
            "memory.db": "",
            "memory.redact": "",
            "events.bus": (
                "from harnessed.memory import db\nfrom harnessed.memory import redact\n"
            ),
            "events.names": "from harnessed.memory import db\n",
        },
    )
    violations = table_violations(edges_of(package))
    assert [(edge.module, edge.target) for edge in violations] == [
        ("harnessed.events.names", "harnessed.memory.db")
    ]


# ---------------------------------------------------------------------------
# The seeding mechanism of FR-36 is test support and nothing shipped can reach it
# ---------------------------------------------------------------------------

#: The spellings by which a module under ``src/harnessed`` could name ``tests/support/divergence``:
#: through the tests package, through the support package pytest puts on ``sys.path``, or bare.
#: The scan records an external import by its top name, so these are the top names to refuse.
TEST_SUPPORT = frozenset({"tests", "support", "divergence"})

DIVERGENCE_PATH = REPO_ROOT / "tests" / "support" / "divergence.py"


def support_violations(edges: list[Edge]) -> list[Edge]:
    return [edge for edge in edges if edge.target.split(".")[0] in TEST_SUPPORT]


def test_the_seeding_mechanism_is_unimportable_from_the_shipped_package() -> None:
    """FR-36: ``tests/support/divergence.py`` exists, and no module under ``src/`` names it."""
    assert DIVERGENCE_PATH.is_file(), "ticket 071's seeding helper is missing"
    assert "divergence" not in _module_index(PACKAGE_ROOT)
    violations = support_violations(REAL_EDGES)
    assert violations == [], report(violations)


@pytest.mark.parametrize(
    "spelling",
    [
        "from tests.support.divergence import diverged_scored_run\n",
        "from support.divergence import dropped\n",
        "import divergence\n",
        "def seed() -> None:\n    from tests.support import divergence\n\n    _ = divergence\n",
    ],
)
def test_a_shipped_module_reaching_the_seeding_mechanism_is_reported(
    tmp_path: Path, spelling: str
) -> None:
    package = write_package(tmp_path, {"gateway.routes.evals": spelling})
    violations = support_violations(edges_of(package))
    assert len(violations) == 1
    assert violations[0].module == "harnessed.gateway.routes.evals"
    assert violations[0].target in TEST_SUPPORT


# ---------------------------------------------------------------------------
# The dashboard reads the gateway and nothing else (decision 28)
# ---------------------------------------------------------------------------

DASHBOARD_ROOT = REPO_ROOT / "dashboard"

#: What a dashboard source file may never name: the database, its home, or the filesystem.
FORBIDDEN_DASHBOARD_TEXT = ("sqlite", "harness.db", ".harnessed", "node:fs")

_URL_LITERAL = re.compile(r"https?://")
_METHOD = re.compile(r"""\bmethod\s*:\s*(["'`])([^"'`]*)\1""")
_PATH_LITERAL = re.compile(r"""(["'`])(/[^"'`]*)\1""")

#: The one module allowed a `method:` other than `"GET"`, and the only prefix it may name
#: (decision 43, MUST rule 22): a write goes to the control app, never to the read gateway.
CONTROL_CLIENT = "lib/control-client.ts"
CONTROL_PREFIX = "/control/v1/"


def dashboard_sources(root: Path) -> list[Path]:
    """Every ``.ts`` and ``.tsx`` file under ``app/``, ``components/`` and ``lib/``."""
    found: list[Path] = []
    for area in ("app", "components", "lib"):
        base = root / area
        if base.is_dir():
            found.extend(p for p in base.rglob("*") if p.suffix in {".ts", ".tsx"})
    return sorted(found)


def dashboard_violations(root: Path) -> list[str]:
    """One line per offence: the file relative to ``root``, and what it named."""
    offences: list[str] = []
    for path in dashboard_sources(root):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        offences.extend(
            f"{relative}: names {needle!r}" for needle in FORBIDDEN_DASHBOARD_TEXT if needle in text
        )
        if _URL_LITERAL.search(text):
            offences.append(f"{relative}: an http(s) literal (the gateway is the relative /v1)")
        offences.extend(
            f"{relative}: method {match.group(2)!r}"
            for match in _METHOD.finditer(text)
            if match.group(2) != "GET" and relative != CONTROL_CLIENT
        )
        if relative == CONTROL_CLIENT:
            offences.extend(
                f"{relative}: a route outside {CONTROL_PREFIX} ({match.group(2)!r})"
                for match in _PATH_LITERAL.finditer(text)
                if not match.group(2).startswith(CONTROL_PREFIX)
            )
    return offences


def test_dashboard_reads_only_the_gateway() -> None:
    """Decision 28 over the real tree: silent, and it sees the files it judges."""
    assert dashboard_sources(DASHBOARD_ROOT), "the scan found no dashboard source to judge"
    assert not (DASHBOARD_ROOT / "lib" / "gateway-origin.ts").exists(), "decision 43 deleted it"
    assert (DASHBOARD_ROOT / CONTROL_CLIENT).is_file(), "the one writing module exists (FR-65)"
    assert dashboard_violations(DASHBOARD_ROOT) == []


@pytest.mark.parametrize(
    ("relative", "source", "expected"),
    [
        ("lib/store.ts", 'import Database from "better-sqlite3";\n', "names 'sqlite'"),
        ("app/page.tsx", 'const f = "data/harness.db";\n', "names 'harness.db'"),
        ("components/X.tsx", 'import { readFileSync } from "node:fs";\n', "names 'node:fs'"),
        ("lib/panel.ts", 'fetch("http://127.0.0.1:8756/v1/runs");\n', "an http(s) literal"),
        ("app/a/route.ts", 'fetch("/v1/x", { method: "POST" });\n', "method 'POST'"),
        ("lib/fetch-panel.ts", 'fetch("/v1/x", { method: "POST" });\n', "method 'POST'"),
        (
            "lib/control-client.ts",
            'fetch("/v1/runs", { method: "POST" });\n',
            "a route outside /control/v1/ ('/v1/runs')",
        ),
        (
            "lib/gateway-origin.ts",
            'export const DEFAULT = "http://127.0.0.1:8756/v1";\n',
            "an http(s) literal",
        ),
    ],
)
def test_a_dashboard_file_reaching_past_the_gateway_is_reported(
    tmp_path: Path, relative: str, source: str, expected: str
) -> None:
    offender = tmp_path / relative
    offender.parent.mkdir(parents=True, exist_ok=True)
    offender.write_text(source, encoding="utf-8")
    offences = dashboard_violations(tmp_path)
    assert len(offences) == 1, offences
    assert offences[0].startswith(f"{relative}: ")
    assert expected in offences[0]


def test_a_get_request_at_the_relative_path_is_not_reported(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "panel.ts").write_text(
        'fetch("/v1/runs", { method: "GET" });\n', encoding="utf-8"
    )
    assert dashboard_violations(tmp_path) == []


def test_a_post_from_the_control_client_to_the_control_app_is_not_reported(
    tmp_path: Path,
) -> None:
    """The one sanctioned write: the named module, a `POST`, a `/control/v1/**` route."""
    (tmp_path / "lib").mkdir()
    (tmp_path / CONTROL_CLIENT).write_text(
        'fetch(`/control/v1/runs/${id}/messages`, { method: "POST" });\n', encoding="utf-8"
    )
    assert dashboard_violations(tmp_path) == []


# ---------------------------------------------------------------------------
# The one process: dashboard_app is imported by cli and by nothing else (decision 43)
# ---------------------------------------------------------------------------


def test_nothing_below_cli_imports_dashboard_app() -> None:
    """``dashboard_app`` composes the mounts and is reached only from the command surface."""
    importers = [
        edge
        for edge in internal(REAL_EDGES)
        if _area(edge.target) == "dashboard_app" and _area(edge.module) != "cli"
    ]
    assert importers == [], report(importers)


def test_an_import_of_dashboard_app_from_below_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "dashboard_app": "",
            "gateway.app": "from harnessed import dashboard_app\n",
        },
    )
    importers = [
        edge for edge in internal(edges_of(package)) if _area(edge.target) == "dashboard_app"
    ]
    assert [str(edge) for edge in importers] == [
        "harnessed.gateway.app imports harnessed.dashboard_app (line 1)"
    ]


# ---------------------------------------------------------------------------
# The control app writes beside the gateway, never through it (FR-77, decision 43)
# ---------------------------------------------------------------------------


def test_the_control_app_imports_nothing_from_the_gateway() -> None:
    edges = [edge for edge in internal(REAL_EDGES) if _area(edge.module) == "control"]
    assert edges, "the scan found no control module to judge"
    assert reaching(REAL_EDGES, source="control", into="gateway") == []
    assert "harnessed.gateway" not in IMPORT_TABLE["control"]


def test_the_gateway_imports_nothing_from_the_control_app() -> None:
    assert reaching(REAL_EDGES, source="gateway", into="control") == []


@pytest.mark.parametrize(
    "source",
    [
        "from harnessed.gateway.errors import status_of\n",
        "def build() -> None:\n    from harnessed.gateway import serialize\n\n    _ = serialize\n",
    ],
)
def test_a_seeded_gateway_import_in_the_control_app_is_reported(
    tmp_path: Path, source: str
) -> None:
    package = write_package(
        tmp_path,
        {"gateway.errors": "", "gateway.serialize": "", "control.errors": source},
    )
    violations = table_violations(edges_of(package))
    assert len(violations) == 1, report(violations)
    assert violations[0].module == "harnessed.control.errors"
    assert violations[0].target.startswith("harnessed.gateway")


def test_a_control_import_of_a_command_it_does_not_call_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "cli.run_cmd": "",
            "cli.serve_cmd": "",
            "control.routes": (
                "from harnessed.cli import run_cmd\nfrom harnessed.cli.serve_cmd import serve\n"
            ),
        },
    )
    violations = table_violations(edges_of(package))
    assert [(edge.module, edge.target) for edge in violations] == [
        ("harnessed.control.routes", "harnessed.cli.serve_cmd")
    ]


# ---------------------------------------------------------------------------
# MUST rule 14: a query belongs to the package that owns its table
# ---------------------------------------------------------------------------

#: The SQL verbs no string constant under ``gateway/`` may carry. A handler that spelled one
#: would be a second source of truth for a table another package already owns - which is
#: exactly the ``json_extract(payload, '$.eval_run_id')`` decision 14 refused.
SQL_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")

GATEWAY_ROOT = PACKAGE_ROOT / "gateway"


def _string_constants(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _sql_literals_under(root: Path) -> list[str]:
    return [
        f"{path.name}:{lineno} {text!r}"
        for path in sorted(root.rglob("*.py"))
        for lineno, text in _string_constants(path)
        if any(verb in text for verb in SQL_VERBS)
    ]


def test_the_gateway_spells_zero_sql_string_literals() -> None:
    """The import table's structural twin: the gateway reads stores, it does not query tables."""
    assert sorted(path.name for path in GATEWAY_ROOT.rglob("*.py")), "no gateway sources found"
    assert _sql_literals_under(GATEWAY_ROOT) == []


def test_the_sql_scan_reports_a_handler_that_spells_a_query(tmp_path: Path) -> None:
    """Not vacuous: the same scan over a planted handler with a query finds it."""
    planted = tmp_path / "gateway" / "routes"
    planted.mkdir(parents=True)
    (planted / "evals.py").write_text(
        'ROWS = "SELECT case_id FROM eval_results WHERE eval_run_id = ?"\n', encoding="utf-8"
    )
    found = _sql_literals_under(tmp_path)
    assert len(found) == 1
    assert "evals.py" in found[0]


# ---------------------------------------------------------------------------
# Jev never enters the flywheel (ticket 177, FR-119, MUST rule 30)
# ---------------------------------------------------------------------------

#: The router package and the Decider seam: the two things eval scoring, reflection, promotion and
#: the graders may never reach.
DECIDER_TARGETS = ("harnessed.router", "harnessed.providers.jev")

#: ``evals/`` holds the graders (``evals/graders.py``); ``improve/`` holds the reflector and the
#: promoter.
FLYWHEEL_AREAS = frozenset({"evals", "improve"})

#: The repository's own eval harness (``evals/helpers.py`` and the suites' conftest). Not part of
#: the package, so the package scan above never reads it; scanned by name below.
EVAL_HARNESS_ROOT = REPO_ROOT / "evals"


def _names_a_decider(target: str) -> bool:
    return any(target == name or target.startswith(f"{name}.") for name in DECIDER_TARGETS)


def flywheel_decider_edges(edges: list[Edge]) -> list[Edge]:
    """Edges from ``evals/`` or ``improve/`` into the router or the Decider seam."""
    return [
        edge
        for edge in internal(edges)
        if _area(edge.module) in FLYWHEEL_AREAS and _names_a_decider(edge.target)
    ]


def harness_decider_imports(root: Path) -> list[str]:
    """Every import under ``root`` that names the router or the Decider seam, one line each."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [f"{node.module}.{alias.name}" for alias in node.names]
            else:
                continue
            found.extend(
                f"{path.relative_to(root).as_posix()} imports {name} (line {node.lineno})"
                for name in names
                if _names_a_decider(name)
            )
    return found


def test_evals_improve_and_the_graders_import_neither_the_router_nor_jev() -> None:
    """FR-119: 0 imports, in the package's ``evals``/``improve`` and in the repo's eval harness."""
    assert any(_area(edge.module) == "evals" for edge in REAL_EDGES), "the scan sees no evals"
    assert any(edge.module == "harnessed.evals.graders" for edge in REAL_EDGES)
    assert any(_area(edge.module) == "improve" for edge in REAL_EDGES), "the scan sees no improve"
    reached = flywheel_decider_edges(REAL_EDGES)
    assert reached == [], report(reached)
    assert sorted(EVAL_HARNESS_ROOT.rglob("*.py")), "no eval harness sources found"
    assert harness_decider_imports(EVAL_HARNESS_ROOT) == []


@pytest.mark.parametrize(
    ("module", "source", "target"),
    [
        (
            "evals.graders",
            "from harnessed.providers.jev import JevDecider\n",
            "harnessed.providers.jev",
        ),
        ("improve.reflector", "from harnessed.router import route\n", "harnessed.router.route"),
        (
            "improve.promoter",
            "def late():\n    import harnessed.router.questions\n",
            "harnessed.router.questions",
        ),
    ],
)
def test_a_seeded_decider_import_in_the_flywheel_is_reported(
    tmp_path: Path, module: str, source: str, target: str
) -> None:
    package = write_package(
        tmp_path,
        {
            "providers.jev": "",
            "router.route": "",
            "router.questions": "",
            module: source,
        },
    )
    reached = flywheel_decider_edges(edges_of(package))
    assert [(edge.module, edge.target) for edge in reached] == [(f"harnessed.{module}", target)]
    if module == "evals.graders":
        evidence = REPO_ROOT / "evidence" / "177"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "seeded-import.txt").write_text(
            "177 - a seeded import of the Decider seam into the graders is reported\n\n"
            f"seeded: harnessed/{module.replace('.', '/')}.py: {source}"
            f"reported:\n{report(reached)}\n",
            encoding="utf-8",
        )


def test_a_seeded_decider_import_in_the_eval_harness_is_reported(tmp_path: Path) -> None:
    (tmp_path / "helpers.py").write_text(
        "from harnessed.providers import jev\nimport harnessed.router.route\n", encoding="utf-8"
    )
    assert harness_decider_imports(tmp_path) == [
        "helpers.py imports harnessed.providers.jev (line 1)",
        "helpers.py imports harnessed.router.route (line 2)",
    ]


def test_jail_is_a_stdlib_only_leaf_of_acp() -> None:
    """Ticket 214: ``acp/jail.py`` imports nothing from ``harnessed`` but the host leaf's
    ``SeamLine`` (ticket 322, decision 95)."""
    reached = {edge.target for edge in internal(REAL_EDGES) if edge.module == "harnessed.acp.jail"}
    assert reached == {"harnessed.host"}


# ---- decision 87: a platform branch lives in a seam, never in the core (ticket 230, MUST 42) -----

#: The modules decision 87's table names. Only these may branch on the platform.
PLATFORM_SEAMS = frozenset(
    {
        "host.py",
        "acp/jail.py",
        "acp/delegate.py",
        "proc.py",
        "tasks/installer.py",
        "cli/onboarding.py",
        "cli/render.py",
    }
)

#: No known leak: ticket 244 moved ``cli_runner.py``'s batch-file launch and system-directory
#: helpers onto ``acp/delegate.py``'s and deleted the one entry this held.
PLATFORM_ALLOWLIST: frozenset[str] = frozenset()

PLATFORM_PATTERN = re.compile(
    r"sys\.platform|os\.name|platform\.system|import\s+ctypes|from\s+ctypes\b"
    r"|msvcrt|schtasks|crontab|cmd\.exe"
    # Ticket 323 (decision 95): the vocabulary widens to path spelling and process control.
    r"|os\.(?:alt)?sep|(['\"])\\\\\1|['\"][A-Za-z]:[\\/]|\.(?:exe|cmd|bat)['\"]|os\.startfile"
    r"|shell\s*=\s*True|CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW|os\.getuid"
    r"|launchctl|systemctl|loginctl|sandbox-exec|libproc"
)


def platform_leaks(root: Path) -> dict[str, list[str]]:
    """Each module outside the seams that names a platform branch, with the lines that do."""
    leaks: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in PLATFORM_SEAMS or relative in PLATFORM_ALLOWLIST:
            continue
        hits = [
            f"{number}: {text.strip()}"
            for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if PLATFORM_PATTERN.search(text)
        ]
        if hits:
            leaks[relative] = hits
    return leaks


def test_platform_branches_live_in_seams() -> None:
    assert platform_leaks(PACKAGE_ROOT) == {}


def test_a_platform_branch_in_a_core_module_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {"loop.scratch": "import sys\n\nIS_WINDOWS = sys.platform == 'win32'\n"},
    )
    assert list(platform_leaks(package)) == ["loop/scratch.py"]


def test_a_platform_branch_in_a_seam_is_not_reported(tmp_path: Path) -> None:
    package = write_package(tmp_path, {"proc": "import sys\n\nWIN = sys.platform == 'win32'\n"})
    assert platform_leaks(package) == {}


@pytest.mark.parametrize(
    "line",
    [
        "x = os.sep == '/'",
        "parts = name.split(os.altsep)",
        "x = name.replace('\\\\', '/')",
        "x = 'C:\\\\Users'",
        "x = 'C:/Users'",
        "x = 'tool.exe'",
        'x = "run.cmd"',
        "x = 'run.bat'",
        "os.startfile(path)",
        "subprocess.Popen(line, shell=True)",
        "flags = subprocess.CREATE_NEW_PROCESS_GROUP",
        "flags = subprocess.CREATE_NO_WINDOW",
        "uid = os.getuid()",
        "run(['launchctl', 'load'])",
        "run(['systemctl', 'start'])",
        "run(['loginctl', 'enable-linger'])",
        "run(['sandbox-exec', '-f'])",
        "lib = 'libproc'",
    ],
)
def test_each_widened_platform_word_in_a_core_module_is_reported(tmp_path: Path, line: str) -> None:
    package = write_package(tmp_path, {"loop.scratch": line + "\n"})
    assert list(platform_leaks(package)) == ["loop/scratch.py"]
    evidence = REPO_ROOT / "evidence" / "323"
    evidence.mkdir(parents=True, exist_ok=True)
    with (evidence / "seeded-platform-words.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"reported: {line}\n")


def test_the_platform_allowlist_is_empty_and_the_real_tree_passes() -> None:
    assert not PLATFORM_ALLOWLIST
    leaks = platform_leaks(PACKAGE_ROOT)
    evidence = REPO_ROOT / "evidence" / "323"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "real-tree.txt").write_text(
        f"leaks over src/harnessed: {leaks}\n", encoding="utf-8"
    )
    assert leaks == {}


#: Ticket 325 (decision 96): the CI jobs a platform skip may name are the OS jobs of ``ci.yml``.
CI_JOBS = tuple(
    yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))[
        "jobs"
    ]["verify"]["strategy"]["matrix"]["os"]
)
CI_REASON = re.compile(r"ci: (\w+)")


def _reason_text(node: ast.AST, named: dict[str, str]) -> str:
    """Every string literal under ``node``, and the module constant behind any bare name."""
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.append(child.value)
        elif isinstance(child, ast.Name) and child.id in named:
            found.append(named[child.id])
    return " ".join(found)


def platform_skips_without_a_ci_job(tests_root: Path) -> list[str]:
    """``skipif`` calls on ``sys.platform`` or ``os.name`` whose reason names no CI job."""
    bad: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        relative = path.relative_to(tests_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        named = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func).endswith("skipif")):
                continue
            condition = " ".join(ast.unparse(arg) for arg in node.args)
            if "sys.platform" not in condition and "os.name" not in condition:
                continue
            reason = next((kw.value for kw in node.keywords if kw.arg == "reason"), None)
            text = _reason_text(reason, named) if reason is not None else ""
            job = CI_REASON.search(text)
            if job is None or job.group(1) not in CI_JOBS:
                bad.append(f"{relative}:{node.lineno}")
    return bad


def test_every_platform_skip_names_its_ci_job() -> None:
    assert platform_skips_without_a_ci_job(REPO_ROOT / "tests") == []


def test_a_seeded_platform_skip_without_a_ci_job_is_reported(tmp_path: Path) -> None:
    (tmp_path / "test_seed.py").write_text(
        "import sys\nimport pytest\n\n"
        "@pytest.mark.skipif(sys.platform == 'win32', reason='no ci here')\n"
        "def test_a(): ...\n\n"
        "@pytest.mark.skipif(sys.platform == 'win32', reason='ci: linux job')\n"
        "def test_b(): ...\n\n"
        "@pytest.mark.skipif(sys.platform == 'win32', reason='ci: plan9 job')\n"
        "def test_c(): ...\n",
        encoding="utf-8",
    )
    assert platform_skips_without_a_ci_job(tmp_path) == ["test_seed.py:4", "test_seed.py:10"]


def test_the_cli_runner_holds_no_platform_branch_and_its_allowlist_entry_is_gone() -> None:
    # Ticket 244 (FR-145) moved the batch-file launch and the system-directory helper onto
    # ``acp/delegate.py``; nothing is allowlisted any more.
    text = (PACKAGE_ROOT / "acp" / "cli_runner.py").read_text(encoding="utf-8")
    assert PLATFORM_PATTERN.search(text) is None
    assert not PLATFORM_ALLOWLIST


def test_consolidation_reaches_only_the_memory_modules_ticket_248_names() -> None:
    """FR-150: ``memory/consolidation.py`` imports semantic, episodic, db and redact from memory."""
    tree = ast.parse((PACKAGE_ROOT / "memory" / "consolidation.py").read_text(encoding="utf-8"))
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "harnessed.memory":
                reached.update(f"harnessed.memory.{alias.name}" for alias in node.names)
            else:
                reached.add(node.module)
        elif isinstance(node, ast.Import):
            reached.update(alias.name for alias in node.names)
    memory_reached = {name for name in reached if name.startswith("harnessed.memory.")}
    assert memory_reached <= {
        "harnessed.memory.semantic",
        "harnessed.memory.episodic",
        "harnessed.memory.db",
        "harnessed.memory.redact",
    }
    assert not {name for name in reached if "procedural" in name or "improve" in name}


# Ticket 286 (decisions 107, 109): the operator's unpublished records are read through one helper.
UNPUBLISHED_PATTERN = re.compile(r"operator-inputs|\.spec[/\\]launch")
#: The helper, its own test over temporary roots, this scan (which spells the pattern), and the
#: publication audit, which names the trees to keep them out of the index and reads none of them.
RECORDS_READERS = frozenset(
    {"support/records.py", "test_records.py", "test_boundaries.py", "test_publication_audit.py"}
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def unpublished_readers(tests_root: Path) -> list[str]:
    """Test files that spell a path under the unpublished trees outside ``support/records.py``."""
    leaks: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        relative = path.relative_to(tests_root).as_posix()
        if relative in RECORDS_READERS or "fixtures" in path.relative_to(tests_root).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and UNPUBLISHED_PATTERN.search(node.value)
            ):
                leaks.append(relative)
                break
    return leaks


def test_operator_records_are_read_only_through_the_records_helper() -> None:
    assert unpublished_readers(REPO_ROOT / "tests") == []


def test_a_seeded_direct_read_of_the_operator_inputs_is_reported(tmp_path: Path) -> None:
    (tmp_path / "support").mkdir()
    (tmp_path / "support" / "records.py").write_text('X = ".spec/operator-inputs"\n')
    (tmp_path / "test_bad.py").write_text('P = ROOT / ".spec" / "operator-inputs" / "1"\n')
    (tmp_path / "test_bad2.py").write_text('P = ROOT / ".spec/launch/post.md"\n')
    (tmp_path / "test_ok.py").write_text('"""Mentions operator-inputs."""\nX = 1\n')
    assert unpublished_readers(tmp_path) == ["test_bad.py", "test_bad2.py"]


# ---- no walk to a repository root (ticket 312, FR-176, MUST rule 43) ----------------------------

REPOSITORY_WALK_PATTERN = re.compile(r"\.parents\[|Path\(__file__\)")
REPOSITORY_WALK_SEAM = "shipped.py"


def repository_walks(root: Path) -> dict[str, list[str]]:
    """Each module but ``shipped.py`` that climbs from its own file toward a repository root."""
    walks: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative == REPOSITORY_WALK_SEAM:
            continue
        hits = [
            f"{number}: {text.strip()}"
            for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if REPOSITORY_WALK_PATTERN.search(text)
        ]
        if hits:
            walks[relative] = hits
    return walks


def test_no_walk_to_a_repository_root() -> None:
    assert repository_walks(PACKAGE_ROOT) == {}


def test_a_seeded_walk_to_a_repository_root_is_reported(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        {
            "loop.scratch": (
                "from pathlib import Path\n\nROOT = Path(__file__).resolve().parents[3]\n"
            ),
            "shipped": "from pathlib import Path\n\nHERE = Path(__file__)\n",
        },
    )
    assert list(repository_walks(package)) == ["loop/scratch.py"]
