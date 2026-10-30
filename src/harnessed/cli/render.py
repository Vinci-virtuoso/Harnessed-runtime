"""The only module that prints. Rich for humans, one JSON object for machines.

A ``print()`` anywhere else in the package is a bug: observable moments publish events and the
CLI renders them. Every function here takes a finished result and writes it; none of them
compute, compare or decide anything, which is what keeps a number an operator reads identical to
the number ``--json`` hands a parser.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from rich.console import Console
from rich.markup import escape

from harnessed.errors import ProviderNotConfiguredError
from harnessed.host import SeamLine
from harnessed.serialize import canonical

if TYPE_CHECKING:  # pragma: no cover - typing only
    from harnessed.cli.doctor_cmd import Check, Rung
    from harnessed.evals.runner import EvalRun
    from harnessed.loop.engine import LoopResult

__all__ = [
    "checks",
    "error",
    "eval_run",
    "failure",
    "json_object",
    "line",
    "reply",
    "rows",
    "rungs",
    "uncertain_stop",
    "warning",
]

_STATUS_MARK = {
    "ok": "[green]ok  [/green]",
    "skip": "[yellow]skip[/yellow]",
    "fail": "[red]fail[/red]",
}

_RUNG_MARK = {
    "live": "[green]live    [/green]",
    "declared": "[yellow]declared[/yellow]",
    "not yet": "[cyan]not yet [/cyan]",
    "broken": "[red]broken  [/red]",
}


class _Renderable(Protocol):
    name: str
    status: str
    detail: str


def _utf8_when_redirected() -> None:
    """Make a redirected stdout UTF-8, whatever the console code page says (Known defect (c)).

    A Windows console reports cp1252, and redirecting stdout to a file or a pipe keeps that
    encoding, so a payload with one character outside it raised ``UnicodeEncodeError`` after the
    run had succeeded. A terminal is left alone: it renders in its own encoding. This is the
    console-encoding seam of decision 87; no other module reconfigures a stream.
    """
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None or stream.isatty():
        return
    if (getattr(stream, "encoding", None) or "").lower().replace("-", "") != "utf8":
        reconfigure(encoding="utf-8")


def describe() -> SeamLine:
    """The console encoding line of ``doctor``'s platform section."""
    if sys.platform == "win32":
        return SeamLine("console encoding", "UTF-8 on a redirected stdout", None)
    return SeamLine("console encoding", "native", None)


def _console(stderr: bool = False) -> Console:
    # Built per call so the console resolves the current sys.stdout, which is what makes the
    # output capturable by pytest and by Typer's CliRunner.
    _utf8_when_redirected()
    return Console(stderr=stderr, soft_wrap=True)


def line(text: str) -> None:
    """One line of human-facing output, verbatim.

    Markup and Rich's syntax highlighter are both off: this is where a subsystem's own rendering
    arrives (``steps.describe``, ``promoter.describe``), and a highlighter that colours the
    ``=`` in ``provider=fake`` puts escape codes through the middle of a sentence the
    architecture pins literally.
    """
    _console().print(text, markup=False, highlight=False)


def rows(title: str, items: Sequence[str]) -> None:
    """A heading and its rows. An empty list still prints the heading, never a blank screen."""
    console = _console()
    console.print(title, style="bold", markup=False, highlight=False)
    for item in items:
        console.print(f"  {item}", markup=False, highlight=False)


def checks(results: Sequence[Check] | Sequence[_Renderable]) -> None:
    """The doctor report: one line per check, its status and its reason."""
    console = _console()
    for check in results:
        mark = _STATUS_MARK.get(check.status, check.status)
        console.print(f"{mark}  {check.name}  [dim]{escape(check.detail)}[/dim]", highlight=False)


def rungs(ladder: Sequence[Rung]) -> None:
    """The onboarding ladder: one line per rung, its state, and what advances it.

    ``not yet`` is deliberately not coloured like a failure. It is the state of an operator who
    has not needed that rung, and the report says so in the same voice it says ``live``.
    """
    console = _console()
    console.print(f"The {len(ladder)} rungs:", style="bold", markup=False, highlight=False)
    for rung in ladder:
        mark = _RUNG_MARK.get(rung.state, rung.state)
        console.print(
            f"  rung {rung.number}  {rung.name:<14}{mark}  [dim]{escape(rung.detail)}[/dim]",
            highlight=False,
        )


def eval_run(run: EvalRun) -> None:
    """One scored run: the aggregate first, then every case as it was graded."""
    console = _console()
    console.print(
        f"[bold]{escape(run.suite)}[/bold] ({escape(run.label)})  run {escape(run.eval_run_id)}  "
        f"score {run.score}  {run.passes}/{run.case_count} passed  "
        f"[dim]{run.scored_now} scored this pass[/dim]",
        highlight=False,
    )
    for case in run.cases:
        mark = "[green]pass[/green]" if case.passed else "[red]fail[/red]"
        replayed = " [dim](replayed)[/dim]" if case.replayed else ""
        console.print(
            f"  {mark}  {escape(case.case_id)}  {case.score}{replayed}  "
            f"[dim]{escape(case.detail)}[/dim]",
            highlight=False,
        )
    if run.pending:
        console.print(
            f"[yellow]{len(run.pending)} pending[/yellow]  "
            f"[dim]awaiting a verdict, skipped: {escape(', '.join(run.pending))}[/dim]",
            highlight=False,
        )
    if run.unscored:
        summary = unscored_line(
            run.case_count, run.case_count + len(run.unscored), [u.reason for u in run.unscored]
        )
        console.print(
            f"[yellow]{escape(summary)}[/yellow]  "
            f"[dim]score is the mean over scored cases only[/dim]",
            highlight=False,
        )
        for item in run.unscored:
            console.print(
                f"  [yellow]unscored[/yellow]  {escape(item.case_id)}  {escape(item.error_class)} "
                f"after {item.attempts} attempt(s)",
                highlight=False,
            )


def unscored_line(scored: int, total: int, reasons: Sequence[str]) -> str:
    """``N of M scored, K unscored: transport`` (ticket 231, FR-160): a run that lost cases to
    dropped connections says so beside its score, never a mean over fewer cases in silence."""
    return f"{scored} of {total} scored, {len(reasons)} unscored: {', '.join(sorted(set(reasons)))}"


def reply(result: LoopResult, run_id: str = "") -> None:
    """What one ad-hoc run produced: the answer, then what it cost.

    ``run_id`` is printed because a run now leaves something behind - an episode keyed by it. An
    id the operator never sees is an episode they cannot read back, which would leave
    ``harnessed memory episodes`` a command with no way to learn its own argument.
    """
    console = _console()
    console.print(result.reply, markup=False, highlight=False)
    trailer = f"run {escape(run_id)}  " if run_id else ""
    # A truncated answer still ends on guardrail 1, so the trailer names the cut from the
    # structural field; a clean finish prints exactly what it always did (decision 27).
    cut = f", answer truncated ({escape(str(result.stop_reason))})" if result.truncated else ""
    console.print(
        f"[dim]{trailer}{result.iterations} iteration(s), {len(result.tool_calls)} tool call(s), "
        f"ended on {escape(result.guardrail)}{cut}[/dim]",
        highlight=False,
    )


def uncertain_stop(payload: Mapping[str, object]) -> None:
    """An uncertain stop's reply (FR-103): the reason, the floor that failed, and the top routes
    with their percent strings, exactly as ``cli/routed.py::uncertain_payload`` built them.

    Never an answer - there is no reply to print, and this function never prints one.
    """
    console = _console()
    console.print(f"stopped: {payload['stop_reason']}", markup=False, highlight=False)
    route = payload["route"]
    if route is not None:
        floor_percent = payload["floor_percent"]
        floor_line = f" (floor {floor_percent})" if floor_percent is not None else ""
        console.print(f"  chose {route!r}{floor_line}", markup=False, highlight=False)
    routes = payload["routes"]
    assert isinstance(routes, Sequence)
    if routes:
        console.print("  top routes:", markup=False, highlight=False)
        for entry in routes:
            assert isinstance(entry, Mapping)
            console.print(
                f"    {entry['route']}  {entry['confidence_percent']}",
                markup=False,
                highlight=False,
            )


def json_object(payload: object) -> None:
    """A single JSON object on stdout and nothing else, so the output is machine-parseable."""
    _utf8_when_redirected()
    print(canonical(payload))


def failure(message: str) -> None:
    """A refusal an operator has to read, on stderr, next to the non-zero exit that follows."""
    print(message, file=sys.stderr)


def warning(message: str) -> None:
    """A caution an operator should read before what follows happens, on stderr.

    Not a failure: the command goes on. It is kept off stdout so a ``--json`` consumer never
    sees it, and prefixed so a human can tell it from the error line that ends a run.
    """
    print(f"warning: {message}", file=sys.stderr)


def error(exc: BaseException) -> None:
    """A HarnessError rendered to stderr. Never a traceback dressed up as a friendly message.

    An unconfigured harness (FR-27) is the one failure an operator is *expected* to hit, on their
    first run, so it is printed as guidance: the sentence, then the two things they can type. The
    two lines are built from the error's own ``remedy`` and ``env_var`` fields rather than from
    its message, so re-wording the message can never silently drop the instructions.
    """
    print(f"error: {exc}", file=sys.stderr)
    if isinstance(exc, ProviderNotConfiguredError):
        print(f"  {exc.remedy}", file=sys.stderr)
        print(f"  {exc.env_var}=fake   replay the committed fixtures, offline", file=sys.stderr)
