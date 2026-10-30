"""``harnessed doctor --offline`` - the health check and the last stage of the verify harness.

It asserts the Python version, that the database opens in WAL mode, that migrations are current
with matching checksums, that the fixture corpus loads, that FTS5 is compiled into the local
SQLite, and that the configured provider needs no key. It exits 0 with zero outbound
connections.

**Every check is computed from disk at call time.** Between ticket 001 and ticket 028 three of
the six were static strings saying their subsystem had not landed yet - long after it had - so
stage 6 of the verify harness reported nothing on every ticket in between. A status that cannot
change is not a check, and ``tests/test_cli.py`` scans this file's AST to keep it that way.
"""

from __future__ import annotations

import shutil
import socket
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import typer

from harnessed import config, host, proc, shipped
from harnessed.acp import config as acp_config
from harnessed.acp import jail, probes
from harnessed.cli import render
from harnessed.cli.evidence import Capture
from harnessed.config import PROVIDER_VAR, Settings, load
from harnessed.errors import (
    ConfigError,
    DecisionError,
    EvidenceIncomplete,
    ExportMissing,
    GatewayExtraMissing,
    ProviderError,
    StorageError,
)
from harnessed.memory import db, redact
from harnessed.providers.fake import FakeProvider
from harnessed.providers.jev import Decider, JevDecider, Question
from harnessed.router import decisions as router_decisions
from harnessed.tasks import installer, schedule

__all__ = [
    "Check",
    "CheckStatus",
    "Rung",
    "RungState",
    "ScheduleLine",
    "doctor",
    "platform_section",
    "no_outbound",
    "registry_warnings",
    "run_checks",
    "run_rungs",
    "schedule_line",
]

#: The one question of each type the live rung asks (FR-97). Not the routing questions decision
#: 58 names (``route``, ``needs_memory``, ``memory_kind``) - those belong to ``router/questions.py``
#: once ticket 160 creates it. This is a health probe, never a routing decision: its answer is
#: read for pass/fail and thrown away.
_PROBE_QUESTIONS: Final[tuple[Question, ...]] = (
    Question("noul", "noul", instructions="Is this a health probe?"),
    Question(
        "choice",
        "choice",
        ("a", "b"),
        instructions="Which letter fits this probe?",
        criteria={"a": "the first letter", "b": "the second letter"},
    ),
    Question(
        "score",
        "score",
        ("not at all", "somewhat", "fully"),
        instructions="How complete is this probe?",
    ),
)
_PROBE_STATE: Final = "Task: harnessed doctor --live router probe\n"

CheckStatus = Literal["ok", "skip", "fail"]

#: What a rung of the onboarding ladder can be. ``not yet`` is the state an operator who has not
#: climbed it is in, and it is deliberately not a failure: a doctor that goes red because you
#: have not installed a coding agent is a doctor people learn to ignore. No state is a constant
#: any more: rung 4 was ``v3`` while the dashboard was the next milestone, and from ticket 125
#: it is a check like rung 3 (FR-66). ``declared`` is rung 2 with an executable found but no
#: current proof (ticket 088): only a handshake proves an agent speaks ACP, and
#: ``harnessed agents test`` records it (ticket 089).
RungState = Literal["live", "declared", "not yet", "broken"]

REQUIRED_PYTHON: tuple[int, int] = (3, 12)

#: Where the committed fixture corpus lives. Read inside :func:`run_checks` rather than bound as
#: a default argument, so a test can point the check somewhere the corpus is not and watch it
#: fail - which is the only way to know the check can speak at all.
CORPUS_ROOT: Final[Path] = shipped.fake_wire()


@dataclass(frozen=True, slots=True)
class Check:
    """One health check: what was asserted, how it went, and why."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class Rung:
    """One rung of the onboarding ladder: where the operator is, and what advances them."""

    number: int
    name: str
    state: RungState
    detail: str

    @property
    def broken(self) -> bool:
        """Whether this rung is one the operator configured and it does not work."""
        return self.state == "broken"


def _rung_cli(settings: Settings) -> Rung:
    """Rung 1: a provider is configured, so ``run``, ``eval`` and ``memory`` all work.

    Says so too when the routing key is missing (ticket 232), in ``config.py``'s own words.
    """
    notice = settings.routing_key_notice()
    also = "" if notice is None else f"; {notice}"
    if settings.provider is None:
        return Rung(1, "CLI", "not yet", f"harnessed init - one key and the CLI works{also}")
    try:
        settings.key_var_for(settings.provider)
    except ConfigError as exc:
        return Rung(1, "CLI", "broken", str(exc))
    return Rung(
        1, "CLI", "live", f"provider {settings.provider}, harnessed run / eval / memory{also}"
    )


#: What rung 2 adds while routing is off (ticket 316): a delegation is a router's route.
AGENTS_NEED_ROUTING: Final = "agents run only with routing on"


def _rung_agent(path: Path, home: Path, *, routing_off: bool = False) -> Rung:
    """Rung 2 (:func:`_rung_agents`), with the routing-off words appended to its detail."""
    rung = _rung_agents(path, home)
    if not routing_off:
        return rung
    return Rung(rung.number, rung.name, rung.state, f"{rung.detail}; {AGENTS_NEED_ROUTING}")


def _rung_agents(path: Path, home: Path) -> Rung:
    """Rung 2: an ACP coding agent is declared, installed, and proven by a recorded probe.

    No registry is ``not yet`` - nothing is broken, the operator simply has not climbed here. A
    registry naming an executable that is not installed is ``broken``, because it is a rung they
    did configure, and the detail quotes the install command rather than making them find it.

    A found executable alone is ``declared``, never ``live``: this rung once said ``live`` for two
    agents neither of which could open a session (ticket 088). ``live`` needs the newest
    ``harnessed agents test`` result for the agent to have passed against the entry as it stands
    now (ticket 089); a newest matching failure is ``broken``. The rung takes the weakest agent's
    state and the detail lists each agent's own.
    """
    advance = f"harnessed agents add - templates: {', '.join(acp_config.TEMPLATES)}"
    if not path.exists():
        return Rung(2, "Coding agent", "not yet", advance)
    try:
        agents = acp_config.load_agents(path)
    except ConfigError as exc:
        return Rung(2, "Coding agent", "broken", str(exc))
    missing = [spec for spec in agents.values() if shutil.which(spec.command) is None]
    if missing:
        spec = missing[0]
        return Rung(
            2,
            "Coding agent",
            "broken",
            f"{path} declares {spec.name}, which runs {spec.command!r}, not on PATH. "
            f"{spec.install_hint()}",
        )
    found = _proofs(home, list(agents))
    states = [_agent_state(spec, found.get(spec.name)) for spec in agents.values()]
    weakest = min((state for state, _ in states), key=_STRENGTH.index)
    return Rung(2, "Coding agent", weakest, f"{path}: " + "; ".join(line for _, line in states))


#: Weakest first: the rung reports the worst agent's state.
_STRENGTH: Final[tuple[RungState, ...]] = ("broken", "declared", "live")


def _agent_state(spec: acp_config.AgentSpec, proof: probes.Probe | None) -> tuple[RungState, str]:
    """One agent's own state and the words that explain it."""
    retest = f"harnessed agents test {spec.name}"
    state, line = _proof_state(spec, proof, retest)
    if spec.kind != acp_config.DEFAULT_KIND:
        found = jail.detect()
        reason = redact.scrub(found.reason)
        line += f" [jail: {found.kind} - {reason}]" if reason else f" [jail: {found.kind}]"
    else:
        line += f" [settings: {settings_word(spec)}]"
    return state, line


#: The four things rung 2 says about an ``acp`` entry's settings (ticket 215, FR-131).
SETTINGS_HARNESSED: Final = "harnessed"
SETTINGS_ADD: Final = "folder - add session_meta"
SETTINGS_NO_OVERRIDE: Final = "folder - its adapter documents no override"
SETTINGS_NO_TEMPLATE: Final = "folder - no template"


def settings_word(spec: acp_config.AgentSpec) -> str:
    """Whose settings an ``acp`` entry launches with, and why, from data only."""
    if acp_config.settings_of(spec) == acp_config.SETTINGS_HARNESSED:
        return SETTINGS_HARNESSED
    template = acp_config.TEMPLATES.get(spec.name)
    if template is None:
        return SETTINGS_NO_TEMPLATE
    return SETTINGS_ADD if template.session_meta else SETTINGS_NO_OVERRIDE


def _proof_state(
    spec: acp_config.AgentSpec, proof: probes.Probe | None, retest: str
) -> tuple[RungState, str]:
    if proof is None:
        return "declared", f"{spec.name} declared, never proven - prove it: {retest}"
    if proof.fingerprint != probes.fingerprint(spec, shutil.which(spec.command)):
        return "declared", f"{spec.name} changed since its proof on {proof.date} - {retest}"
    if proof.status == probes.PASSED:
        login = probes.login_word(proof.detail)
        return "live", f"{spec.name} live, proven {proof.date}" + (f", {login}" if login else "")
    return "broken", f"{spec.name} failed its test on {proof.date}: {proof.detail}"


def _proofs(home: Path, agents: list[str]) -> dict[str, probes.Probe]:
    """The newest probe per agent, read without creating, migrating or writing anything.

    No database, one that will not open, or one behind the migration that made the table all
    mean the same thing here: there is no proof yet.
    """
    database = home / db.DATABASE_FILENAME
    if not database.exists():
        return {}
    try:
        conn = db.connect_readonly(database)
    except StorageError:
        return {}
    with closing(conn):
        if db.schema_version(conn) < probes.SCHEMA_VERSION:
            return {}
        newest = {name: probes.latest(conn, name) for name in agents}
    return {name: proof for name, proof in newest.items() if proof is not None}


def registry_warnings(agents: dict[str, acp_config.AgentSpec]) -> tuple[str, ...]:
    """Every quality nudge a thin ``agents.toml`` entry earns (decision 62; FR-100).

    Never a failure and never part of ``ok`` - an operator who has not filled in ``use_for`` or
    ``billing`` yet has broken nothing. ``doctor`` prints each line with :func:`render.warning`,
    which is why none of this lives in :class:`Check`: a caution the run goes on past, not a
    pass/fail verdict.
    """
    warnings: list[str] = []
    by_use_for: dict[str, list[str]] = {}
    for name, spec in sorted(agents.items()):
        if not spec.use_for:
            warnings.append(f"{name} declares no use_for; doctor cannot say what to route to it")
        else:
            by_use_for.setdefault(spec.use_for, []).append(name)
        if spec.billing is None:
            warnings.append(f"{name} declares no billing; its turns will never be metered")
        if spec.billing == acp_config.BILLING_OPENROUTER and spec.base_url_var is None:
            warnings.append(f"{name} is billed openrouter but declares no base_url_var")
        if spec.kind == acp_config.DEFAULT_KIND and settings_word(spec) == SETTINGS_ADD:
            lines = acp_config.render_template_fields(acp_config.TEMPLATES[name])
            warnings.append(
                f"{name} runs with its folder's settings; paste into [agents.{name}]: "
                f"{' and '.join(lines)}, or run harnessed agents add {name} --refresh"
            )
        if spec.tool_names and not spec.session_meta:
            warnings.append(f"{name} declares tool_names but no session_meta, so they do nothing")
    for text, names in sorted(by_use_for.items()):
        if len(names) > 1:
            warnings.append(f"{' and '.join(names)} share an identical use_for: {text!r}")
    return tuple(warnings)


def _registry_warnings(path: Path) -> tuple[str, ...]:
    """:func:`registry_warnings` over whatever ``path`` declares, or nothing for an unreached or
    broken registry - rung 2 already reports that state, and a warning would only repeat it."""
    try:
        agents = acp_config.load_agents(path)
    except ConfigError:
        return ()
    return registry_warnings(agents)


def _rung_gateway(settings: Settings) -> Rung:
    """Rung 3: the read-only gateway (v2), and the state is read off the app, never off a string.

    ``harnessed.gateway`` is imported here, inside the check, exactly as ``serve_cmd`` imports it
    (decision 21): with the extra absent the import fails and the rung is ``not yet`` naming the
    one command that installs it, never an import error at CLI start. With the extra present the
    check builds the app over the same database ``serve`` would bind and counts ``app.routes``.
    ``live`` follows that count and nothing else, which is what MUST rule 12 asks of a doctor
    that once reported a subsystem from a sentence about it: an app that imports and registers
    zero routes is ``broken``, not ``live``.
    """
    try:
        from harnessed.gateway import create_app
    except ImportError:
        remedy = GatewayExtraMissing.remedy
        return Rung(3, "Gateway", "not yet", f"{remedy} - v2, read-only GET plus SSE")
    database_path = (settings.home / db.DATABASE_FILENAME).resolve()
    try:
        app = create_app(database_path)
    except StorageError as exc:
        return Rung(3, "Gateway", "broken", f"the gateway cannot serve {database_path}: {exc}")
    route_count = len(app.routes)
    if route_count == 0:
        return Rung(3, "Gateway", "broken", "the gateway app imports but registers 0 routes")
    return Rung(3, "Gateway", "live", f"v2 - harnessed serve, {route_count} routes registered")


def _rung_dashboard(settings: Settings) -> Rung:
    """Rung 4: the one process, and its state is read off the app the way rung 3 reads its own.

    The extra absent is ``not yet`` with the install command, as for rung 3: ``harnessed.gateway``
    is imported here, inside the check, so a checkout without it reports a rung rather than
    failing at CLI start. With it present the check asks two things of the disk, in the order
    ``harnessed dashboard`` asks them: is the export staged in the installed package (else
    ``broken`` naming the build command, FR-66), and does the one app build over the same home
    the command would serve. ``live`` then follows the mounted apps' route tables, enumerated
    off the app and never compared as a string (MUST rule 12): the gateway mount registering
    zero routes is ``broken``, exactly as it is for rung 3.
    """
    try:
        import harnessed.gateway  # noqa: F401 - the extra, tested the way rung 3 tests it
        from harnessed import dashboard_app
        from harnessed.cli.dashboard_cmd import COMMAND, staged
    except ImportError:
        remedy = GatewayExtraMissing.remedy
        return Rung(4, "Dashboard", "not yet", f"{remedy} - v4, one process: the UI and /v1")
    export = dashboard_app.export_directory()
    if not staged(export):
        return Rung(
            4, "Dashboard", "broken", f"no dashboard export at {export}; run {ExportMissing.remedy}"
        )
    try:
        app = dashboard_app.create_app(settings.home)
    except StorageError as exc:
        return Rung(4, "Dashboard", "broken", f"the dashboard cannot serve {settings.home}: {exc}")
    mounted = _mounted_routes(app)
    if mounted.get(dashboard_app.GATEWAY_MOUNT, 0) == 0:
        return Rung(
            4, "Dashboard", "broken", f"the {dashboard_app.GATEWAY_MOUNT} mount registers 0 routes"
        )
    listing = ", ".join(f"{path} {count} routes" for path, count in mounted.items())
    return Rung(4, "Dashboard", "live", f"v4 - {COMMAND}, export at {export}, mounted: {listing}")


def _mounted_routes(app: Any) -> dict[str, int]:
    """Each mount's path and the number of routes its sub-app registers, in mount order.

    A mount carrying a sub-application exposes that application's route table as ``routes``; a
    static mount exposes none and counts 0, which is right, because it serves files, not routes.
    """
    counted: dict[str, int] = {}
    for route in app.routes:
        routes = getattr(route, "routes", None)
        if routes is None:
            continue
        counted[getattr(route, "path", "") or "/"] = len(routes)
    return counted


def _rung_router(settings: Settings, *, live: bool, decider: Decider | None = None) -> Rung:
    """Rung 5: FR-97's live rung (decision 57).

    Static ``not yet`` naming the command that climbs it, unless ``live`` asks for the climb
    itself: reading back a *prior* probe to decide this rung's state - the way rung 2 reads
    ``agent_probes`` - is ``router/decisions.py::probe_state``, ticket 186's. Until then this
    rung only ever reports what the invocation in front of it just did.
    """
    if decider is None and not settings.routing_on():
        return Rung(5, "router.probe", "not yet", settings.routing_off_reason() or "")
    if not live:
        return Rung(
            5,
            "router.probe",
            "not yet",
            "harnessed doctor --live - one noul, one choice and one score to the pinned "
            "router model",
        )
    conn = db.connect()
    try:
        return _probe_router_live(settings, conn, decider=decider)
    finally:
        conn.close()


def _probe_router_live(
    settings: Settings, conn: sqlite3.Connection, *, decider: Decider | None = None
) -> Rung:
    """Send :data:`_PROBE_QUESTIONS` to the pinned router model and record the outcome.

    ``decider`` is an injected seam exactly like ``corpus_root`` above it: a test hands in a
    stub transport one layer below the Decider's own opener, never a socket (MUST rule 18).
    """
    model = settings.router_model
    live_decider: Decider = JevDecider(model, settings) if decider is None else decider
    started = time.perf_counter()
    try:
        result = live_decider.decide(_PROBE_STATE, _PROBE_QUESTIONS)
    except DecisionError as exc:
        elapsed = time.perf_counter() - started
        probe = router_decisions.record_probe(
            conn,
            model=model,
            status=router_decisions.FAILED,
            latency_seconds=f"{elapsed:.3f}",
            cost=None,
            detail=f"fail: {exc}",
        )
        return Rung(5, "router.probe", "broken", probe.detail)
    elapsed = time.perf_counter() - started
    probe = router_decisions.record_probe(
        conn,
        model=result.model,
        status=router_decisions.PASSED,
        latency_seconds=f"{elapsed:.3f}",
        cost=result.cost,
        detail=f"pass: latency {elapsed:.3f}s, cost {result.cost}",
    )
    return Rung(5, "router.probe", "live", probe.detail)


def run_rungs(
    settings: Settings,
    *,
    agents_path: Path | None = None,
    live: bool = False,
    decider: Decider | None = None,
) -> tuple[Rung, ...]:
    """The ladder, in climbing order, computed from the environment and the working directory.

    ``agents_path`` is resolved in the body rather than as a default argument, so the registry a
    test points this at moves with it (the lesson ticket 028 recorded about injectable roots).
    ``live`` and ``decider`` reach only rung 5 (FR-97); every other rung is unaffected by either.
    """
    path = acp_config.DEFAULT_PATH if agents_path is None else agents_path
    return (
        _rung_cli(settings),
        _rung_agent(path, settings.home, routing_off=not settings.routing_on()),
        _rung_gateway(settings),
        _rung_dashboard(settings),
        _rung_router(settings, live=live, decider=decider),
    )


@contextmanager
def no_outbound() -> Iterator[list[tuple[str, int]]]:
    """Refuse every outbound socket connection and record the attempts.

    This is what makes "zero outbound connections" an assertion rather than a claim. The socket
    guard that covers the whole test session is FR-1's job; this one covers exactly the doctor
    run, so ``harnessed doctor --offline`` is honest even outside pytest.
    """
    attempts: list[tuple[str, int]] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def _record(address: Any) -> tuple[str, int]:
        if isinstance(address, tuple) and len(address) >= 2:
            return (str(address[0]), int(address[1]))
        return (str(address), 0)

    def guard(self: socket.socket, address: Any) -> Any:
        target = _record(address)
        attempts.append(target)
        raise ConfigError(
            f"offline contract violated: something tried to connect to {target[0]}:{target[1]}. "
            f"`harnessed doctor --offline` makes zero outbound connections."
        )

    socket.socket.connect = guard  # type: ignore[assignment,method-assign]
    socket.socket.connect_ex = guard  # type: ignore[assignment,method-assign]
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]


def _check_python() -> Check:
    found = ".".join(str(part) for part in sys.version_info[:3])
    wanted = ".".join(str(part) for part in REQUIRED_PYTHON)
    if sys.version_info[:2] != REQUIRED_PYTHON:
        return Check("python.version", "fail", f"needs {wanted}, found {found}")
    return Check("python.version", "ok", found)


def _check_fts5() -> Check:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
    except sqlite3.OperationalError as exc:
        return Check(
            "sqlite.fts5",
            "fail",
            f"FTS5 is not compiled into this CPython's SQLite ({exc}); semantic memory needs it",
        )
    finally:
        connection.close()
    return Check("sqlite.fts5", "ok", f"compiled in (sqlite {sqlite3.sqlite_version})")


def _check_provider_configured(settings: Settings) -> Check:
    """Is a provider set, and if it needs a key, is the key here? Asked in every mode.

    This used to be one check, ``provider.no_key_required``, that answered the *offline*
    question unconditionally - so a correctly configured live machine, key present, rung 1 live,
    was told ``fail`` in red and to switch to ``fake``. Ticket 051 split it: this check says what
    is configured; :func:`_check_provider_offline` says whether it is offline, and is only asked
    under ``--offline``. FR-29: doctor fails only when a configured rung is broken, and a keyed,
    live provider is the opposite of broken.
    """
    if settings.provider is None:
        # Not "skip": an unconfigured harness cannot run anything, so reporting it as absent
        # rather than as broken would be the same silence FR-27 exists to remove.
        return Check(
            "provider.configured",
            "fail",
            f"no provider is configured. Run `harnessed init`, or set {PROVIDER_VAR}=fake "
            f"to stay offline.",
        )
    var = settings.key_var_for(settings.provider)
    if var is None:
        return Check(
            "provider.configured",
            "ok",
            f"HARNESSED_PROVIDER={settings.provider} needs no API key",
        )
    if settings.api_key_for(settings.provider) is None:
        # The same sentence MissingCredentialError carries, not a second wording of it.
        return Check(
            "provider.configured",
            "fail",
            f"HARNESSED_PROVIDER={settings.provider} needs {var} and none is set. Put it in "
            f"a .env file in this directory, or export it - the environment wins.",
        )
    return Check(
        "provider.configured",
        "ok",
        f"HARNESSED_PROVIDER={settings.provider}, {var} present - configured and live",
    )


def _check_provider_offline(settings: Settings) -> Check:
    """Can this configuration run with zero outbound connections? Asked only under --offline.

    Stage 6 of the verify harness is ``doctor --offline``, and there a keyed provider really is a
    failure: the gate must not pass a checkout that reaches for the network. Unconfigured is
    also a failure here, for the same reason it is everywhere.
    """
    if settings.provider is None:
        return Check("provider.offline", "fail", "no provider is configured")
    var = settings.key_var_for(settings.provider)
    if var is None:
        return Check(
            "provider.offline",
            "ok",
            f"HARNESSED_PROVIDER={settings.provider} makes no outbound connections",
        )
    return Check(
        "provider.offline",
        "fail",
        f"HARNESSED_PROVIDER={settings.provider} needs {var}, so this configuration "
        f"is not offline. Set HARNESSED_PROVIDER=fake to stay offline.",
    )


def _check_wal() -> Check:
    """Open the one database the way every command opens it, and read the mode back off it."""
    path = db.database_path()
    try:
        conn = db.connect()
    except StorageError as exc:
        return Check("database.wal", "fail", f"{path} could not be opened or migrated: {exc}")
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    finally:
        conn.close()
    mode = "unknown" if row is None else str(row[0])
    if mode.casefold() != "wal":
        return Check("database.wal", "fail", f"{path} is in {mode} mode, not WAL")
    return Check("database.wal", "ok", f"{path} open in {mode} mode")


def _check_migrations(source: Path | None) -> Check:
    """Apply anything outstanding and re-verify every applied file's checksum.

    ``migrate()`` is idempotent and checks the recorded checksums before it applies anything, so
    calling it *is* the assertion: an edited committed migration fails here rather than being
    built on.
    """
    try:
        conn = db.connect()
    except StorageError as exc:
        return Check("migrations.current", "fail", f"the database would not open: {exc}")
    try:
        report = db.migrate(conn, source)
        version = db.schema_version(conn)
    except StorageError as exc:
        return Check("migrations.current", "fail", f"migrations are not current: {exc}")
    finally:
        conn.close()
    applied = "nothing outstanding" if not report.applied else f"applied {list(report.applied)}"
    return Check(
        "migrations.current",
        "ok",
        f"at version {version} of {len(report.migrations)}, checksums match, {applied}",
    )


def _check_fixtures(settings: Settings, root: Path) -> Check:
    """Load the committed corpus from disk and report the vendor directories beside it."""
    if not root.is_dir():
        return Check(
            "fixtures.corpus",
            "fail",
            f"no fixture corpus at {root}; the offline proof has nothing to answer from",
        )
    try:
        provider = FakeProvider(settings, root)
    except ProviderError as exc:
        return Check("fixtures.corpus", "fail", f"the corpus at {root} is malformed: {exc}")
    vendors = sorted(path.name for path in root.parent.iterdir() if path.is_dir())
    return Check(
        "fixtures.corpus",
        "ok",
        f"{len(provider.scopes)} scopes under {root.name}; vendors: {', '.join(vendors)}",
    )


def run_checks(
    settings: Settings,
    *,
    offline: bool = False,
    corpus_root: Path | None = None,
    migrations_dir: Path | None = None,
) -> tuple[Check, ...]:
    """Every declared check, in report order. Each one reads the disk; none reports a constant.

    ``offline`` is the one bit the provider check was missing (ticket 051): without it the check
    could not tell "is this configured" from "is this offline" and answered the second question
    to every caller. The offline check appears only when it was asked for.

    The two optional roots exist so a check can be run against a location where its subsystem is
    *not*, which is how the suite knows the check can fail. Both default to ``None`` and are
    resolved in the body, so monkeypatching :data:`CORPUS_ROOT` still moves the default.
    """
    checks = [
        _check_python(),
        _check_fts5(),
        _check_wal(),
        _check_migrations(migrations_dir),
        _check_fixtures(settings, CORPUS_ROOT if corpus_root is None else corpus_root),
        _check_provider_configured(settings),
    ]
    if offline:
        checks.append(_check_provider_offline(settings))
    return tuple(checks)


def doctor(
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Assert the offline contract and make zero connections."),
    ] = False,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Probe the pinned router model with one noul, one choice and one score, "
            "and record the result.",
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
    evidence: Annotated[
        Path | None,
        typer.Option(
            "--evidence",
            help="With --live, write the report here whole: the file is replaced only by a "
            "report with no broken rung, otherwise it is left as it was and the partial goes "
            "to <path>.incomplete.",
        ),
    ] = None,
) -> None:
    """Check that this checkout can run offline and deterministically."""
    if offline and live:
        render.failure(
            "--offline and --live are exclusive: one asserts zero connections, the other "
            "makes one on purpose."
        )
        raise typer.Exit(code=2)
    if evidence is not None and not live:
        render.failure("--evidence records a live report: pass --live with it.")
        raise typer.Exit(code=2)
    try:
        with Capture(evidence) as capture:
            ok = _report(offline=offline, live=live, as_json=as_json)
            capture.complete = ok
    except EvidenceIncomplete as exc:
        render.failure(str(exc))
        raise typer.Exit(code=1) from exc
    if not ok:
        raise typer.Exit(code=1)


@dataclass(frozen=True, slots=True)
class ScheduleLine:
    """The one schedule line (FR-142): ``ok`` or ``warn``, never a failure of any rung."""

    status: Literal["ok", "warn"]
    detail: str


def schedule_line(*, backend: installer.SchedulerBackend | None = None) -> ScheduleLine:
    """Is the OS tick installed (from the record), when did a schedule last run (from the ledger),
    and is the entry still there and unchanged (an injected query, never a run of the task)?

    A missing or changed entry warns and says how to repair it; it never fails an offline rung.
    """
    chosen = installer.current_backend() if backend is None else backend
    backend_name = chosen.label if chosen.reason is None else f"{chosen.label} ({chosen.reason})"
    with closing(db.connect()) as conn:
        newest = schedule.last_install(conn)
        ran = [row["last_run_at"] for row in schedule.rows(conn) if row["last_run_at"]]
    last = f"last ran {max(ran)}" if ran else "never ran"
    if newest is None or newest["uninstalled_at"] is not None:
        return ScheduleLine(
            "ok", f"{backend_name}: not installed (harnessed schedule install); {last}"
        )
    try:
        found = chosen.query()
    except StorageError as exc:
        return ScheduleLine("warn", f"{backend_name}: installed by record, could not check: {exc}")
    if found is None:
        return ScheduleLine(
            "warn",
            f"installed by record, missing from {backend_name}: run harnessed schedule install; "
            f"{last}",
        )
    if found != newest["entry_sha256"]:
        return ScheduleLine(
            "warn",
            f"installed by record, entry in {backend_name} changed since: run harnessed schedule "
            f"install; {last}",
        )
    return ScheduleLine("ok", f"{backend_name}: installed, entry matches the record; {last}")


def platform_section() -> tuple[host.SeamLine, ...]:
    """The six seams' lines in decision 95's order; each seam owns its own platform branch."""
    from harnessed.cli import onboarding  # onboarding imports this module for its ladder

    return (
        host.describe(),
        jail.describe(),
        proc.describe(),
        installer.describe(),
        onboarding.describe(),
        render.describe(),
    )


def _platform_text(line: host.SeamLine) -> str:
    return f"platform: {line.seam}: {line.implementation}" + (
        f" ({line.reason})" if line.reason else ""
    )


def _report(*, offline: bool, live: bool, as_json: bool) -> bool:
    """Run the checks and the ladder, print the report, and say whether nothing is broken."""
    settings = load()
    home = str(settings.home.resolve())
    env_path = Path(config.ENV_FILENAME)
    env_file = str(env_path.resolve()) if env_path.is_file() else None
    if offline:
        with no_outbound():
            results = run_checks(settings, offline=True)
            ladder = run_rungs(settings)
    elif live:
        results = run_checks(settings)
        ladder = run_rungs(settings, live=True)
    else:
        results = run_checks(settings)
        ladder = run_rungs(settings)

    for message in _registry_warnings(acp_config.DEFAULT_PATH):
        render.warning(message)
    scheduled = schedule_line()
    seams = platform_section()

    # A rung nobody has climbed is not a failure (FR-29); a rung they configured and broke is.
    ok = all(check.status != "fail" for check in results) and not any(
        rung.broken for rung in ladder
    )
    if as_json:
        render.json_object(
            {
                "ok": ok,
                "offline": offline,
                "home": home,
                "env_file": env_file,
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail} for c in results
                ],
                "schedule": {"status": scheduled.status, "detail": scheduled.detail},
                "platform": [
                    {"seam": s.seam, "implementation": s.implementation, "reason": s.reason}
                    for s in seams
                ],
                "rungs": [
                    {"number": r.number, "name": r.name, "state": r.state, "detail": r.detail}
                    for r in ladder
                ],
            }
        )
    else:
        render.line(f"home: {home}")
        render.line(f"env: {env_file or f'no {config.ENV_FILENAME} in this folder'}")
        render.checks(results)
        render.rungs(ladder)
        if scheduled.status == "warn":
            render.warning(f"schedule: {scheduled.detail}")
        else:
            render.line(f"schedule: {scheduled.detail}")
        for seam_line in seams:
            render.line(_platform_text(seam_line))
    return ok
