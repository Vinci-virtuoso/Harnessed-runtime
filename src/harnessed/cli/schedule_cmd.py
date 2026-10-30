"""``harnessed schedule add|list|remove|install|uninstall`` - the rules and the one OS tick
(FR-142, decision 79).

``add`` stores a rule in the ledger (``tasks/schedule.py`` computes ``next_due_at``); ``install``
puts the one fixed entry in the operating system's scheduler through the backend
``tasks/installer.py`` picks, and records it. Nothing here names a platform or a scheduler tool.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from harnessed.cli import render
from harnessed.memory import db
from harnessed.tasks import installer, schedule

__all__ = [
    "add_schedule",
    "install_schedule",
    "list_schedules",
    "remove_schedule",
    "schedule_app",
    "uninstall_schedule",
]

schedule_app = typer.Typer(
    name="schedule",
    help="When queued tasks run on their own: rules in the ledger, one tick in the OS scheduler.",
    no_args_is_help=True,
)

_JSON = Annotated[
    bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
]


@schedule_app.callback()
def _group() -> None:
    """Keep ``schedule`` a command group (see ``eval_cmd._group`` for why)."""


@schedule_app.command("add")
def add_schedule(
    every: Annotated[
        str | None, typer.Option("--every", help="A duration, 15m upward: 15m, 6h, 1d.")
    ] = None,
    cron: Annotated[
        str | None, typer.Option("--cron", help='Five fields, e.g. "0 7 * * 1-5".')
    ] = None,
    as_json: _JSON = False,
) -> None:
    """Store a schedule. It runs nothing itself; the installed tick does."""
    conn = db.connect()
    try:
        row = schedule.add(conn, every=every, cron=cron)
    finally:
        conn.close()
    if as_json:
        render.json_object(schedule.json_payload(row))
        return
    render.line(f"{row['schedule_id']}: {row['rule_words']}, next {row['next_due_at']}")


@schedule_app.command("list")
def list_schedules(as_json: _JSON = False) -> None:
    """Print every schedule with whether the OS tick is installed."""
    conn = db.connect()
    try:
        found = schedule.listing(conn)
    finally:
        conn.close()
    if as_json:
        render.json_object(schedule.listing_payload(found))
        return
    if not found:
        render.line("no schedules")
        return
    render.rows(
        f"{len(found)} schedule(s)",
        tuple(
            f"{row['schedule_id']}  {row['rule_words']}  next {row['next_due_at']}  "
            f"installed {row['installed']}"
            for row in found
        ),
    )


@schedule_app.command("remove")
def remove_schedule(
    schedule_id: Annotated[str, typer.Argument(help="The id `schedule add` printed.")],
    as_json: _JSON = False,
) -> None:
    """Remove a schedule from the ledger."""
    conn = db.connect()
    try:
        schedule.remove(conn, schedule_id)
    finally:
        conn.close()
    if as_json:
        render.json_object({"schedule_id": schedule_id, "removed": "true"})
        return
    render.line(f"removed: {schedule_id}")


@schedule_app.command("install")
def install_schedule(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would be written and write nothing.")
    ] = False,
) -> None:
    """Install the one OS entry that runs `harnessed task run --due` every 15 minutes."""
    backend = installer.current_backend()
    home = installer.check_home()
    entry = installer.Entry(python=sys.executable, cwd=str(Path.cwd()), home=home)
    rendered = backend.install(entry, dry_run=dry_run)
    if dry_run:
        render.line(
            f"{backend.label}, dry run: nothing written.\n{rendered.command}\n{rendered.body}"
        )
        return
    conn = db.connect()
    try:
        recorded = schedule.record_install(conn, backend.where, rendered)
    finally:
        conn.close()
    render.line(
        f"{'installed' if recorded else 'already installed'} in {backend.label}: "
        f"{rendered.entry_name}\n{rendered.body}"
    )
    shown, caution = backend.after_install()
    if shown:
        render.line("\n".join(shown))
    if caution:
        render.warning(caution)


@schedule_app.command("uninstall")
def uninstall_schedule() -> None:
    """Remove the OS entry. Running it again says there was nothing to remove."""
    backend = installer.current_backend()
    removed = backend.uninstall(dry_run=False)
    conn = db.connect()
    try:
        schedule.record_uninstall(conn)
    finally:
        conn.close()
    render.line(f"removed from {backend.label}" if removed else f"not installed in {backend.label}")
