"""The Typer application and the ``harnessed`` console script entry point.

Every v1 command group hangs off this one app: ``run``, ``eval run``, ``improve run``,
``improve promote``, ``memory``, ``agents test``, ``gate`` and ``doctor``, plus v2's ``serve``
and ``sessions list``, the twin of the session listing FR-48 serves, and v4's
``dashboard``, the one process that serves the UI and mounts the gateway (decision 46), and
``runs totals``, the twin of the journal totals FR-67 serves, ``events gate``, the twin of
the gate split FR-69 serves, ``events totals`` and ``memory totals``, FR-70's two twins, and
``prices refresh``, the one command that asks a provider for a price, with ``prices list``, the
twin of the stored rows FR-73 serves, and ``spend``, the twin of the journal's cost FR-74 serves,
and ``providers list``, the twin of the provider states FR-89 serves.
Each lives in its own module and each is a handful of lines - parse, compose, call one
subsystem callable, render.

``main`` is the one place in the repository that catches broadly, and it catches ``HarnessError``
only: a named error is rendered to stderr with exit code 1, and anything else propagates as a
traceback, because an unexpected traceback is a bug report and must not be dressed up as a
friendly message. Typer's pretty exception hook is disabled for exactly that reason - it would
replace the traceback with a panel.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Annotated

import typer

from harnessed.cli import render
from harnessed.cli.agents_cmd import agents_app
from harnessed.cli.dashboard_cmd import dashboard
from harnessed.cli.decisions_cmd import decisions_app
from harnessed.cli.doctor_cmd import doctor
from harnessed.cli.eval_cmd import eval_app
from harnessed.cli.events_cmd import events_app
from harnessed.cli.gate_cmd import gate
from harnessed.cli.health_cmd import health
from harnessed.cli.improve_cmd import improve_app
from harnessed.cli.init_cmd import init
from harnessed.cli.learn_cmd import learn
from harnessed.cli.memory_cmd import memory_app
from harnessed.cli.outputs_cmd import outputs_app
from harnessed.cli.permissions_cmd import permissions_app
from harnessed.cli.prices_cmd import prices_app
from harnessed.cli.providers_cmd import providers_app
from harnessed.cli.router_cmd import router_app
from harnessed.cli.run_cmd import run, runs_app
from harnessed.cli.schedule_cmd import schedule_app
from harnessed.cli.serve_cmd import serve
from harnessed.cli.sessions_cmd import sessions_app
from harnessed.cli.spend_cmd import spend
from harnessed.cli.task_cmd import task_app
from harnessed.errors import HarnessError

__all__ = ["app", "main"]

app = typer.Typer(
    name="harnessed",
    help="A legible, offline-deterministic agent harness.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _show_version(value: bool) -> None:
    """Print the installed distribution's version and stop (decision 90)."""
    if value:
        render.line(version("harnessed-runtime"))
        raise typer.Exit


@app.callback()
def _root(
    show_version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Print the installed version and exit.",
        ),
    ] = False,
) -> None:
    """Keep ``harnessed`` a command group.

    Typer collapses a single-command app into a bare command, which would make
    ``harnessed doctor`` an unexpected extra argument. The callback pins the group shape; each
    sub-app declares its own for the same reason.
    """


app.command("init")(init)
app.command("run")(run)
app.command("gate")(gate)
app.command("doctor")(doctor)
app.command("serve")(serve)
app.command("dashboard")(dashboard)
app.command("spend")(spend)
app.command("health")(health)
app.command("learn")(learn)
app.add_typer(eval_app, name="eval")
app.add_typer(improve_app, name="improve")
app.add_typer(memory_app, name="memory")
app.add_typer(agents_app, name="agents")
app.add_typer(decisions_app, name="decisions")
app.add_typer(router_app, name="router")
app.add_typer(sessions_app, name="sessions")
app.add_typer(runs_app, name="runs")
app.add_typer(events_app, name="events")
app.add_typer(prices_app, name="prices")
app.add_typer(providers_app, name="providers")
app.add_typer(outputs_app, name="outputs")
app.add_typer(permissions_app, name="permissions")
app.add_typer(task_app, name="task")
app.add_typer(schedule_app, name="schedule")


def main() -> None:
    """Console script entry point. Maps a HarnessError to stderr plus exit code 1."""
    try:
        app()
    except HarnessError as exc:
        render.error(exc)
        raise SystemExit(1) from exc
