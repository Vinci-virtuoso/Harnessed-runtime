"""``agents.toml``: the file that makes the coding agent swappable (FR-22).

Every agent-specific fact - which executable, which arguments, which working directory, which
permission mode, which ceilings, how to install it - lives here as data. The source never names a
CLI, which is the one thing ``acp.md`` says this package exists to prevent, and a scan over the
whole package enforces it.

``agents.toml`` itself is gitignored, because it names local executables and may carry paths that
have no business in a repository. :data:`EXAMPLE_PATH` is the committed half of the pair, and the
error raised for a missing file points at it.

Validation is eager and total. An unknown key is a typo, an unknown mode is a typo, and a missing
``command`` is an agent that cannot run - all three are :class:`ConfigError` at load rather than a
surprise at the first delegation. That is the same discipline ``evals/suite.py`` applies to a YAML
suite: a malformed declaration fails where it was written.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from harnessed.acp.permissions import MODES, Policy
from harnessed.errors import AgentNotFoundError, ConfigError

__all__ = [
    "BILLINGS",
    "BILLING_OPENROUTER",
    "BILLING_SUBSCRIPTION",
    "DEFAULT_KIND",
    "DEFAULT_MODE",
    "DEFAULT_PATH",
    "EXAMPLE_PATH",
    "FIELDS",
    "KINDS",
    "MODES",
    "TEMPLATES",
    "TEMPLATE_ALLOW",
    "TEMPLATE_DENY",
    "AgentSpec",
    "AgentTemplate",
    "agent_named",
    "load_agents",
    "refresh_entry",
    "render_entry",
    "render_example",
    "render_template_fields",
    "quota_markers_for",
    "template_named",
]

#: Where the loader looks when no path is injected. Relative, so it is the operator's repository
#: rather than a machine-wide location.
DEFAULT_PATH: Final = Path("agents.toml")
EXAMPLE_PATH: Final = Path("agents.toml.example")

DEFAULT_MODE: Final = "allowlist"
DEFAULT_TIMEOUT_S: Final = 300.0
DEFAULT_ASK_TIMEOUT_S: Final = 30.0

#: An entry with no declared ``kind`` speaks the Agent Client Protocol, exactly as every v4 entry
#: did (decision 62; FR-98's backward-compatibility criterion).
DEFAULT_KIND: Final = "acp"
KINDS: Final[frozenset[str]] = frozenset({"acp", "cli"})

#: How an agent's turns are metered. Absent means "not metered" - no cost figure is ever computed
#: for it (decision 63).
BILLINGS: Final[frozenset[str]] = frozenset({"oauth", "openrouter"})

#: The one ``billing`` value a thin-entry check needs to name on its own (ticket 159's
#: ``doctor`` warning on a missing ``base_url_var``). A named reference rather than a literal
#: comparison, exactly like ``deepseek``'s entry in ``PROVIDER_NAME_COLLISIONS``
#: (``tests/test_agents_onboarding.py``): "openrouter" is also a watched LLM-provider name
#: (``config.PROVIDER_KEY_VARS``), and ``test_provider_quirks.py``'s FR-9 scan bans any module
#: outside ``providers/`` from branching on that literal string.
BILLING_OPENROUTER: Final = "openrouter"

#: A subscription entry's billing: never proxied, its transcript's ``usage_update.cost`` kept as an
#: API-equivalent cost (decision 63, ticket 171). Named here so the one module that may spell the
#: word is the one that does (``test_agent_probes.py``'s OAuth scan).
BILLING_SUBSCRIPTION: Final = "oauth"

#: For a ``cli`` entry: whether the task replaces a ``{task}`` token in ``args``, or arrives on
#: stdin (decision 62).
DEFAULT_INPUT: Final = "argv"

#: The confidence range a ``floor`` override must sit in - never below Groq's own floor, never at
#: or above certainty (decision 59).
FLOOR_MIN: Final = 0.50
FLOOR_MAX: Final = 0.99

#: Every key an ``[agents.<name>]`` table may carry. Anything else is a typo and fails at load.
FIELDS: Final[frozenset[str]] = frozenset(
    {
        "command",
        "args",
        "cwd",
        "mode",
        "allow",
        "deny",
        "timeout_s",
        "ask_timeout_s",
        "install",
        "kind",
        "use_for",
        "not_for",
        "billing",
        "produces",
        "outputs",
        "floor",
        "base_url_var",
        "key_var",
        "input",
        "allow_write",
        "session_meta",
        "tool_names",
    }
)


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One declared agent. Everything :mod:`~harnessed.acp.delegate` needs to run it."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    mode: str = DEFAULT_MODE
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    timeout_s: float = DEFAULT_TIMEOUT_S
    ask_timeout_s: float = DEFAULT_ASK_TIMEOUT_S
    install: str = ""
    kind: str = DEFAULT_KIND
    use_for: str = ""
    not_for: str = ""
    billing: str | None = None
    produces: str | None = None
    outputs: str | None = None
    floor: float | None = None
    base_url_var: str | None = None
    key_var: str | None = None
    input: str = DEFAULT_INPUT
    allow_write: bool = False
    #: The ``_meta`` table ``session/new`` carries, so the agent launches under Harnessed's
    #: settings rather than its folder's (ticket 213). Empty means no ``_meta`` is sent.
    session_meta: Mapping[str, Any] = field(default_factory=dict)
    #: The vendor's own tool names for each ACP tool kind; derives the launch-time deny list.
    tool_names: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def policy(self, *, interactive: bool = False) -> Policy:
        """The matrix input this agent implies. ``interactive`` is the caller's, not the file's."""
        return Policy(
            mode=self.mode,
            allow=self.allow,
            deny=self.deny,
            interactive=interactive,
            ask_timeout_s=self.ask_timeout_s,
        )

    def install_hint(self) -> str:
        """What to tell a caller whose machine does not have this agent installed."""
        if self.install:
            return f"Install it with:\n    {self.install}"
        return (
            f"Install the executable named by 'command' in the [agents.{self.name}] table of "
            f"{DEFAULT_PATH.name}, or point that key at one already on PATH."
        )


#: The starting permission grant every template is written with. Uniform on purpose: a per-agent
#: policy would be a conditional, and the one rule this table exists to keep is that adding an
#: agent is a row rather than a branch. It is deliberately narrow - an agent may read, search and
#: think, and the two irreversible kinds are refused - and the operator widens it by editing
#: their own ``agents.toml``.
TEMPLATE_ALLOW: Final[tuple[str, ...]] = ("read", "search", "think", "edit")
TEMPLATE_DENY: Final[tuple[str, ...]] = ("delete", "execute")


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    """A known-good ``[agents.<name>]`` table, ready to be written into a registry.

    This is the one place in the repository a specific coding agent is named, and it is data:
    a handful of fields and no behaviour. A fourth agent is a fourth row here, never a branch
    anywhere.
    """

    name: str
    command: str
    install: str
    summary: str
    args: tuple[str, ...] = ()
    use_for: str = ""
    kind: str = DEFAULT_KIND
    billing: str | None = None
    base_url_var: str | None = None
    key_var: str | None = None
    input: str = DEFAULT_INPUT
    session_meta: Mapping[str, Any] = field(default_factory=dict)
    tool_names: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: ``{error data key: values}`` an agent's JSON-RPC error carries when it refuses for quota,
    #: recorded from a real refusal (decisions 63, 70). Template data only: not a registry field.
    quota_markers: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)

    def spec(self) -> AgentSpec:
        """The :class:`AgentSpec` this template declares, as the loader will read it back."""
        return AgentSpec(
            name=self.name,
            command=self.command,
            args=self.args,
            mode=DEFAULT_MODE,
            allow=TEMPLATE_ALLOW,
            deny=TEMPLATE_DENY,
            install=self.install,
            use_for=self.use_for,
            kind=self.kind,
            billing=self.billing,
            base_url_var=self.base_url_var,
            key_var=self.key_var,
            input=self.input,
            session_meta=self.session_meta,
            tool_names=self.tool_names,
        )


#: What to tell an operator whose machine has no Node.js. Three of the four templates run their
#: adapter straight off the registry, so the executable that has to exist is the runner.
_NODE_INSTALL: Final = (
    "install Node.js 18 or newer from https://nodejs.org - it ships the npx this template runs"
)

#: Templates whose ``base_url_var`` and ``key_var`` are ``[ASSUMPTION: unverified until
#: installed]`` (decision 70): nobody has run these three through the metering proxy yet, so the
#: variable names are a starting guess, not a confirmed fact. The FR-113 ticket's live test runs
#: each installed harness and records which ones actually meter.
_UNVERIFIED_NOTE: Final = (
    " base_url_var and key_var are [ASSUMPTION: unverified until installed] - the FR-113 ticket "
    "confirms them against a real run."
)

#: Every agent confirmed to speak the Agent Client Protocol, re-checked on 2026-09-13 when two
#: adapters had been renamed under ``@agentclientprotocol``, plus v5's two ``cli`` harnesses and a
#: generic ``cli`` starting point (decisions 62, 70; FR-100). Keyed by each row's own name, so the
#: table cannot disagree with itself, and in insertion order, which is the order
#: ``agents.toml.example`` and ``agents add`` report. No row carries an auto-approve flag: every
#: permission request is decided by the registry's policy.
TEMPLATES: Final[dict[str, AgentTemplate]] = {
    template.name: template
    for template in (
        AgentTemplate(
            name="claude-code",
            command="npx",
            args=("-y", "@agentclientprotocol/claude-agent-acp"),
            install=_NODE_INSTALL,
            summary="Anthropic's coding agent, through its published ACP adapter",
            use_for="general coding tasks in this repository",
            billing="oauth",
            session_meta={
                "claudeCode": {
                    "options": {"settingSources": [], "allowDangerouslySkipPermissions": False}
                }
            },
            tool_names={
                "execute": ("Bash",),
                "edit": ("Edit", "Write", "NotebookEdit"),
                "delete": ("Bash",),
                "read": ("Read",),
                "search": ("Glob", "Grep"),
                "fetch": ("WebFetch", "WebSearch"),
            },
        ),
        AgentTemplate(
            name="codex",
            command="npx",
            args=("-y", "@agentclientprotocol/codex-acp"),
            install=(
                "npm install -g @openai/codex, then run codex once to sign in - "
                "the adapter uses that sign-in"
            ),
            summary="OpenAI's coding CLI, through its published ACP adapter",
            use_for="coding tasks better suited to OpenAI's own agent",
            billing="oauth",
        ),
        AgentTemplate(
            name="pi",
            command="npx",
            args=("-y", "pi-acp"),
            install=_NODE_INSTALL,
            summary="the pi coding agent, through its published ACP adapter",
            use_for="coding tasks routed through OpenRouter." + _UNVERIFIED_NOTE,
            billing="openrouter",
            base_url_var="PI_BASE_URL",
            key_var="PI_API_KEY",
        ),
        AgentTemplate(
            name="grok",
            command="grok",
            args=("agent", "stdio"),
            install=(
                "curl -fsSL https://x.ai/cli/install.sh | bash, add ~/.grok/bin to PATH, then "
                "run grok once to sign in - it reads its own sign-in or XAI_API_KEY, and "
                "Harnessed passes it no key"
            ),
            summary="xAI's Grok Build, which speaks ACP natively over stdio",
            use_for="coding tasks better suited to xAI's own agent",
            billing="oauth",
            # Recorded 2026-10-03 (ticket 194): a -32603 whose data carries the HTTP status.
            quota_markers={"http_status": (402,)},
        ),
        AgentTemplate(
            name="hermes",
            command="hermes",
            install=(
                "install the Hermes coding harness from its own release, then run it once to "
                "sign in - Harnessed passes it no key of its own"
            ),
            summary="the Hermes coding harness, run once per delegation over stdin",
            use_for="a non-interactive coding task for the Hermes harness." + _UNVERIFIED_NOTE,
            kind="cli",
            input="stdin",
            billing="openrouter",
            base_url_var="HERMES_BASE_URL",
            key_var="HERMES_API_KEY",
        ),
        AgentTemplate(
            name="deepseek",
            command="deepseek-coder",
            install=(
                "install the DeepSeek coding harness from its own release, then run it once to "
                "sign in - Harnessed passes it no key of its own"
            ),
            summary="the DeepSeek coding harness, run once per delegation over stdin",
            use_for="a non-interactive coding task for the DeepSeek harness." + _UNVERIFIED_NOTE,
            kind="cli",
            input="stdin",
            billing="openrouter",
            base_url_var="DEEPSEEK_BASE_URL",
            key_var="DEEPSEEK_API_KEY",
        ),
        AgentTemplate(
            name="generic-cli",
            command="your-cli-agent",
            install=(
                "point 'command' (and 'args', if it needs any) in this table at any "
                "non-interactive CLI that reads a task and prints a reply - there is nothing to "
                "install for this template itself"
            ),
            summary="a starting point for any coding CLI with no ACP adapter of its own",
            use_for="a generic non-interactive coding task, once 'command' names a real CLI",
            kind="cli",
            input="stdin",
        ),
    )
}

#: The header the example file carries. Documentation for every key, written once.
_EXAMPLE_HEADER: Final = """\
# Harnessed ACP agent registry. Copy to agents.toml and adjust, or let
# `harnessed agents add <name>` write an entry for you.
#
# This file is generated from the template table in src/harnessed/acp/config.py.
# Edit the table, not this file: tests/test_agents_onboarding.py asserts the two
# are byte-identical, so they cannot drift.
#
# agents.toml is gitignored: it names local executables and may carry paths you
# do not want in the repository. Only this example file is committed.
#
# default       which agent an unnamed delegation gets. Uncomment one line, or
#               name an agent per delegation.
#
# [agents.<name>]
#   command      the executable that speaks the Agent Client Protocol over stdio
#   args         arguments appended to the command
#   cwd          working directory for the delegated task; defaults to the run cwd
#   mode         allow | deny | allowlist | ask. Defaults to allowlist.
#                  allow      every tool the agent asks for, except those in deny
#                  deny       nothing
#                  allowlist  only the tools named in allow
#                  ask        prompt a human; outside an interactive session this
#                             degrades to allowlist and logs a warning, so an
#                             unattended run neither stalls nor auto-approves
#   allow        tool kinds the agent may run without asking
#   deny         tool kinds the agent may never run; deny always wins over allow
#   timeout_s    hard wall-clock ceiling before the process tree is killed
#   ask_timeout_s  how long an interactive prompt waits before denying
#   install      the exact command that installs this agent, quoted back to the
#                caller when the executable is not on PATH
#   kind         acp | cli. Defaults to acp - the command speaks the Agent
#                Client Protocol. cli runs a synchronous, non-interactive
#                process once per delegation; see input and allow_write.
#   use_for      free text describing what to delegate to this agent
#   not_for      free text describing what never to delegate to this agent
#   billing      oauth | openrouter. Absent means not metered: no cost is ever
#                computed for this agent's turns.
#   produces     the output kind (text, ui, image, diagram, code, record) an
#                agent result should be shown as; absent lets the served media
#                type decide
#   outputs      a glob under the working directory naming which files a
#                completed delegation may copy out as output files
#   floor        the confidence an agent route needs before it is acted on,
#                from 0.50 to 0.99; absent uses the built-in default
#   base_url_var / key_var   for billing = "openrouter", the names of the
#                environment variables the harness reads for a base URL and a
#                key - never the values. The key is never a field of this file.
#   input        argv | stdin, for kind = "cli": whether the task replaces a
#                {task} token in args, or arrives on stdin
#   allow_write  for kind = "cli": whether the process may write inside its
#                working directory. false only detects a write after the
#                fact and fails the delegation; it does not stop one.
#   session_meta  for kind = "acp": a table sent as the session's _meta, so the
#                agent launches under these settings and never its working
#                folder's own. A non-empty table is what `confined` means.
#   tool_names   the agent's own tool names for each ACP tool kind. Every kind in
#                deny (and, in allowlist mode, every mapped kind not in allow)
#                is added to the agent's own disallowed list at launch.
#
# The allow and deny lists name ACP tool kinds - read, edit, delete, move,
# search, execute, think, fetch, switch_mode, other - falling back to the tool's
# title when an agent reports no kind.
"""


#: Whose settings an ``acp`` entry launches with (FR-131, G-60): its own ``session_meta`` or
#: its folder's.
SETTINGS_HARNESSED: Final = "harnessed"
SETTINGS_FOLDER: Final = "folder"
#: Why an ``acp`` entry runs with its folder's settings and cannot be fixed (FR-131).
NO_OVERRIDE_REASON: Final = "its adapter documents no override"


def settings_of(spec: AgentSpec) -> str | None:
    """``harnessed`` or ``folder`` for an ``acp`` entry, ``None`` for a ``cli`` entry.

    The one function ``doctor`` and ``/v1/agents`` both call, from the entry as declared.
    """
    if spec.kind != DEFAULT_KIND:
        return None
    return SETTINGS_HARNESSED if spec.session_meta else SETTINGS_FOLDER


def no_override(spec: AgentSpec) -> bool:
    """Whether ``spec`` runs with its folder's settings and its template documents no override."""
    template = TEMPLATES.get(spec.name)
    return (
        settings_of(spec) == SETTINGS_FOLDER and template is not None and not template.session_meta
    )


def template_named(name: str) -> AgentTemplate:
    """The template called ``name``, or a :class:`ConfigError` listing the ones that exist."""
    try:
        return TEMPLATES[name]
    except KeyError as exc:
        raise ConfigError(
            f"there is no agent template named {name!r}. Templates: {', '.join(TEMPLATES)}."
        ) from exc


def quota_markers_for(name: str) -> Mapping[str, tuple[Any, ...]]:
    """The quota markers of the template called ``name``, or none when there is no such template."""
    template = TEMPLATES.get(name)
    return {} if template is None else template.quota_markers


def render_entry(template: AgentTemplate) -> str:
    """One ``[agents.<name>]`` table, as TOML, ending in exactly one newline.

    Every key the spec carries away from its default is written, so what the loader reads back
    is what the template declared - a round trip rather than a resemblance.
    """
    spec = template.spec()
    lines = [
        f"# {template.summary}",
        f"[agents.{template.name}]",
        f"command = {_toml(spec.command)}",
        f"args = {_toml(list(spec.args))}",
        f"mode = {_toml(spec.mode)}",
        f"allow = {_toml(list(spec.allow))}",
        f"deny = {_toml(list(spec.deny))}",
        f"install = {_toml(spec.install)}",
    ]
    if spec.kind != DEFAULT_KIND:
        lines.append(f"kind = {_toml(spec.kind)}")
    if spec.use_for:
        lines.append(f"use_for = {_toml(spec.use_for)}")
    if spec.billing is not None:
        lines.append(f"billing = {_toml(spec.billing)}")
    if spec.base_url_var is not None:
        lines.append(f"base_url_var = {_toml(spec.base_url_var)}")
    if spec.key_var is not None:
        lines.append(f"key_var = {_toml(spec.key_var)}")
    if spec.input != DEFAULT_INPUT:
        lines.append(f"input = {_toml(spec.input)}")
    lines.extend(render_template_fields(template))
    return "\n".join(lines) + "\n"


def render_template_fields(template: AgentTemplate) -> tuple[str, ...]:
    """The ``session_meta = ...`` and ``tool_names = ...`` lines a template declares, if any."""
    spec = template.spec()
    lines: list[str] = []
    if spec.session_meta:
        lines.append(f"session_meta = {_toml_value(spec.session_meta)}")
    if spec.tool_names:
        lines.append(f"tool_names = {_toml_value(spec.tool_names)}")
    return tuple(lines)


def refresh_entry(text: str, name: str, lines: tuple[str, ...]) -> str:
    """``text`` with the template-owned ``lines`` set inside ``[agents.<name>]``, in place.

    A textual edit, not a TOML write: a line starting with the same key is replaced, a missing
    one is inserted after the span's last non-blank line, and every other byte keeps its place.
    The caller re-loads the result, which is what validates it.
    """
    rows = text.splitlines(keepends=True)
    header = f"[agents.{name}]"
    start = next((i for i, row in enumerate(rows) if row.strip() == header), None)
    if start is None:
        return text
    end = next((i for i in range(start + 1, len(rows)) if rows[i].startswith("[")), len(rows))
    for line in lines:
        key = line.split(" =", 1)[0]
        hit = next(
            (i for i in range(start + 1, end) if rows[i].startswith(f"{key} =")),
            None,
        )
        if hit is not None:
            ending = "\n" if rows[hit].endswith("\n") else ""
            rows[hit] = line + ending
            continue
        last = max(i for i in range(start, end) if rows[i].strip())
        if not rows[last].endswith("\n"):
            rows[last] += "\n"
        rows.insert(last + 1, line + "\n")
        end += 1
    return "".join(rows)


def render_example() -> str:
    """The whole committed example file: the documented header, then every template."""
    hints = "\n".join(f'# default = "{name}"' for name in TEMPLATES)
    parts = [_EXAMPLE_HEADER, hints + "\n"]
    parts.extend(render_entry(template) for template in TEMPLATES.values())
    return "\n".join(parts)


def load_agents(path: Path) -> dict[str, AgentSpec]:
    """Every agent declared in ``path``, keyed by name, validated eagerly."""
    document = _read(path)
    tables = document.get("agents")
    if not isinstance(tables, dict) or not tables:
        raise ConfigError(
            f"{path} declares no agents. Add an [agents.<name>] table with a 'command' key; "
            f"{EXAMPLE_PATH.name} shows the shape."
        )
    return {name: _spec(path, name, table) for name, table in tables.items()}


def agent_named(path: Path, name: str | None) -> AgentSpec:
    """The agent called ``name``, or the default one when ``name`` is ``None``."""
    agents = load_agents(path)
    if name is None:
        return agents[_default_name(path, agents)]
    try:
        return agents[name]
    except KeyError as exc:
        raise AgentNotFoundError(
            f"{path} declares no agent named {name!r}. Declared agents: "
            f"{', '.join(sorted(agents))}."
        ) from exc


# ---- internals -------------------------------------------------------------------------------


def _toml_value(value: object) -> str:
    """An inline TOML table (or any JSON-shaped value) in JSON's TOML-compatible spelling.

    JSON objects are not TOML inline tables, so a mapping is written key by key.
    """
    if isinstance(value, Mapping):
        body = ", ".join(
            f"{json.dumps(str(key))} = {_toml_value(item)}" for key, item in value.items()
        )
        return "{" + body + "}"
    if isinstance(value, tuple):
        return json.dumps(list(value))
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(value)


def _toml(value: str | list[str]) -> str:
    """A TOML literal for a string or a list of strings.

    JSON's syntax for both is a strict subset of TOML's, including the escapes, so the standard
    library's encoder is the whole implementation - and a hand-rolled quoter is one more thing
    that could put an unescaped backslash from a Windows path into a generated file.
    """
    return json.dumps(value)


def _read(path: Path) -> dict[str, Any]:
    """Parse ``path``, or say exactly which file is missing and where the template is."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"no agent registry at {path}. Copy {EXAMPLE_PATH.name} to {DEFAULT_PATH.name} and "
            f"point its 'command' key at a CLI that speaks the Agent Client Protocol."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _default_name(path: Path, agents: dict[str, AgentSpec]) -> str:
    """Which agent an unnamed delegation gets: the declared default, or the only one there is."""
    document = _read(path)
    declared = document.get("default")
    if isinstance(declared, str):
        if declared not in agents:
            raise ConfigError(
                f"{path} names {declared!r} as its default, which it does not declare. "
                f"Declared agents: {', '.join(sorted(agents))}."
            )
        return declared
    if len(agents) == 1:
        return next(iter(agents))
    raise ConfigError(
        f"{path} declares more than one agent and no default. Add a top-level "
        f'default = "<name>" line, or name one per delegation. '
        f"Declared agents: {', '.join(sorted(agents))}."
    )


def _spec(path: Path, name: str, table: object) -> AgentSpec:
    """One validated :class:`AgentSpec`, or a :class:`ConfigError` naming the offending key."""
    if not isinstance(table, dict):
        raise ConfigError(f"[agents.{name}] in {path} must be a table")
    unknown = sorted(set(table) - FIELDS)
    if unknown:
        raise ConfigError(
            f"[agents.{name}] in {path} has no key named {', '.join(repr(k) for k in unknown)}. "
            f"Known keys: {', '.join(sorted(FIELDS))}."
        )
    command = table.get("command")
    if not isinstance(command, str) or not command:
        raise ConfigError(f"[agents.{name}] in {path} needs a non-empty 'command'")
    mode = table.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        raise ConfigError(
            f"[agents.{name}] in {path} declares mode {mode!r}. Modes: {', '.join(MODES)}."
        )
    kind = table.get("kind", DEFAULT_KIND)
    if kind not in KINDS:
        raise ConfigError(
            f"[agents.{name}] in {path} declares kind {kind!r}. Kinds: {', '.join(sorted(KINDS))}."
        )
    billing = table.get("billing")
    if billing is not None and billing not in BILLINGS:
        raise ConfigError(
            f"[agents.{name}] in {path} declares billing {billing!r}. "
            f"Billing: {', '.join(sorted(BILLINGS))}."
        )
    return AgentSpec(
        name=name,
        command=command,
        args=_strings(path, name, "args", table.get("args", [])),
        cwd=_optional_string(path, name, "cwd", table.get("cwd")),
        mode=mode,
        allow=_strings(path, name, "allow", table.get("allow", [])),
        deny=_strings(path, name, "deny", table.get("deny", [])),
        timeout_s=_number(path, name, "timeout_s", table.get("timeout_s", DEFAULT_TIMEOUT_S)),
        ask_timeout_s=_number(
            path, name, "ask_timeout_s", table.get("ask_timeout_s", DEFAULT_ASK_TIMEOUT_S)
        ),
        install=_optional_string(path, name, "install", table.get("install")) or "",
        kind=kind,
        use_for=_optional_string(path, name, "use_for", table.get("use_for")) or "",
        not_for=_optional_string(path, name, "not_for", table.get("not_for")) or "",
        billing=billing,
        produces=_optional_string(path, name, "produces", table.get("produces")),
        outputs=_optional_string(path, name, "outputs", table.get("outputs")),
        floor=_floor(path, name, table["floor"]) if "floor" in table else None,
        base_url_var=_optional_string(path, name, "base_url_var", table.get("base_url_var")),
        key_var=_optional_string(path, name, "key_var", table.get("key_var")),
        input=_optional_string(path, name, "input", table.get("input")) or DEFAULT_INPUT,
        allow_write=_bool(path, name, "allow_write", table.get("allow_write"), default=False),
        session_meta=_table(path, name, "session_meta", table.get("session_meta")),
        tool_names=_tool_names(path, name, table.get("tool_names")),
    )


def _table(path: Path, name: str, key: str, value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[agents.{name}].{key} in {path} must be a table")
    return dict(value)


def _tool_names(path: Path, name: str, value: object) -> dict[str, tuple[str, ...]]:
    return {
        kind: _strings(path, name, f"tool_names.{kind}", names)
        for kind, names in _table(path, name, "tool_names", value).items()
    }


def _strings(path: Path, name: str, key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"[agents.{name}].{key} in {path} must be a list of strings")
    return tuple(str(item) for item in value)


def _optional_string(path: Path, name: str, key: str, value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ConfigError(f"[agents.{name}].{key} in {path} must be a string")


def _number(path: Path, name: str, key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"[agents.{name}].{key} in {path} must be a number of seconds")
    if value <= 0:
        raise ConfigError(f"[agents.{name}].{key} in {path} must be greater than zero")
    return float(value)


def _floor(path: Path, name: str, value: object) -> float:
    """A ``floor`` override, validated to ``[FLOOR_MIN, FLOOR_MAX]`` (decision 59)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"[agents.{name}].floor in {path} must be a number")
    number = float(value)
    if not (FLOOR_MIN <= number <= FLOOR_MAX):
        raise ConfigError(
            f"[agents.{name}].floor in {path} is {number!r}, outside [{FLOOR_MIN}, {FLOOR_MAX}]"
        )
    return number


def _bool(path: Path, name: str, key: str, value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"[agents.{name}].{key} in {path} must be true or false")
    return value
