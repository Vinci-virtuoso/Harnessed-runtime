"""``harnessed agents add``, ``test`` and ``list`` - rung 2 of the ladder (FR-29, FR-90).

``add`` is the guided half. It writes an ``[agents.<name>]`` table from the template registry in
:mod:`harnessed.acp.config` into the operator's ``agents.toml``, so attaching a coding agent is a
command rather than a transcription exercise against a document. It is idempotent by the rule the
architecture states for every onboarding command: a second run reports the entry that is already
there and leaves the file byte-identical - it appends, and it never rewrites or reorders.

``test`` is the proving half: one real delegation with a trivial, non-destructive task, because
the only honest way to report that an agent works is to run it - spawn, ``initialize``,
``new_session``, ``prompt``, summary - and to say which protocol version came back. A version
mismatch surfaces as ``AcpVersionMismatch`` and an uninstalled executable as a
``DelegationError`` quoting the template's install command, both rendered by ``main``'s error
contract rather than as a spawn traceback.

No ledger is passed. ``delegate()`` is idempotent only when it is handed a store, and a probe
that answered from a recorded row - or refused as an exhausted attempt - would report on the
ledger rather than on the agent. Every result, passing or failing, is recorded beside it in
``agent_probes`` instead (ticket 089), which is the proof doctor's rung 2 reads.

``list`` is the reporting half (ticket 148), the twin of ``GET /v1/agents``: each declared agent's
newest recorded probe, read and never re-run, so its ``--json`` line and the route's body are the
same bytes (FR-37).

No command names an agent. Which agents exist is the template table's business, which is
what keeps adding a fourth one a row of data.
"""

from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path
from typing import Annotated, Final

import typer

from harnessed import config
from harnessed.acp import PROTOCOL_VERSION, cli_runner, jail, probes
from harnessed.acp import config as acp_config
from harnessed.acp.delegate import delegate
from harnessed.cli import render
from harnessed.errors import ConfigError, DelegationError
from harnessed.memory import db

__all__ = ["PROBE_ACCEPTANCE", "PROBE_TASK", "add", "agents_app", "list_agents", "test"]

#: The probe. Deliberately something an agent can satisfy by answering rather than by editing,
#: so a handshake check never leaves a working tree changed.
PROBE_TASK: Final = (
    "Reply with one short sentence confirming you received this prompt. Change no files."
)
PROBE_ACCEPTANCE: Final = "The agent completed the handshake and returned a summary."

agents_app = typer.Typer(
    name="agents",
    help="Work with the external coding agents declared in agents.toml.",
    no_args_is_help=True,
)


@agents_app.callback()
def _group() -> None:
    """Keep ``agents`` a command group (see ``eval_cmd._group`` for why)."""


@agents_app.command("add")
def add(
    name: Annotated[
        str | None, typer.Argument(help="Which template to write. Omit to list them.")
    ] = None,
    agents_path: Annotated[
        Path | None, typer.Option("--agents", help="Where the agent registry lives.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh", help="Rewrite the template-owned fields of the declared entry in place."
        ),
    ] = False,
) -> None:
    """Write a known-good agent entry into ``agents.toml``, or say what is already there.

    ``--refresh`` rewrites the one declared entry's template-owned lines (``session_meta``,
    ``tool_names``) in place and touches no other byte; without it ``add`` never rewrites.
    """
    path = acp_config.DEFAULT_PATH if agents_path is None else agents_path
    if name is None:
        if refresh:
            raise ConfigError(
                "--refresh needs the NAME argument: harnessed agents add NAME --refresh"
            )
        _list_templates(as_json)
        return

    template = acp_config.template_named(name)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    declared = name in acp_config.load_agents(path) if before.strip() else False

    if refresh and declared:
        _refresh(path, before, template, as_json)
        return

    if not declared:
        # Appended, never rewritten: everything the operator already had keeps its bytes and its
        # order, which is what makes a second run of this command a no-op rather than a diff.
        separator = "" if not before or before.endswith("\n\n") else "\n" if before else ""
        path.write_text(before + separator + acp_config.render_entry(template), encoding="utf-8")

    if as_json:
        render.json_object(
            {
                "agent": template.name,
                "added": not declared,
                "path": str(path),
                "command": template.command,
                "install": template.install,
            }
        )
        return
    if declared:
        render.line(f"{path} already declares {template.name}: nothing written")
        return
    render.rows(
        f"{path}: added {template.name}",
        (
            f"command   {template.command} {' '.join(template.args)}".rstrip(),
            f"install   {template.install}",
            f"prove it  harnessed agents test {template.name}",
        ),
    )


def _refresh(path: Path, before: str, template: acp_config.AgentTemplate, as_json: bool) -> None:
    """Set the template's own lines inside its declared table, or say there is nothing to do."""
    lines = acp_config.render_template_fields(template)
    after = acp_config.refresh_entry(before, template.name, lines)
    if after != before:
        path.write_text(after, encoding="utf-8")
        try:
            acp_config.load_agents(path)
        except ConfigError:
            path.write_text(before, encoding="utf-8")
            raise
    fields = tuple(line.split(" =", 1)[0] for line in lines) if after != before else ()
    if as_json:
        render.json_object({"agent": template.name, "refreshed": list(fields), "path": str(path)})
    elif not lines:
        render.line(f"{template.name} has no template-owned fields: nothing to refresh")
    elif after == before:
        render.line(f"{path} already carries {template.name}'s template values: nothing written")
    else:
        render.line(f"{path}: refreshed {template.name} ({', '.join(fields)})")


def _list_templates(as_json: bool) -> None:
    """Every template that can be added, with what it runs. Writes nothing."""
    if as_json:
        render.json_object(
            {
                "templates": [
                    {
                        "agent": template.name,
                        "command": template.command,
                        "install": template.install,
                        "summary": template.summary,
                    }
                    for template in acp_config.TEMPLATES.values()
                ]
            }
        )
        return
    width = max(len(name) for name in acp_config.TEMPLATES)
    render.rows(
        "Agents this harness can add. Pick one: harnessed agents add <name>",
        tuple(
            f"{template.name:<{width}}  {template.summary}"
            for template in acp_config.TEMPLATES.values()
        ),
    )


@agents_app.command("test")
def test(
    name: Annotated[
        str | None, typer.Argument(help="The declared agent. Omit for the default one.")
    ] = None,
    agents_path: Annotated[
        Path | None, typer.Option("--agents", help="Where the agent registry lives.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Spawn a declared agent, complete the handshake, and report what came back."""
    settings = config.load()
    path = acp_config.DEFAULT_PATH if agents_path is None else agents_path
    spec = acp_config.agent_named(path, name)
    settings.home.mkdir(parents=True, exist_ok=True)
    # Taken before the launch, so the proof names the entry and the program that were tested.
    mark = probes.fingerprint(spec, shutil.which(spec.command))
    is_cli = spec.kind == "cli"
    try:
        # The two branches `cli/routed.py::run_agent` takes, minus its output snapshot and metering,
        # which a probe must not do (ticket 221): a cli entry speaks no ACP, so a handshake would
        # time out on every one of them.
        if is_cli:
            delegation = cli_runner.run(spec, PROBE_TASK, PROBE_ACCEPTANCE)
        else:
            delegation = delegate(spec, PROBE_TASK, PROBE_ACCEPTANCE, home=settings.home)
    except DelegationError as exc:
        # A failure is recorded too, or a stale pass would outlive a broken agent.
        _record(settings.home, spec.name, mark, probes.FAILED, str(exc), is_cli=is_cli)
        raise
    detail = delegation.summary or f"session {delegation.session_id}"
    found = jail.detect()
    if is_cli:
        detail = probes.with_jail(found, detail)
    elif spec.billing == acp_config.BILLING_SUBSCRIPTION:
        # FR-116: a subscription entry's proof says whether its login is live (ticket 175).
        detail = probes.with_login(delegation.login, detail)
    _record(settings.home, spec.name, mark, probes.PASSED, detail, is_cli=is_cli)
    # The version is reported rather than negotiated here: `delegate()` raises
    # `AcpVersionMismatch` unless the agent answered `initialize` with exactly this number, so a
    # delegation that returned at all is a delegation that agreed on it. A cli entry negotiates
    # none, so it reports `null`.
    protocol_version = None if is_cli else PROTOCOL_VERSION
    payload: dict[str, object] = {
        "agent": spec.name,
        "kind": spec.kind,
        "protocol_version": protocol_version,
        **delegation.as_dict(),
    }
    if as_json:
        render.json_object(payload)
    elif is_cli:
        render.rows(
            f"agent {spec.name}: {delegation.status}",
            (f"jail {probes.jail_label(found)}", delegation.summary),
        )
    else:
        render.rows(
            f"agent {spec.name}: {delegation.status}",
            (
                f"protocol version {PROTOCOL_VERSION}",
                *((word,) if (word := probes.login_word(detail)) else ()),
                f"session {delegation.session_id}",
                delegation.summary,
            ),
        )


def _record(
    home: Path, agent: str, mark: str, status: str, detail: str, *, is_cli: bool = False
) -> None:
    """Append one probe result to the database ``doctor`` reads it back from.

    A cli probe stores ``0`` for the version (the column is ``NOT NULL`` and migrations are never
    edited); :func:`probes.states` serves ``null`` for it.
    """
    with closing(db.connect(home / db.DATABASE_FILENAME)) as conn:
        probes.record(
            conn,
            agent=agent,
            fingerprint=mark,
            status=status,
            protocol_version=0 if is_cli else PROTOCOL_VERSION,
            detail=detail,
        )


@agents_app.command("list")
def list_agents(
    agents_path: Annotated[
        Path | None, typer.Option("--agents", help="Where the agent registry lives.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print every declared agent with its newest recorded probe. Runs no handshake.

    ``--json`` prints ``probes.json_payload`` for ``probes.states``, the two callables
    ``GET /v1/agents`` serves.
    """
    path = acp_config.DEFAULT_PATH if agents_path is None else agents_path
    with closing(db.connect()) as conn:
        rows = probes.states(conn, path)
    if as_json:
        render.json_object(probes.json_payload(rows))
        return
    if not rows:
        render.line("no agent CLIs configured: see `harnessed agents add`")
        return
    render.rows(
        f"{len(rows)} declared agent(s), each with its newest recorded probe",
        tuple(f"{row.agent:<12} {_word(row)}  {_proof(row)}" for row in rows),
    )


def _word(row: probes.AgentState) -> str:
    """The Models card's word: ``live`` only on a pass that matches the launch as it stands."""
    if not row.probed:
        return "unproven"
    if not row.fingerprint_matches:
        return "stale"
    return "live" if row.status == probes.PASSED else "failed"


def _proof(row: probes.AgentState) -> str:
    retest = f"harnessed agents test {row.agent}"
    if not row.probed:
        return f"never probed - prove it: {retest}"
    version = "none" if row.protocol_version is None else row.protocol_version
    return f"{row.status} {row.tested_at} protocol {version}: {row.detail} - {retest}"
