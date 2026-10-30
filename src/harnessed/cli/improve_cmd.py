"""``harnessed improve run`` and ``harnessed improve promote <id>`` - the flywheel's two verbs.

Both call ``improve/steps.py::run_cycle``, which is resumable over the ``improvement_steps``
ledger: a dry run records baseline, reflection, candidate and holdout, and stops before the
decision, so ``promote`` re-issues the same cycle and every earlier step replays off the ledger
rather than being re-measured. That is why promotion is not a second, different code path - it
is the same cycle, allowed to reach its last step.

``reflector`` is a named seam on purpose. The reflection model is the one collaborator a test of
the falsifiability of the proof has to replace (ticket 029), and replacing it must not mean
reaching inside a command body.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Annotated, Final

import typer

from harnessed import config
from harnessed.cli import render
from harnessed.cli.run_cmd import Session, open_session
from harnessed.errors import ImproveError, SurfaceNotFound
from harnessed.evals.scoring import MINIMUM_LIVE_SAMPLES
from harnessed.evals.suite import load_named_suite
from harnessed.improve import steps
from harnessed.improve.reflector import Model
from harnessed.loop.prompt import build_system_prompt
from harnessed.memory import db, procedural

__all__ = ["improve_app", "promote", "promotions", "recorded_candidate", "reflector", "run", "show"]

#: What ``--samples`` means, written once and given to both verbs. Both need it: ``promote``
#: re-issues the same cycle, and a step the ledger is missing is measured there rather than in
#: ``run`` - so a promote that could not be told how many draws to take measured one by
#: construction (ticket 052).
SAMPLES_HELP = (
    "Draw every case this many times in each measurement. Against a live model the promoter "
    "refuses to compare fewer than its minimum; one is the default and is exactly the truth "
    "against the replaying provider."
)

improve_app = typer.Typer(
    name="improve",
    help="Measure, reflect, measure again, and decide by arithmetic.",
    no_args_is_help=True,
)


@improve_app.callback()
def _group() -> None:
    """Keep ``improve`` a command group (see ``eval_cmd._group`` for why)."""


def reflector(session: Session) -> Model:
    """The model the reflection step consults. The configured provider, by default."""
    return session.provider


def recorded_candidate(conn: sqlite3.Connection, suite: str) -> str:
    """The candidate the latest cycle over ``suite`` actually wrote.

    Argument validation, not ledger logic: the arithmetic, the decision and the write all happen
    inside ``run_cycle``. This only refuses to start a cycle whose promotion would decide a
    different candidate than the operator named, because promoting the wrong id is not a
    mistake a run should discover halfway through.
    """
    improvement_run_id = steps.latest(conn, suite)
    if improvement_run_id is None:
        raise ImproveError(
            f"no improvement run over suite {suite!r} has been recorded, so there is no "
            f"candidate to promote. Run `harnessed improve run --dry-run` first."
        )
    reflection = steps.get(conn, improvement_run_id, steps.REFLECT, suite)
    if reflection is None:
        raise ImproveError(
            f"improvement run {improvement_run_id} never reached its reflection step. "
            f"Re-issue `harnessed improve run --dry-run` to resume it."
        )
    cand_id = str(reflection.payload["cand_id"])
    if not cand_id:
        raise ImproveError(
            f"improvement run {improvement_run_id} reflected on suite {suite!r} and wrote no "
            f"candidate: the baseline failed nothing the reflection could act on."
        )
    return cand_id


@improve_app.command("run")
def run(
    suite: Annotated[str, typer.Option("--suite", help="The suite the cycle measures.")] = "smoke",
    holdout: Annotated[
        str, typer.Option("--holdout", help="The unseen suite the reflector never reads.")
    ] = "holdout",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Stop before the promotion; record every step.")
    ] = False,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Start a new cycle instead of resuming the latest.")
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option(
            config.PROVIDER_FLAG,
            help="Use this provider for this run only. Beats HARNESSED_PROVIDER and .env.",
        ),
    ] = None,
    samples: Annotated[int, typer.Option("--samples", help=SAMPLES_HELP)] = 1,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Run one improvement cycle, resuming the latest one unless --fresh is given."""
    _cycle(
        suite,
        holdout,
        dry_run=dry_run,
        fresh=fresh,
        as_json=as_json,
        samples=samples,
        provider=provider,
    )


@improve_app.command("promote")
def promote(
    cand_id: Annotated[str, typer.Argument(help="The candidate id the cycle recorded.")],
    suite: Annotated[str, typer.Option("--suite", help="The suite the cycle measured.")] = "smoke",
    holdout: Annotated[
        str, typer.Option("--holdout", help="The unseen suite the reflector never reads.")
    ] = "holdout",
    provider: Annotated[
        str | None,
        typer.Option(
            config.PROVIDER_FLAG,
            help="Use this provider for this run only. Beats HARNESSED_PROVIDER and .env.",
        ),
    ] = None,
    samples: Annotated[int, typer.Option("--samples", help=SAMPLES_HELP)] = 1,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Decide the recorded candidate by arithmetic, and activate it only if it wins."""
    _cycle(
        suite,
        holdout,
        dry_run=False,
        fresh=False,
        as_json=as_json,
        samples=samples,
        expect=cand_id,
        provider=provider,
    )


#: How many promotion decisions ``improve promotions`` shows without ``--limit``: the route's own.
PROMOTIONS_LIMIT: Final = 20


@improve_app.command("promotions")
def promotions(
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many decisions to show.")] = (
        PROMOTIONS_LIMIT
    ),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print the recorded promotion decisions, in decided order (ticket 232).

    The twin of ``GET /v1/promotions``: ``procedural.promotions`` rows under ``promotions``, as
    stored and never re-derived, so the ``--json`` line and the body are the same bytes.
    """
    found = procedural.promotions(db.connect(), limit)
    if as_json:
        render.json_object({"promotions": [asdict(row) for row in found]})
        return
    render.rows(
        f"{len(found)} promotion decision(s)",
        tuple(f"{row.cand_id}  promoted={row.promoted}  {row.decided_at}" for row in found),
    )


@improve_app.command("show")
def show(
    improvement_run_id: Annotated[str, typer.Argument(help="The improvement run id.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print one recorded improvement cycle (ticket 232).

    The twin of ``GET /v1/improve/cycles/{improvement_run_id}``: ``steps.cycle_for`` through
    ``steps.json_payload``, the payload ``improve run --json`` prints for the cycle it just wrote.
    """
    conn = db.connect()
    cycle = steps.cycle_for(conn, improvement_run_id)
    if cycle is None:
        raise SurfaceNotFound(
            f"no improvement run {improvement_run_id!r}: the ledgers hold nothing under that id."
        )
    if as_json:
        render.json_object(steps.json_payload(cycle))
    else:
        render.line(
            f"improvement run {cycle.improvement_run_id}: suite {cycle.suite}, "
            f"candidate {cycle.cand_id or 'none'}, {len(cycle.steps)} step(s), "
            f"{'promoted' if cycle.promoted else 'not promoted'}"
        )


def _cycle(
    suite: str,
    holdout: str,
    *,
    dry_run: bool,
    fresh: bool,
    as_json: bool,
    samples: int = 1,
    expect: str | None = None,
    provider: str | None = None,
) -> None:
    """One cycle: load both suites, open the session, run, render. Both commands are this."""
    measured = load_named_suite(suite)
    unseen = load_named_suite(holdout)
    session = open_session(provider=provider)
    provider = session.settings.require_provider()
    if expect is not None:
        recorded = recorded_candidate(session.conn, measured.name)
        if recorded != expect:
            raise ImproveError(
                f"candidate {expect!r} is not the candidate the latest cycle over suite "
                f"{measured.name!r} wrote, which is {recorded!r}. A promotion decides the "
                f"candidate that was measured, never one named from outside the run."
            )
    cycle = steps.run_cycle(
        session.conn,
        suite=measured,
        holdout=unseen,
        solve=session.solve,
        reflector=reflector(session),
        model=session.model,
        provider=provider,
        build=build_system_prompt,
        dry_run=dry_run,
        fresh=fresh,
        samples=samples,
        minimum_samples=_minimum_for(session, provider),
        bus=session.bus,
    )
    if as_json:
        render.json_object(steps.json_payload(cycle))
    else:
        render.line(steps.describe(cycle, provider=provider))


def _minimum_for(session: Session, provider: str) -> int:
    """How many draws the promoter demands, decided by the one fact the CLI knows and the
    promoter cannot: whether the configured provider needs a key.

    The promoter has no way to reach a provider - that is its guarantee - so it cannot ask which
    one scored a run. The composition root can. A provider with no key variable replays or runs
    locally and is deterministic; one draw is exactly the truth, and the minimum is 1. A keyed
    provider is a live model, one draw is a coin, and the minimum is MINIMUM_LIVE_SAMPLES.
    Keyed on "needs a key", never on the literal name ``fake``.
    """
    return 1 if session.settings.key_var_for(provider) is None else MINIMUM_LIVE_SAMPLES
