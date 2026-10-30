"""``harnessed task add|list|show|cancel`` - the verbs of the tasks ledger (FR-140, decision 76).

Each is a handful of lines over ``tasks/store.py``: parse, open the one database, call, render.
``add`` is ``store.queue`` with ``via`` ``cli``, the call ``POST /control/v1/tasks`` also makes with
``console``. ``cancel`` hands the store the denial of a parked permission (``rule: operator``,
``answered_via: cli``) because ``tasks`` imports no ``acp``. ``run`` hands a task to
``cli/task_run.py`` (ticket 240).
"""

from __future__ import annotations

import contextlib
from typing import Annotated

import typer

from harnessed import config
from harnessed.acp import pending
from harnessed.cli import render, task_run
from harnessed.errors import ParameterError, PermissionAnswered, SurfaceNotFound
from harnessed.memory import db
from harnessed.tasks import store

__all__ = [
    "add_task",
    "cancel_task",
    "list_tasks",
    "run_task_cmd",
    "show_task",
    "task_app",
    "task_totals",
]

task_app = typer.Typer(
    name="task",
    help="Jobs you hand the harness: queue one, look at the ledger, cancel one.",
    no_args_is_help=True,
)

_JSON = Annotated[
    bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
]


@task_app.callback()
def _group() -> None:
    """Keep ``task`` a command group (see ``eval_cmd._group`` for why)."""


def added_payload(row: dict[str, str | None]) -> dict[str, str | None]:
    """What ``task add`` answers: the id, the three budget values and where they came from."""
    return {
        "task_id": row["task_id"],
        "budget_iterations": row["budget_iterations"],
        "budget_seconds": row["budget_seconds"],
        "budget_dollars": row["budget_dollars"],
        "budget_source": row["budget_source"],
    }


@task_app.command("add")
def add_task(
    text: Annotated[str, typer.Argument(help="What the task should do.")],
    cwd: Annotated[
        str | None, typer.Option("--cwd", help="Working directory; default here.")
    ] = None,
    entry: Annotated[
        str | None, typer.Option("--entry", help="Restrict the route to one entry.")
    ] = None,
    budget_iterations: Annotated[
        int | None, typer.Option("--budget-iterations", help="Loop turns and agent hops allowed.")
    ] = None,
    budget_seconds: Annotated[
        int | None, typer.Option("--budget-seconds", help="Seconds of work allowed.")
    ] = None,
    budget_dollars: Annotated[
        str | None, typer.Option("--budget-dollars", help="Dollars of spend allowed, e.g. 0.50.")
    ] = None,
    allow_dirty: Annotated[
        bool, typer.Option("--allow-dirty", help="Let it run in a working tree with changes.")
    ] = False,
    as_json: _JSON = False,
) -> None:
    """Queue a task. It runs nothing; `task run` does."""
    conn = db.connect()
    try:
        row = store.queue(
            conn,
            text,
            cwd=cwd,
            entry=entry,
            budget_iterations=budget_iterations,
            budget_seconds=budget_seconds,
            budget_dollars=budget_dollars,
            allow_dirty=allow_dirty,
            via="cli",
        )
    finally:
        conn.close()
    answer = added_payload(row)
    if as_json:
        render.json_object(answer)
        return
    render.line(
        f"queued {answer['task_id']}: {answer['budget_iterations']} steps, "
        f"{answer['budget_seconds']} s, ${answer['budget_dollars']} ({answer['budget_source']})"
    )


@task_app.command("list")
def list_tasks(
    state: Annotated[str | None, typer.Option("--state", help="One state, e.g. `queued`.")] = None,
    session: Annotated[
        str | None, typer.Option("--session", help="Only the task this session belongs to.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="At most this many, newest first.")] = (
        store.DEFAULT_LIMIT
    ),
    as_json: _JSON = False,
) -> None:
    """Print tasks, newest first (`needs_human` ones longest waiting first)."""
    conn = db.connect()
    try:
        rows = store.listing(conn, state=state, session=session, limit=limit)
        if as_json:
            render.json_object(store.listing_payload(rows))
            return
    finally:
        conn.close()
    if not rows:
        render.line("no tasks")
        return
    render.rows(
        f"{len(rows)} task(s)",
        tuple(f"{row['task_id']}  {row['state']}  {row['text']}" for row in rows),
    )


@task_app.command("show")
def show_task(
    task_id: Annotated[str, typer.Argument(help="The id `task add` printed.")],
    as_json: _JSON = False,
) -> None:
    """Print one task."""
    conn = db.connect()
    try:
        row = store.get(conn, task_id)
        if row is None:
            raise SurfaceNotFound(f"no task {task_id!r}: the tasks ledger holds none.")
        shown = store.json_payload(row)
    finally:
        conn.close()
    if as_json:
        render.json_object(shown)
        return
    render.rows(
        task_id,
        tuple(
            f"{key}: {value}"
            for key, value in shown.items()
            if value is not None and not key.endswith("_reason")
        ),
    )


@task_app.command("totals")
def task_totals(as_json: _JSON = False) -> None:
    """Count every task, all time, by state, with how many were completed unattended."""
    conn = db.connect()
    try:
        counted = store.totals_payload(store.totals(conn))
    finally:
        conn.close()
    if as_json:
        render.json_object(counted)
        return
    render.rows(
        f"tasks, all time, counted {counted['counted_at']}",
        tuple(f"{key}: {value}" for key, value in counted.items() if key != "counted_at"),
    )


@task_app.command("run")
def run_task_cmd(
    task_id: Annotated[
        str | None, typer.Argument(help="The id `task add` printed; leave out with --due.")
    ] = None,
    message: Annotated[
        str | None,
        typer.Option("--message", help="Resume a paused task with your own words."),
    ] = None,
    due: Annotated[
        bool,
        typer.Option(
            "--due",
            help="The scheduler's command: run the queued tasks of every due schedule.",
        ),
    ] = False,
    provider: Annotated[
        str | None, typer.Option(config.PROVIDER_FLAG, help="Use this provider for this run only.")
    ] = None,
    allow_dirty: Annotated[
        bool,
        typer.Option("--allow-dirty", help="Let this run go into a working tree with changes."),
    ] = False,
    as_json: _JSON = False,
) -> None:
    """Run a queued task to its budget, or resume one that is waiting on you.

    The task goes message by message through the same routing as `harnessed run` until the router
    says it is done, its budget is spent, or it pauses for you. A task paused on an uncertain
    route resumes on the same session, with `--message` as your own text. One waiting on an agent's
    request is answered first (`harnessed permissions answer`). `--due` is what the operating
    system's scheduler runs (`harnessed schedule install`): it runs each due schedule's queued
    tasks one at a time and exits 0 having run nothing when none is due.
    """
    if due:
        if task_id is not None or message is not None:
            raise ParameterError("--due runs what is due: leave out the task id and --message.")
        done = task_run.run_due(provider=provider)
        if as_json:
            render.json_object(
                {
                    "schedules": ",".join(done.schedules),
                    "ran": ",".join(done.ran),
                    "in_flight": "true" if done.in_flight else "false",
                    **(
                        {}
                        if done.running is None
                        else {
                            "task_id": done.running.task_id,
                            "text": done.running.text,
                            "message": str(done.running),
                        }
                    ),
                }
            )
            return
        render.line(
            (str(done.running) if done.running else "in flight: waiting for the next tick")
            if done.in_flight
            else f"ran {len(done.ran)} task(s) for {len(done.schedules)} due schedule(s)"
        )
        return
    if task_id is None:
        raise ParameterError("name the task to run, or pass --due.")
    row = task_run.run_task(
        task_id,
        first_message=message,
        author="task" if message is None else "operator",
        provider=provider,
        allow_dirty=allow_dirty,
    )
    if as_json:
        render.json_object(store.payload(row))
        return
    reason = row["stop_reason"] or row["pause_reason"]
    render.line(f"{row['state']}{'' if reason is None else f' ({reason})'}: {row['task_id']}")


@task_app.command("cancel")
def cancel_task(
    task_id: Annotated[str, typer.Argument(help="The id `task add` printed.")],
    as_json: _JSON = False,
) -> None:
    """Cancel a queued task, or one waiting on you (its pending request is denied)."""
    conn = db.connect()

    def deny(permission_id: str) -> None:
        # Somebody answering first is not a reason to keep the task: it is cancelled all the same.
        with contextlib.suppress(PermissionAnswered):
            pending.answer(conn, permission_id, "deny", rule="operator", via="cli")

    try:
        row = store.cancel(conn, task_id, deny=deny)
    finally:
        conn.close()
    if as_json:
        render.json_object({"task_id": row["task_id"], "state": row["state"]})
        return
    render.line(f"{row['state']}: {row['task_id']}")
