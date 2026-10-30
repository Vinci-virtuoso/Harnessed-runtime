"""``harnessed init`` - rung 1 of the ladder: one key, and the CLI works.

FR-26. A fresh clone has every pillar and no on-ramp, and this is the on-ramp: pick a provider,
take its credential, prove the credential with exactly one cheap call, and merge two or three
variables into ``.env``.

The validate-then-write is not here: it is ``onboarding.configure_provider``, and this command is
its first caller (FR-87, MUST rule 16). Its three rules - merge, never rewrite; nothing written
until the credential is proven; what was typed is never printed or returned - are stated there.
This module owns the asking: which provider, and the credential, whose interactive prompt hides
its input.

``--provider`` makes the run non-interactive: the credential is then read from stdin, which is
what lets the whole flow be tested offline and scripted in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Final

import typer

from harnessed import config
from harnessed.cli import onboarding, render
from harnessed.cli.doctor_cmd import AGENTS_NEED_ROUTING, Rung
from harnessed.cli.onboarding import ladder
from harnessed.errors import ConfigError

__all__ = [
    "ENV_FILENAME",
    "EXAMPLE_FILENAME",
    "OWNED_KEYS",
    "TRY_IT_NOW",
    "init",
    "ladder",
]

ENV_FILENAME: Final = ".env"
EXAMPLE_FILENAME: Final = ".env.example"

#: The variables init sets outright. The chosen provider's credential variable joins them at
#: run time, from ``config.PROVIDER_KEY_VARS`` - which is why it is not listed here: naming it
#: twice is how the two tables drift apart. Everything else in ``.env`` is the operator's.
OWNED_KEYS: Final[tuple[str, ...]] = ("HARNESSED_PROVIDER", "HARNESSED_MODEL")

#: The command init names because that command will now actually run. Ticket 038 withheld it on
#: the branch where it would have failed - an on-ramp naming a command that fails is worse than
#: one naming none - and ticket 039 removed the branch by making the written ``.env`` enough.
TRY_IT_NOW: Final = 'Try it now: harnessed run "summarise what this harness does"'


def _choose_provider() -> str:
    """Ask which provider to configure, listing the ones the harness knows."""
    render.rows(
        "Which provider should the orchestrator use?",
        tuple(sorted(config.PROVIDER_KEY_VARS)),
    )
    chosen: str = typer.prompt("provider")
    return chosen.strip()


def _read_credential(key_var: str, interactive: bool) -> str:
    """The credential for ``key_var``, hidden when a human is typing it.

    A non-interactive run takes the first line of stdin instead of prompting, so
    ``--provider`` plus a piped key is a complete, promptless flow.
    """
    if interactive or sys.stdin.isatty():
        typed: str = typer.prompt(key_var, hide_input=True)
    else:
        typed = sys.stdin.readline()
    value = typed.strip()
    if not value:
        raise ConfigError(
            f"no value was supplied for {key_var}. Pipe it on stdin, or run "
            f"`harnessed init` without --provider and type it at the prompt."
        )
    return value


def init(
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Configure this provider and ask nothing."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Pin HARNESSED_MODEL to this model id."),
    ] = None,
    validate: Annotated[
        bool,
        typer.Option("--validate/--no-validate", help="Prove the credential with one call."),
    ] = True,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Configure a provider, write ``.env``, and say what now works."""
    settings = config.load()
    chosen = _choose_provider() if provider is None else provider
    # Raises ConfigError naming the known providers before anything is read or written.
    key_var = settings.key_var_for(chosen)

    credential = None if key_var is None else _read_credential(key_var, provider is None)
    payload = onboarding.configure_provider(
        chosen,
        credential,
        validate=validate,
        env_path=Path(ENV_FILENAME),
        example_path=Path(EXAMPLE_FILENAME),
        model=model,
    )

    if as_json:
        render.json_object(payload)
        return

    held = "" if key_var is None else f", credential in {key_var}"
    if payload["unchanged"]:
        render.line(f"{ENV_FILENAME} unchanged: provider {chosen} is already configured{held}")
    else:
        render.line(f"{ENV_FILENAME} written: provider {chosen}{held}")

    render.rungs(tuple(Rung(**rung) for rung in payload["rungs"]))
    settings = config.load(provider=chosen)
    notice = settings.routing_key_notice()
    if notice is not None:
        render.line(notice)
    off = settings.routing_off_reason()
    if off is not None:
        render.line(f"{off}; {AGENTS_NEED_ROUTING}")
    # No conditional, because there is no longer a state in which the command would fail: the
    # credential is either unnecessary or in the file this run just wrote, which the next command
    # reads. Ticket 038's export instruction lived here and is deleted rather than left dormant.
    render.line(TRY_IT_NOW)
    render.line("Optional, when you want work handed to a coding agent: harnessed agents add")
