"""The three questions, the registry-built route options and the floors (decisions 58, 59;
FR-101, FR-102, FR-103).

``FIXED_ROUTE_OPTIONS`` is the vocabulary that exists before any agent joins the choice -
``groq_chat``, ``groq_memory`` and ``needs_human``, in that order. :func:`route_options` widens it
with one option per installed registry entry, named by the entry, inserted between the Groq pair
and ``needs_human`` in registry order (decision 58's middle clause); :func:`route_descriptions`
pairs every option, fixed or registry, with the text decision 58 says describes it.

An entry is an option only when its executable resolves - the same ``shutil.which(spec.command)``
check ``doctor`` reports (``cli/doctor_cmd.py``) - reimplemented here with ``shutil`` directly
rather than by importing that module: ``router/`` may not import ``cli/``.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from decimal import Decimal
from typing import Final

from harnessed.acp.config import AgentSpec
from harnessed.providers.jev import Question

__all__ = [
    "DEFAULT_AGENT_FLOOR",
    "CONTINUATION_DONE_DESCRIPTION",
    "DEFAULT_RETRIEVAL_KINDS",
    "DONE",
    "FIXED_ROUTE_OPTIONS",
    "FLOORS",
    "MEMORY_KINDS",
    "NEEDS_MEMORY_THRESHOLD",
    "QUESTIONS",
    "RETRIEVAL_KINDS_FOR",
    "TASK_DONE_FLOOR",
    "MEMORY_KIND_DESCRIPTIONS",
    "MEMORY_KIND_INSTRUCTIONS",
    "MEMORY_KIND_QUESTION",
    "NEEDS_MEMORY_INSTRUCTIONS",
    "NEEDS_MEMORY_QUESTION",
    "ROUTE_INSTRUCTIONS",
    "ROUTE_QUESTION",
    "build_route_question",
    "floor_for",
    "retrieval_kinds_for",
    "route_descriptions",
    "route_options",
]

#: Decision 58's fixed vocabulary, in the order a `route` choice must offer it. `needs_human`
#: stays last even before the registry grows the middle: an entry sits between the two Groq
#: options and this one, never after it.
FIXED_ROUTE_OPTIONS: Final[tuple[str, ...]] = ("groq_chat", "groq_memory", "needs_human")

#: A chain hop's extra option (decision 58, FR-117): offered last, and only after an agent has
#: returned - a message's first decision never offers it. It has no floor and no handler.
DONE: Final = "done"

#: The confidence a task's continuation must answer `done` at for the task to end (decision 59 as
#: amended, FR-141): as high as an agent route, because a premature `done` ends the job.
TASK_DONE_FLOOR: Final = Decimal("0.80")

#: `semantic`, `procedural`, `episodic` - `memory/retrieval.py::ALL_KINDS`'s own three, restated
#: here because `router/` may not import `memory.retrieval` for a single tuple (FR-101, decision
#: 56's import row names `harnessed.memory`, the package, not this one internal constant).
MEMORY_KINDS: Final[tuple[str, ...]] = ("semantic", "procedural", "episodic")

#: A route's confidence floor (decision 59): the two fixed Groq routes'. `needs_human` has none
#: because answering it is itself the uncertain stop, never a route acted on above a threshold.
#: Every other route - an agent entry - is :func:`floor_for`'s job: its default is
#: :data:`DEFAULT_AGENT_FLOOR`, overridden per entry by ``AgentSpec.floor`` (``acp/config.py``
#: already validates that override to ``[0.50, 0.99]`` at load time, so this module trusts it).
FLOORS: Final[dict[str, Decimal]] = {
    "groq_chat": Decimal("0.50"),
    "groq_memory": Decimal("0.50"),
}

#: Every agent route's floor absent a per-entry override (decision 59, FR-103).
DEFAULT_AGENT_FLOOR: Final = Decimal("0.80")


def floor_for(route: str, agents: Mapping[str, AgentSpec] | None = None) -> Decimal:
    """The confidence floor ``route`` is checked against (decision 59).

    :data:`FLOORS`'s own value for the two fixed Groq routes; otherwise the matching entry's
    ``floor`` override when ``agents`` names one, else :data:`DEFAULT_AGENT_FLOOR`. `needs_human`
    is never asked here - it has no floor to check (``route.py`` branches on it first).
    """
    if route in FLOORS:
        return FLOORS[route]
    spec = None if agents is None else agents.get(route)
    if spec is not None and spec.floor is not None:
        return Decimal(str(spec.floor))
    return DEFAULT_AGENT_FLOOR


#: Decision 58's static text for the three fixed options - never sourced from a registry entry.
#: This is the wording the 2026-09-25 spike measured (`research.md` Appendix A, ticket 196): the
#: prior text here was never what the spike sent, and a live probe with this wording sent `ping`
#: and the notes task to `groq_chat` at 0.92 and 0.97.
_FIXED_DESCRIPTIONS: Final[dict[str, str]] = {
    "groq_chat": (
        "A direct conversational answer from general knowledge: greetings, definitions, "
        "explanations, advice, or writing a short text or file. No web, no live facts, no "
        "repository."
    ),
    "groq_memory": (
        "Answering or storing needs the operator's own memory: who they are, what they built, "
        "their past decisions, earlier sessions, or facts they told the harness before."
    ),
    "needs_human": "Ambiguous, destructive or unsafe requests the operator must confirm first.",
    DONE: "the previous step already finished the task; Groq writes the reply from its output",
}

#: What `done` means at hop 0 of a task's continuation (ticket 275, FR-141): that state has no
#: `Previous step:` section, so the criterion is judged against what it does show.
CONTINUATION_DONE_DESCRIPTION: Final = (
    "the task under 'The task:' is finished, judged by the last reply (under 'Last reply:' or "
    "the last assistant line), and nothing more is needed"
)

#: Where `needs_memory` retrieves (decision 59). `cli/run_cmd.py::_router_memory` compares a
#: routed verdict's `needs_memory` against this to build the gate's `RouterMemory` (decision 7 as
#: amended, ticket 169); `memory/gate.py` imports neither this constant nor `Verdict`.
NEEDS_MEMORY_THRESHOLD: Final = Decimal("0.5")

#: `memory_kind`'s answer, mapped onto `retrieval.assemble`'s own `kinds` vocabulary (FR-111): a
#: `semantic` question searches facts, an `episodic` one searches this run's episodes, and a
#: `procedural` one retrieves nothing extra - a procedure already loads wholesale into the system
#: prompt (decision 7, `Session.system()`), so searching for one here would offer it twice.
#: Written as the literal strings `retrieval.py::ALL_KINDS` uses rather than imported from it, the
#: same restatement `MEMORY_KINDS` already makes: `router/` may not import `memory.retrieval`.
RETRIEVAL_KINDS_FOR: Final[dict[str, tuple[str, ...]]] = {
    "semantic": ("fact",),
    "episodic": ("episode",),
    "procedural": (),
}

#: `_router_memory`'s kinds before this ticket, and still what it falls back to for a
#: `memory_kind` this map does not name - a decider can only ever answer one of `MEMORY_KINDS`,
#: so the fallback is defensive rather than a route a real answer takes.
DEFAULT_RETRIEVAL_KINDS: Final[tuple[str, ...]] = ("fact", "episode")


def retrieval_kinds_for(memory_kind: str | None) -> tuple[str, ...]:
    """`RETRIEVAL_KINDS_FOR[memory_kind]`, or :data:`DEFAULT_RETRIEVAL_KINDS` when `memory_kind`
    is `None` or not one of :data:`MEMORY_KINDS` (FR-111)."""
    if memory_kind is None:
        return DEFAULT_RETRIEVAL_KINDS
    return RETRIEVAL_KINDS_FOR.get(memory_kind, DEFAULT_RETRIEVAL_KINDS)


#: What each question asks, sent as its ``instructions`` (ticket 195). The state is a string, so
#: the text names "this task" rather than a backticked state field.
ROUTE_INSTRUCTIONS: Final = "Which handler should carry out this task?"
NEEDS_MEMORY_INSTRUCTIONS: Final = "Does answering this task need the operator's stored memory?"
MEMORY_KIND_INSTRUCTIONS: Final = "Which kind of memory would this task need?"

#: `memory_kind`'s option descriptions, sent as its ``criteria`` (the recorded request in
#: ``.spec/operator-inputs/195/``).
MEMORY_KIND_DESCRIPTIONS: Final[dict[str, str]] = {
    "semantic": "facts about the operator or projects",
    "procedural": "how-to procedures",
    "episodic": "past sessions and decisions",
}

ROUTE_QUESTION: Final = Question(
    name="route",
    kind="choice",
    options=FIXED_ROUTE_OPTIONS,
    instructions=ROUTE_INSTRUCTIONS,
    criteria={option: _FIXED_DESCRIPTIONS[option] for option in FIXED_ROUTE_OPTIONS},
)
NEEDS_MEMORY_QUESTION: Final = Question(
    name="needs_memory", kind="noul", instructions=NEEDS_MEMORY_INSTRUCTIONS
)
MEMORY_KIND_QUESTION: Final = Question(
    name="memory_kind",
    kind="choice",
    options=MEMORY_KINDS,
    instructions=MEMORY_KIND_INSTRUCTIONS,
    criteria=MEMORY_KIND_DESCRIPTIONS,
)

#: One call, three questions, together (decision 58, FR-101). ``ROUTE_QUESTION`` is the no-agents
#: baseline; a caller with a registry asks :func:`build_route_question` for the widened one.
QUESTIONS: Final[tuple[Question, ...]] = (
    ROUTE_QUESTION,
    NEEDS_MEMORY_QUESTION,
    MEMORY_KIND_QUESTION,
)


def _installed(spec: AgentSpec) -> bool:
    """Whether ``spec.command`` resolves on this machine - the same check ``doctor`` reports."""
    return shutil.which(spec.command) is not None


def route_options(
    agents: Mapping[str, AgentSpec] | None = None,
    *,
    hop: bool = False,
    exclude: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Decision 58's `route` vocabulary: :data:`FIXED_ROUTE_OPTIONS`'s Groq pair, one option per
    installed entry in ``agents`` named by the entry and in registry order, then `needs_human`,
    then :data:`DONE` last on a chain hop only (``hop=True``). An entry whose executable does not
    resolve is left out rather than failing at delegation.

    ``exclude`` names the entry that produced the previous step: on a hop it is not offered again
    (ticket 220); at hop 0 it is ignored."""
    resolved = {} if agents is None else agents
    skipped = exclude if hop else ()
    entries = tuple(
        name for name, spec in resolved.items() if name not in skipped and _installed(spec)
    )
    options = (*FIXED_ROUTE_OPTIONS[:-1], *entries, FIXED_ROUTE_OPTIONS[-1])
    return (*options, DONE) if hop else options


def route_descriptions(
    agents: Mapping[str, AgentSpec] | None = None,
    *,
    hop: bool = False,
    exclude: tuple[str, ...] = (),
    continuation: bool = False,
) -> dict[str, str]:
    """Every option :func:`route_options` would offer, paired with decision 58's description: a
    fixed option's is :data:`_FIXED_DESCRIPTIONS`; an installed entry's is its `use_for`, then
    `" Not for: "` and its `not_for` when it has one. ``continuation`` swaps :data:`DONE`'s text for
    :data:`CONTINUATION_DONE_DESCRIPTION` (ticket 275)."""
    resolved = {} if agents is None else agents
    descriptions = {key: text for key, text in _FIXED_DESCRIPTIONS.items() if hop or key != DONE}
    if hop and continuation:
        descriptions[DONE] = CONTINUATION_DONE_DESCRIPTION
    skipped = exclude if hop else ()
    for name, spec in resolved.items():
        if name not in skipped and _installed(spec):
            not_for = f" Not for: {spec.not_for}" if spec.not_for else ""
            descriptions[name] = f"{spec.use_for}{not_for}"
    return descriptions


def build_route_question(
    agents: Mapping[str, AgentSpec] | None = None,
    *,
    hop: bool = False,
    exclude: tuple[str, ...] = (),
    continuation: bool = False,
) -> Question:
    """The `route` question widened by ``agents`` (decision 58's middle clause, FR-102), with
    :data:`DONE` last on a chain hop (``hop=True``, FR-117) and ``exclude`` withheld on a hop
    (ticket 220). Its ``criteria`` are :func:`route_descriptions`, so every offered option
    reaches Jev with its text (ticket 195). ``continuation`` marks a task's hop 0, where ``done`` is
    described against the task and its last reply (ticket 275)."""
    return Question(
        name="route",
        kind="choice",
        options=route_options(agents, hop=hop, exclude=exclude),
        instructions=ROUTE_INSTRUCTIONS,
        criteria=route_descriptions(agents, hop=hop, exclude=exclude, continuation=continuation),
    )
