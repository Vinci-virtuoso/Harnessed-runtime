"""The only module in the repository that reads ``os.environ``.

Configuration is one frozen dataclass built once by :func:`load`. Every variable it reads is
documented in ``.env.example``; nothing here has a default that needs a key.

There is no default *provider* at all (FR-27). Resolution is ``--provider`` flag, then
``HARNESSED_PROVIDER``, then the ``.env`` file, then nothing - and "nothing" stays ``None`` all the
way to :meth:`Settings.require_provider`, which raises. A fallback to ``fake`` here would hand an
operator who has configured nothing a fixture replay that looks like a working agent.

Reading ``.env`` is this module's job for the same reason reading the environment is: the third
arm of that order is a configuration source, and there is only one reader of those.

That order applies to the **key vars too** (ticket 039). Until then the file arm read exactly one
variable, ``HARNESSED_PROVIDER``, so a key ``harnessed init`` had just written to ``.env`` was
ignored by the very next command and had to be exported by hand into every new shell. The reason
recorded for the exclusion was that loading keys would put them into :func:`secret_values`,
redaction and every live adapter - but :func:`secret_values` feeding redaction is what *scrubs* a
credential, so that was a benefit described as a cost. The one thing that arrangement must never
become is a key the adapters can see and the redactor cannot, which is why :func:`secret_values`
reads the same file for itself rather than trusting a caller to have loaded it.

And to **every documented variable** (ticket 040). Three tickets in a row taught this loader one
more name from the file - 032 the provider, 039 the keys, and a fourth round was already queued
for the model, the base url and the iteration ceiling. There is no per-variable list here now:
:func:`load` resolves every field it builds through :func:`_resolved`, so the file arm cannot be
forgotten for the next variable the way it was forgotten for ``HARNESSED_MODEL``. A name this
module does not document is still not read - the file is a configuration source, not a second
environment.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from harnessed.errors import ConfigError, MissingCredentialError, ProviderNotConfiguredError

__all__ = [
    "DEFAULT_ROUTER_MODEL",
    "ENV_FILENAME",
    "FAKE_LATENCY_VAR",
    "MODEL_VAR",
    "PROVIDER_KEY_VARS",
    "PROVIDER_VAR",
    "ROUTER_MODEL_VAR",
    "SECRET_VARS",
    "TOKEN_BUDGET_FLAG",
    "Settings",
    "child_env",
    "dotenv_home",
    "xdg_config_home",
    "fake_latency_ms",
    "load",
    "merge_env_text",
    "parse_env_text",
    "secret_values",
]

DEFAULT_HOME: Final = ".harnessed"
DEFAULT_TOKEN_BUDGET: Final = 4000
DEFAULT_MAX_ITERATIONS: Final = 12

#: How long an agent's permission request waits for the operator before it is denied at its
#: ceiling (ticket 234, FR-130). Ten minutes: long enough to walk away and come back.
DEFAULT_PERMISSION_CEILING_S: Final = 600

#: How many unconsolidated episodes make a batch (ticket 247, FR-148, decision 81).
DEFAULT_CONSOLIDATE_AFTER: Final = 20

#: A task's budget when none is given (ticket 239, FR-140, decision 76): twenty loop turns and agent
#: hops, ten minutes, fifty cents. The dollars are a string because a figure on the wire is one.
DEFAULT_TASK_BUDGET_ITERATIONS: Final = 20
DEFAULT_TASK_BUDGET_SECONDS: Final = 600
DEFAULT_TASK_BUDGET_DOLLARS: Final = "0.50"

# The command-line spelling of the per-run retrieval budget override (FR-11). The flag is owned
# here, next to the variable and the default it competes with, so the three cannot drift apart
# and the command that binds it has one name to import rather than a literal to retype.
TOKEN_BUDGET_FLAG: Final = "--token-budget"

# The command-line spelling of the provider override, the variable it beats, and the file that
# variable beats. All three named here, next to each other, because they are one resolution order
# and a spelling that drifts is an arm that silently stops working.
PROVIDER_FLAG: Final = "--provider"
PROVIDER_VAR: Final = "HARNESSED_PROVIDER"
ENV_FILENAME: Final = ".env"

# The variable that names the model id. Spelled here rather than at the two places that report
# it missing, because a message telling an operator to set a variable the loader does not read
# is the failure ticket 040 exists to remove.
MODEL_VAR: Final = "HARNESSED_MODEL"

# The variable that pins the router's Decisions model (decision 57, FR-95). Unlike MODEL_VAR this
# one has a shipped default: the router always calls some model, so "unset" means the pinned id,
# never "no provider configured".
ROUTER_MODEL_VAR: Final = "HARNESSED_ROUTER_MODEL"

# The provider that opens no socket; it routes through ``FakeDecider`` with no routing key.
OFFLINE_PROVIDER: Final = "fake"
DEFAULT_ROUTER_MODEL: Final = "typesafe/jev-1.13-20260917"

# Which environment variable holds the key for each provider. ``None`` means the provider needs
# no credential at all, which is what keeps the default selection offline.
PROVIDER_KEY_VARS: Final[dict[str, str | None]] = {
    "fake": None,
    "ollama": None,
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

# Every variable whose value must never reach a store, an event payload or a log line.
SECRET_VARS: Final[frozenset[str]] = frozenset(
    name for name in PROVIDER_KEY_VARS.values() if name is not None
)


@dataclass(frozen=True, slots=True)
class Settings:
    """The whole configuration surface, resolved once.

    ``_keys`` is excluded from ``repr`` so a settings object can be printed, journalled or put in
    a traceback without leaking a credential.
    """

    home: Path
    provider: str | None
    model: str | None
    small_model: str | None
    base_url: str | None
    embed_base_url: str | None
    embed_model: str | None
    retrieval_token_budget: int
    max_iterations: int
    router_model: str = DEFAULT_ROUTER_MODEL
    #: ``HARNESSED_PERMISSION_CEILING_S``: seconds an unanswered permission request waits (FR-130).
    permission_ceiling_s: int = DEFAULT_PERMISSION_CEILING_S
    #: ``HARNESSED_CONSOLIDATE_AFTER``: unconsolidated episodes that trigger one batch (FR-148).
    consolidate_after: int = DEFAULT_CONSOLIDATE_AFTER
    #: A task's budget when ``task add`` is given none (FR-140, decision 76). Fixed figures, not
    #: environment reads: the operator states a budget per task or accepts these.
    task_budget_iterations: int = DEFAULT_TASK_BUDGET_ITERATIONS
    task_budget_seconds: int = DEFAULT_TASK_BUDGET_SECONDS
    task_budget_dollars: str = DEFAULT_TASK_BUDGET_DOLLARS
    #: Ticket 179 deleted the fallback env var and everything that read it as a choice; this stays
    #: fixed ``True`` rather than disappearing outright because ``tests/test_e2e_flywheel.py``
    #: (``scope_out`` for that ticket) reads it as a sanity check that routing is on before
    #: trusting a "0 Decider calls" assertion. Nothing in ``src/`` else reads it.
    router: bool = True
    _keys: dict[str, str] = field(repr=False, default_factory=dict)

    def key_var_for(self, provider: str) -> str | None:
        """The environment variable holding ``provider``'s key, or ``None`` if it needs none."""
        if provider not in PROVIDER_KEY_VARS:
            raise ConfigError(
                f"unknown provider {provider!r}. Known providers: "
                f"{', '.join(sorted(PROVIDER_KEY_VARS))}."
            )
        return PROVIDER_KEY_VARS[provider]

    def api_key_for(self, provider: str) -> str | None:
        """The configured key for ``provider``, or ``None`` when absent or not needed."""
        var = self.key_var_for(provider)
        if var is None:
            return None
        return self._keys.get(var)

    def routing_key_notice(self) -> str | None:
        """What an operator without the routing key reads (ticket 232), or ``None`` once it is set.

        Routing runs the router's decider and metered agents through OpenRouter, so a machine with
        only a chat key still chats and cannot route. The words live here, beside the variable's
        name, so ``doctor`` and ``init`` cannot say it two ways.
        """
        var = PROVIDER_KEY_VARS["openrouter"]
        assert var is not None
        if self._keys.get(var):
            return None
        return f"routing also needs {var}"

    def routing_on(self) -> bool:
        """Whether a message is routed: the offline provider always is, a live one needs the
        OpenRouter key the Decisions API takes (ticket 316, decision 105)."""
        if self.provider == OFFLINE_PROVIDER:
            return True
        var = PROVIDER_KEY_VARS["openrouter"]
        assert var is not None
        return bool(self._keys.get(var))

    def routing_off_reason(self) -> str | None:
        """Why routing is off, in the one sentence ``doctor``, ``init`` and the router read
        serve, or ``None`` when it is on (ticket 316, FR-175)."""
        if self.routing_on():
            return None
        return f"routing is off: set {PROVIDER_KEY_VARS['openrouter']} to turn it on"

    def require_provider(self) -> str:
        """The resolved provider name, or :class:`ProviderNotConfiguredError` (FR-27).

        Every caller that is about to *do* something with a provider goes through here rather
        than reading the attribute, so the one place that turns "nothing configured" into a
        failure is this one and there is nowhere for a fallback to be added quietly.
        """
        if self.provider is None:
            raise ProviderNotConfiguredError()
        return self.provider

    def require_api_key(self, provider: str) -> str:
        """The key for ``provider``, or :class:`MissingCredentialError` naming the variable."""
        var = self.key_var_for(provider)
        if var is None:
            raise ConfigError(f"provider {provider!r} takes no API key")
        key = self._keys.get(var)
        if not key:
            raise MissingCredentialError(provider, var)
        return key


# One ``NAME=`` assignment, with whatever indentation the operator gave it. A line that starts
# with ``#`` is a comment and is never an assignment, however much it looks like one.
#
# The name may be preceded by ``export `` and whitespace may sit on either side of the ``=``; both
# are what an operator's hand-written file actually contains (finding 8). Quoting is a value
# concern and is handled by :func:`_unquote`, not here.
_ASSIGNMENT: Final = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def parse_env_text(text: str) -> dict[str, str]:
    """The variables a dotenv file actually sets, in file order.

    Only uncommented assignments count. Values are stripped the same way :func:`_env` strips
    them, then unquoted, so what this reports is what :func:`load` would see if the file were
    exported.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match is None or line.lstrip().startswith("#"):
            continue
        _, _, value = line.partition("=")
        values[match.group(1)] = _unquote(value)
    return values


def _unquote(value: str) -> str:
    """One assignment's value with whitespace and one pair of matching quotes removed.

    ``KEY="quoted"`` is an assignment to ``quoted``, not to a value that still wears its quote
    characters. Only a matching pair is stripped - ``KEY="a"b`` stays as it is, because a half-
    quoted value is a typo the operator should see, not one the loader guesses at.
    """
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
        return stripped[1:-1]
    return stripped


def _ending(line: str) -> str:
    """The line terminator ``line`` carries, or ``""`` for a final line without one."""
    if line.endswith("\r\n"):
        return "\r\n"
    return "\n" if line.endswith("\n") else ""


def merge_env_text(text: str, updates: Mapping[str, str]) -> str:
    """``text`` with each of ``updates`` set, and every other byte left exactly where it was.

    This is what makes ``harnessed init`` safe to run against a file an operator already owns
    (FR-26). A variable the file already assigns is rewritten **in place**, keeping its position
    and its line terminator; one it does not assign is appended. Comments, blank lines, ordering
    and a commented-out placeholder are all preserved - a ``# GROQ_API_KEY=`` line documents the
    variable, it does not set it, so uncommenting it would edit a line the caller does not own.

    A dotenv library is deliberately not used: every one of them normalises quoting, ordering or
    spacing on the way out, and normalising an operator's file is the failure this exists to
    prevent.
    """
    remaining = dict(updates)
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _ASSIGNMENT.match(line)
        name = match.group(1) if match else None
        if name is not None and name in remaining and not line.lstrip().startswith("#"):
            out.append(f"{name}={remaining.pop(name)}{_ending(line)}")
        else:
            out.append(line)
    if not remaining:
        return "".join(out)

    newline = next((_ending(line) for line in reversed(out) if _ending(line)), "\n")
    if out and not _ending(out[-1]):
        out[-1] += newline
    out.extend(f"{name}={value}{newline}" for name, value in remaining.items())
    return "".join(out)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _resolved_int(name: str, default: int, dotenv: Mapping[str, str]) -> int:
    """The same order as :func:`_resolved`, parsed. A typo fails wherever it was written.

    The file arm goes through this parse rather than around it: a ``.env`` holding
    ``HARNESSED_MAX_ITERATIONS=lots`` that silently fell back to the default would be a setting
    an operator can see in their own file and the harness quietly ignores.
    """
    raw = _resolved(name, dotenv)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _dotenv_values() -> dict[str, str]:
    """Every assignment :data:`ENV_FILENAME` in the working directory makes, values stripped.

    The file is read on **every call**, never cached in a module global. A cache would make the
    result depend on when the module was first imported, which is exactly the bug a test that
    writes a ``.env`` into a temporary directory would hit, and :func:`secret_values` is called
    long after the process started.

    An unreadable or undecodable file is treated as absent rather than as an error. This is one
    arm of a search: an arm that raised would make a stray file fatal to a command the operator
    configured perfectly well by other means, and it would let a bad config file turn a redaction
    into an exception, which is the worse failure of the two.
    """
    try:
        text = Path(ENV_FILENAME).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return parse_env_text(text)


def dotenv_home() -> Path:
    """The home a process started with no shell variables would resolve: ``.env``, then the default.

    The scheduled task's process is that process (decision 79), so ``schedule install`` compares
    this with :attr:`Settings.home` and refuses when they differ.
    """
    return Path(_dotenv_values().get("HARNESSED_HOME", "").strip() or DEFAULT_HOME)


def xdg_config_home() -> Path:
    """``$XDG_CONFIG_HOME``, else ``~/.config``: where the systemd user timer's units live."""
    configured = _env("XDG_CONFIG_HOME")
    return Path(configured) if configured else Path.home() / ".config"


def _resolved(name: str, dotenv: Mapping[str, str]) -> str | None:
    """``name`` from the environment, then from an already-parsed ``.env``, then nothing.

    The middle and last arms of the one order this module owns, in one function, so the provider
    and the key vars cannot drift into two different precedences.
    """
    value = _env(name)
    if value is not None:
        return value
    from_file = dotenv.get(name, "").strip()
    return from_file or None


#: The one knob this module reads that is **not** configuration (ticket 119, FR-53). Named here
#: because MUST rule 3 puts every ``os.environ`` read in this file, and nowhere else: it is not in
#: ``.env.example``, not in :class:`Settings`, not in ``doctor``'s report and not in ``init``'s
#: owned keys, because none of those is true of a test affordance.
FAKE_LATENCY_VAR: Final = "HARNESSED_FAKE_LATENCY_MS"


def fake_latency_ms() -> int:
    """How long :class:`~harnessed.providers.fake.FakeProvider` waits before it answers, in ms.

    **A test affordance, not a run-time option.** The fake provider reads a committed fixture off
    disk and returns, so an eval run publishes its whole event stream inside about 24 ms (ticket
    112 measured it). Nothing can attach to a session that is already finished by the time it is
    discoverable, so the end-to-end walk that proves live streaming needs the publisher to take
    its time. Only wall time changes: same corpus, same key, same bytes.

    Deliberately outside :class:`Settings` and outside the ``.env`` arm of :func:`_resolved`. A
    delay is worth nothing to an operator and would be a bill and a lie on a real provider, so it
    may not travel in a file that gets copied between machines. The environment, and only for the
    process that set it.

    An unset or blank variable is the default, 0. Anything else that is not a whole non-negative
    number of milliseconds is a :class:`~harnessed.errors.ConfigError` at the point of the read,
    not a silent 0: a typo that read as 0 would leave a walk hunting a window nobody widened.
    """
    raw = _env(FAKE_LATENCY_VAR)
    if raw is None:
        return 0
    try:
        milliseconds = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{FAKE_LATENCY_VAR} must be a whole number of milliseconds, got {raw!r}"
        ) from exc
    if milliseconds < 0:
        raise ConfigError(
            f"{FAKE_LATENCY_VAR} must be a whole number of milliseconds and cannot be "
            f"negative, got {raw!r}"
        )
    return milliseconds


def child_env() -> dict[str, str]:
    """This process's environment, minus every :data:`SECRET_VARS` value (MUST rule 33).

    Two callers: ``acp/cli_runner.py`` for a ``kind = "cli"`` agent and ``acp/delegate.py`` for
    every ``acp`` one. Each is a real child process, and decision 62 promises it never sees a
    credential. Reading ``os.environ`` to build that child's
    environment stays here, per All AI agents MUST rule 3, rather than making ``cli_runner.py`` a
    second reader of it.
    """
    env = dict(os.environ)
    for name in SECRET_VARS:
        env.pop(name, None)
    return env


def secret_values() -> tuple[str, ...]:
    """Every :data:`SECRET_VARS` value this process could use, longest first.

    This is what :mod:`harnessed.memory.redact` matches verbatim (FR-13), and it exists so
    redaction can know the process's own credentials without becoming a second reader of the
    environment (All AI agents MUST rule 3). Longest first, then alphabetical, so one value that
    contains another is replaced whole and the result is deterministic.

    Both the exported value and the one in ``.env`` are returned when they differ. Precedence
    decides which key is *used*; redaction has no reason to prefer one, and scrubbing the value
    that lost costs nothing while covering the operator who has just rotated a key.

    It reads live rather than through :func:`load`, and takes the raw file parse rather than a
    resolved :class:`Settings`, because a malformed ``HARNESSED_PROVIDER`` must not be able to
    turn a redaction into an error: failing to scrub is the worse failure.
    """
    dotenv = _dotenv_values()
    found = {
        value
        for var in SECRET_VARS
        for value in (_env(var), dotenv.get(var, "").strip() or None)
        if value is not None
    }
    return tuple(sorted(found, key=lambda value: (-len(value), value)))


def _resolve_provider(flag: str | None, dotenv: Mapping[str, str]) -> str | None:
    """The FR-27 order, top to bottom, with no fallback arm at the end.

    ``None`` out of here is not a defect and not a default - it is the honest report that nobody
    has chosen. :meth:`Settings.require_provider` is what turns it into a failure, at the moment
    something actually needs a provider.
    """
    return flag or _resolved(PROVIDER_VAR, dotenv)


def load(*, provider: str | None = None, token_budget: int | None = None) -> Settings:
    """Build :class:`Settings` from the environment plus the documented defaults.

    ``provider`` is the :data:`PROVIDER_FLAG` override and wins outright; see
    :func:`_resolve_provider` for the rest of the order. ``token_budget`` is the per-run override
    :data:`TOKEN_BUDGET_FLAG` binds to. Precedence is the usual one and is resolved here rather
    than at a call site: the flag beats ``HARNESSED_TOKEN_BUDGET``, which beats
    :data:`DEFAULT_TOKEN_BUDGET`.
    """
    dotenv = _dotenv_values()
    resolved = _resolve_provider(provider, dotenv)
    keys = {
        var: value for var in sorted(SECRET_VARS) if (value := _resolved(var, dotenv)) is not None
    }
    settings = Settings(
        home=Path(_resolved("HARNESSED_HOME", dotenv) or DEFAULT_HOME),
        provider=resolved,
        model=_resolved(MODEL_VAR, dotenv),
        small_model=_resolved("HARNESSED_SMALL_MODEL", dotenv),
        base_url=_resolved("HARNESSED_BASE_URL", dotenv),
        embed_base_url=_resolved("HARNESSED_EMBED_BASE_URL", dotenv),
        embed_model=_resolved("HARNESSED_EMBED_MODEL", dotenv),
        retrieval_token_budget=(
            token_budget
            if token_budget is not None
            else _resolved_int("HARNESSED_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET, dotenv)
        ),
        max_iterations=_resolved_int("HARNESSED_MAX_ITERATIONS", DEFAULT_MAX_ITERATIONS, dotenv),
        router_model=_resolved(ROUTER_MODEL_VAR, dotenv) or DEFAULT_ROUTER_MODEL,
        permission_ceiling_s=_resolved_int(
            "HARNESSED_PERMISSION_CEILING_S", DEFAULT_PERMISSION_CEILING_S, dotenv
        ),
        consolidate_after=_resolved_int(
            "HARNESSED_CONSOLIDATE_AFTER", DEFAULT_CONSOLIDATE_AFTER, dotenv
        ),
        _keys=keys,
    )
    # Fails fast on a typo in the flag, the variable or the file rather than at the first model
    # call. Nothing named at all is not a typo, so it is not checked here - it is FR-27's error,
    # raised by require_provider when something needs a provider.
    if resolved is not None:
        settings.key_var_for(resolved)
    return settings
