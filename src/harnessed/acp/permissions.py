"""The fail-closed permission matrix (FR-22, ``acp.md`` "Permissions - fail closed, never hang").

Pure and synchronous. No I/O, no protocol, no event: :func:`decide` takes a policy and a tool
name and answers with a :class:`Decision`, and :mod:`~harnessed.acp.client` is what turns that
answer into a wire response and an ``acp.permission.decision`` event. Keeping the rule pure is
what makes the whole matrix testable without a subprocess.

The rules, in the order they are applied, because the order *is* the policy:

1. ``deny`` wins over everything, including ``mode = "allow"``. A tool named there is never run.
2. ``ask`` outside an interactive session degrades to ``allowlist`` and warns. This is the single
   most important line in the package: an unattended overnight run must neither sit on a prompt
   nor silently auto-approve, so it does the safe half of ``ask`` and says so.
3. ``ask`` inside an interactive session asks, and **denies on anything that is not an explicit
   allow** - a timeout, a rejection, or no prompter at all.
4. ``allow``, ``deny`` and ``allowlist`` answer from the configuration and never block on input.

The warning is a :func:`warnings.warn`, not a log line: there is no logging module in v1, the
architecture says observable moments publish events, and the degrade is *also* published, in the
``warning`` field of the decision event. The ``warn`` exists because a degrade that only shows up
in an event table is invisible to the operator watching the run.

``rule`` is the vocabulary a caller reads instead of re-deriving why a tool was refused, and
:data:`RULES` closes it. It covers the refusals as well as the approvals, so nothing downstream
has to reconstruct the reason from ``allowed`` plus the policy.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ALLOW_KINDS",
    "DEGRADE_WARNING",
    "DENY_KINDS",
    "MODES",
    "RULES",
    "Decision",
    "Pending",
    "Policy",
    "Prompter",
    "decide",
    "option_for",
    "row_of",
]

#: Every permission mode an agent may declare. Validated at load, not at decision time.
MODES: Final[tuple[str, ...]] = ("allow", "deny", "allowlist", "ask")

#: The closed vocabulary of reasons. One name per path through :func:`decide`.
RULES: Final[tuple[str, ...]] = (
    "deny_tools",
    "mode_allow",
    "mode_deny",
    "allowlist_hit",
    "allowlist_miss",
    "ask_degraded_allowlist",
    "ask_allowed",
    "ask_denied",
    "ask_timeout",
    "ask_unanswerable",
)

#: The ACP option kinds that mean yes, and the ones that mean no. The agent offers options by
#: kind, so an answer is chosen from what it offered rather than invented.
ALLOW_KINDS: Final[tuple[str, ...]] = ("allow_once", "allow_always")
DENY_KINDS: Final[tuple[str, ...]] = ("reject_once", "reject_always")

DEGRADE_WARNING: Final = (
    "an agent asked for permission in a non-interactive session, so the request was decided "
    "against the allowlist instead of prompting. An unattended run never waits on input."
)

#: How an interactive session is asked: a question, the option kinds on offer, and the ceiling
#: the caller must not exceed. ``None`` means the operator did not answer in time, which denies.
Prompter = Callable[[str, tuple[str, ...], float], "str | None"]


@dataclass(frozen=True, slots=True)
class Policy:
    """One agent's permission configuration, plus whether a human is present to ask."""

    mode: str
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    interactive: bool = False
    ask_timeout_s: float = 30.0
    #: Someone can answer a pending request: a session the control app opened (ticket 234, FR-130,
    #: gate ruling 6: "an answer door exists"). From ticket 240 a task's session has one too.
    answer_door: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    """The answer, and the named rule that produced it. ``warning`` is empty when there is none."""

    allowed: bool
    rule: str
    warning: str = ""


@dataclass(frozen=True, slots=True)
class Pending:
    """``ask`` with an answer door: nobody has answered yet, and :func:`decide` does not wait.

    The caller records the request and waits on its own thread, bounded by the ceiling; this module
    stays pure and synchronous.
    """

    tool: str


def row_of(decision: Decision) -> tuple[str, str, str | None]:
    """``(answer, rule, answered_via)`` as ``permission_decisions`` stores a decided request.

    ``rule`` is that table's vocabulary: ``allowlist`` or ``deny_list`` for the configuration,
    ``operator`` for a person at the terminal (``cli``), ``ceiling`` for a prompt nobody answered.
    """
    answer = "approve" if decision.allowed else "deny"
    if decision.rule in ("ask_allowed", "ask_denied"):
        return answer, "operator", "cli"
    if decision.rule == "ask_timeout":
        return answer, "ceiling", None
    if decision.rule == "ask_unanswerable":
        return answer, "operator", None
    return answer, ("allowlist" if decision.allowed else "deny_list"), None


def decide(policy: Policy, tool: str, *, prompter: Prompter | None = None) -> Decision | Pending:
    """Whether ``tool`` may run under ``policy``, or :class:`Pending` when only a person can say.

    Never raises, never blocks.
    """
    if tool in policy.deny:
        return Decision(allowed=False, rule="deny_tools")
    if policy.mode == "ask":
        return _ask(policy, tool, prompter)
    if policy.mode == "allow":
        return Decision(allowed=True, rule="mode_allow")
    if policy.mode == "deny":
        return Decision(allowed=False, rule="mode_deny")
    listed = tool in policy.allow
    return Decision(allowed=listed, rule="allowlist_hit" if listed else "allowlist_miss")


def option_for(allowed: bool, kinds: tuple[str, ...]) -> str | None:
    """The first offered kind matching the decision, or ``None`` when the agent offered none."""
    wanted = ALLOW_KINDS if allowed else DENY_KINDS
    return next((kind for kind in kinds if kind in wanted), None)


def _ask(policy: Policy, tool: str, prompter: Prompter | None) -> Decision | Pending:
    """The ``ask`` mode: an answer door, a terminal, or neither. Every half fails closed."""
    if policy.answer_door:
        return Pending(tool=tool)
    if not policy.interactive:
        warnings.warn(DEGRADE_WARNING, UserWarning, stacklevel=3)
        return Decision(
            allowed=tool in policy.allow,
            rule="ask_degraded_allowlist",
            warning=DEGRADE_WARNING,
        )
    if prompter is None:
        return Decision(allowed=False, rule="ask_unanswerable")
    question = f"the agent wants to run {tool!r}. Allow it?"
    answer = prompter(question, ALLOW_KINDS + DENY_KINDS, policy.ask_timeout_s)
    if answer is None:
        return Decision(allowed=False, rule="ask_timeout")
    allowed = answer in ALLOW_KINDS
    return Decision(allowed=allowed, rule="ask_allowed" if allowed else "ask_denied")
