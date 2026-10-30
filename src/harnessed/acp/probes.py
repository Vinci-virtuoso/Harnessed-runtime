"""The ``agent_probes`` table - what ``harnessed agents test`` proved, and when (FR-29, FR-42).

Doctor's rung 2 cannot call an agent ``live`` from a file on PATH (ticket 088); only a handshake
proves an agent speaks ACP. This module records every probe's result, passing and failing, and
answers "what is the newest one for this agent" so doctor has proof to read.

**Beside the delegation ledger, never through it.** That ledger is idempotent by design: a task
hash already on record returns its recorded outcome instead of running, so a second probe would
replay the first rather than prove the agent again. Here every probe is a new row.

**The Models page reads the same proof** (FR-90, ticket 148): :func:`states` is one row per agent
in ``agents.toml``, its newest probe and whether that probe's fingerprint matches the launch as
it stands now, or ``probed: false`` with null fields when the agent was never tested. It reads a
recorded probe and runs no handshake of its own; ``GET /v1/agents`` and ``harnessed agents list
--json`` both print :func:`json_payload` of it.

**The fingerprint withdraws a stale proof.** It is taken over what launches the agent - the
entry's ``command``, ``args`` and ``cwd``, its ``session_meta`` and ``tool_names``, plus the
resolved executable - so editing the entry or
reinstalling the program leaves the old pass matching nothing. Policy keys (``mode``, ``allow``,
``deny``) do not change whether an agent speaks ACP and are left out.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from harnessed import clock
from harnessed.acp import config as acp_config
from harnessed.acp import jail
from harnessed.acp.config import AgentSpec
from harnessed.memory import db, redact

__all__ = [
    "FAILED",
    "LOGIN_LIVE",
    "LOGIN_UNKNOWN",
    "PASSED",
    "SCHEMA_VERSION",
    "AgentState",
    "Probe",
    "fingerprint",
    "first_line",
    "jail_label",
    "json_payload",
    "latest",
    "login_word",
    "record",
    "states",
    "with_jail",
    "with_login",
]

#: The handshake completed and a summary came back.
PASSED: Final = "passed"
#: The probe ended in a ``DelegationError`` (a mismatch, a timeout, an agent that died).
FAILED: Final = "failed"

#: A subscription entry's handshake advertised a login and opened a session on it (FR-116).
LOGIN_LIVE: Final = "login live"
#: A subscription entry's handshake said nothing about a login, so the probe cannot say either.
LOGIN_UNKNOWN: Final = "login unknown"
_LOGIN_WORDS: Final = (LOGIN_LIVE, LOGIN_UNKNOWN)

#: The migration that creates the table. A database behind it holds no proof.
SCHEMA_VERSION: Final = 8

_INSERT: Final = (
    "INSERT INTO agent_probes "
    "(agent, fingerprint, status, protocol_version, detail, tested_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_LATEST: Final = (
    "SELECT agent, fingerprint, status, protocol_version, detail, tested_at "
    "FROM agent_probes WHERE agent = ? ORDER BY probe_id DESC LIMIT 1"
)


@dataclass(frozen=True, slots=True)
class Probe:
    """One recorded probe, as stored."""

    agent: str
    fingerprint: str
    status: str
    protocol_version: int
    detail: str
    tested_at: str

    @property
    def date(self) -> str:
        """The day it was proven or refused, which is all a ladder line needs."""
        return self.tested_at[:10]


def fingerprint(spec: AgentSpec, executable: str | None) -> str:
    """sha256 over canonical JSON of what launches ``spec``, with ``executable`` resolved."""
    launch = {
        "command": spec.command,
        "args": list(spec.args),
        "cwd": spec.cwd,
        "executable": executable,
        "session_meta": spec.session_meta,
        "tool_names": {kind: list(names) for kind, names in spec.tool_names.items()},
    }
    canonical = json.dumps(launch, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def first_line(text: str) -> str:
    """The first non-empty line of ``text``, stripped: a probe detail is one line."""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def with_login(login: bool | None, detail: str) -> str:
    """``detail`` led by the login word a subscription entry's probe carries (ticket 175).

    The word rides in ``detail`` rather than a column, so recording it adds no migration.
    """
    return f"{LOGIN_LIVE if login else LOGIN_UNKNOWN}: {detail}"


def with_jail(found: jail.Jail, detail: str) -> str:
    """``detail`` led by the jail a ``cli`` entry's probe ran under (ticket 217).

    ``jail=<kind>: `` or ``jail=none (<reason>): `` where the host refused; the reason is scrubbed
    here, where it is recorded. Like :func:`with_login`, it rides in ``detail``: no migration.
    """
    return f"jail={jail_label(found)}: {detail}"


def jail_label(found: jail.Jail) -> str:
    """``<kind>`` or ``<kind> (<scrubbed reason>)``, the jail as a probe states it."""
    reason = redact.scrub(found.reason)
    return f"{found.kind} ({reason})" if reason else found.kind


def login_word(detail: str) -> str | None:
    """The login word ``detail`` leads with, or ``None`` when the probe carries none."""
    return next((word for word in _LOGIN_WORDS if detail.startswith(f"{word}: ")), None)


def record(
    conn: sqlite3.Connection,
    *,
    agent: str,
    fingerprint: str,
    status: str,
    protocol_version: int,
    detail: str,
) -> Probe:
    """Append one probe result, scrubbed and cut to one line, and return it as stored."""
    probe = Probe(
        agent=redact.scrub(agent),
        fingerprint=fingerprint,
        status=status,
        protocol_version=protocol_version,
        detail=redact.scrub(first_line(detail)),
        tested_at=clock.isoformat(clock.now()),
    )
    db.write(
        conn,
        (
            (
                _INSERT,
                (
                    probe.agent,
                    probe.fingerprint,
                    probe.status,
                    probe.protocol_version,
                    probe.detail,
                    probe.tested_at,
                ),
            ),
        ),
    )
    return probe


def latest(conn: sqlite3.Connection, agent: str) -> Probe | None:
    """The newest probe recorded for ``agent``, or ``None`` when it was never tested.

    Newest by insertion order, not by ``tested_at``: two probes in one second (or under a frozen
    clock) tie on the timestamp and never on the row id.
    """
    row = conn.execute(_LATEST, (agent,)).fetchone()
    if row is None:
        return None
    return Probe(
        agent=str(row[0]),
        fingerprint=str(row[1]),
        status=str(row[2]),
        protocol_version=int(row[3]),
        detail=str(row[4]),
        tested_at=str(row[5]),
    )


@dataclass(frozen=True, slots=True)
class AgentState:
    """One declared agent and its newest recorded probe, as ``GET /v1/agents`` serves it.

    Every probe field but ``agent`` and ``probed`` is ``None`` when no probe exists, so an
    unproven agent is a row of documented nulls rather than a row with keys missing. The
    registry fields (``kind`` through ``confined``) are the entry as declared, read regardless
    of whether it was ever probed (decision 62, FR-120).
    """

    agent: str
    status: str | None
    protocol_version: int | None
    detail: str | None
    tested_at: str | None
    fingerprint_matches: bool | None
    probed: bool
    kind: str
    use_for: str
    not_for: str
    billing: str | None
    produces: str | None
    #: ``False`` for a ``cli`` entry (decision 62's jail, "stated rather than implied"); for an
    #: ``acp`` entry ``True`` only when it launches under Harnessed's settings (a non-empty
    #: ``session_meta``, ticket 213) rather than its folder's.
    confined: bool
    #: ``harnessed`` or ``folder`` for an ``acp`` entry, ``None`` for a ``cli`` entry (G-60).
    settings: str | None
    #: ``bwrap``, ``job_object`` or ``none`` for a ``cli`` entry, ``None`` for an ``acp`` entry.
    jail: str | None
    #: The jail self-test's reason, or why the folder's settings cannot be overridden, else
    #: ``None``.
    confined_reason: str | None


def states(
    conn: sqlite3.Connection, path: Path = acp_config.DEFAULT_PATH
) -> tuple[AgentState, ...]:
    """One row per agent declared in ``path``, in the registry's order, with its newest probe.

    ``fingerprint_matches`` is ticket 089's rule, the one doctor's rung 2 applies: the probe's
    fingerprint against :func:`fingerprint` of the entry and executable as they stand now. No
    registry declares no agents, so it is no rows; a registry that will not load raises the
    :class:`~harnessed.errors.ConfigError` doctor reports as ``broken``.
    """
    if not path.exists():
        return ()
    rows = []
    for spec in acp_config.load_agents(path).values():
        # A `cli` entry is confined only where this host has a `bwrap` jail (ticket 214, measured
        # per host, "stated rather than implied"); an `acp` entry only with a session_meta (213).
        settings = acp_config.settings_of(spec)
        jail_kind: str | None = None
        reason: str | None = acp_config.NO_OVERRIDE_REASON if acp_config.no_override(spec) else None
        if settings is not None:
            confined = settings == acp_config.SETTINGS_HARNESSED
        else:
            found = jail.detect()
            jail_kind = found.kind
            confined = found.kind == jail.BWRAP
            reason = redact.scrub(found.reason) or None
        proof = latest(conn, spec.name)
        if proof is None:
            rows.append(
                AgentState(
                    agent=spec.name,
                    status=None,
                    protocol_version=None,
                    detail=None,
                    tested_at=None,
                    fingerprint_matches=None,
                    probed=False,
                    kind=spec.kind,
                    use_for=spec.use_for,
                    not_for=spec.not_for,
                    billing=spec.billing,
                    produces=spec.produces,
                    confined=confined,
                    settings=settings,
                    jail=jail_kind,
                    confined_reason=reason,
                )
            )
            continue
        current = fingerprint(spec, shutil.which(spec.command))
        rows.append(
            AgentState(
                agent=spec.name,
                status=proof.status,
                # A cli entry negotiates no protocol: its stored `0` is served as `null`.
                protocol_version=None
                if spec.kind != acp_config.DEFAULT_KIND
                else proof.protocol_version,
                detail=proof.detail,
                tested_at=proof.tested_at,
                fingerprint_matches=proof.fingerprint == current,
                probed=True,
                kind=spec.kind,
                use_for=spec.use_for,
                not_for=spec.not_for,
                billing=spec.billing,
                produces=spec.produces,
                confined=confined,
                settings=settings,
                jail=jail_kind,
                confined_reason=reason,
            )
        )
    return tuple(rows)


def json_payload(rows: tuple[AgentState, ...]) -> dict[str, object]:
    """The one wire shape for :func:`states`: ``{"agents": [asdict(row), ...]}``.

    ``GET /v1/agents`` and ``harnessed agents list --json`` both print this, so the body and the
    line are the same bytes (FR-37).
    """
    return {"agents": [asdict(row) for row in rows]}
