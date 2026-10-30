"""The retrieval gate: one cheap question, asked once per run, that can only fail open (FR-12).

Retrieval is not free - it costs a search, a token budget and a chunk of the system prompt - so
something has to decide whether this task needs it at all. That decision is this module, and it
is deliberately the smallest thing that can make it: one request to a small model, one word back,
one boolean.

**The gate goes through the same seam as everything else** (architecture decision 7). It builds an
ordinary :class:`~harnessed.providers.types.ChatRequest` carrying
``metadata["scope"] = "_gate"``, so FakeProvider serves it out of
``corpus/wire/fake/_gate/`` with no special case, and the model name arrives as
``small_model`` from the caller's ``Settings.small_model`` rather than being read here - this
package reads no process variable of its own.

**Every failure fails open.** A provider that raises and an answer that is not a boolean are the
same event: retrieval proceeds and ``retrieval.gate.failed_open`` is published with the reason.
The gate is an optimization, and an optimization that can end a run is a liability, so there is
no path out of :func:`decide` that reports "do not retrieve" without the model having plainly
said so. The catch is narrow on purpose: ``Provider`` promises to raise a child of
``ProviderError`` and nothing else, so anything wider escaping an adapter is a bug in that
adapter and must arrive as a traceback rather than be absorbed here as a routine "no answer".

**It is consulted at most once per run**, which is a property of where the call sits rather than
of a counter: :func:`assemble_if_gated` is the one composed entry point, the caller invokes it
once before the loop starts, and nothing inside ``loop/`` can reach it.

**Nothing here reaches the event bus or the stores directly.** ``publish`` and ``assemble`` are
both injected, the way the sandbox root is injected into the tool registry and the call
serializer into the run journal, so a test can hand this module a counting spy and read the
answer off the number.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from harnessed.errors import ProviderError
from harnessed.memory import retrieval
from harnessed.providers.types import ChatRequest, ChatResponse, Message

__all__ = [
    "AFFIRMATIVE",
    "DECIDED",
    "FAILED_OPEN",
    "GATE_SCOPE",
    "NEGATIVE",
    "SYSTEM_PROMPT",
    "Decision",
    "Model",
    "RouterMemory",
    "assemble_if_gated",
    "decide",
]

#: The fixture scope every gate request declares. FakeProvider keys selection on it.
GATE_SCOPE: Final = "_gate"

#: Published on every decision, including one the gate had to make for itself.
DECIDED: Final = "retrieval.gate.decided"

#: Published when the gate could not get an answer it understood and admitted retrieval anyway.
FAILED_OPEN: Final = "retrieval.gate.failed_open"

#: One word out, so the answer is cheap to produce and cheap to check.
SYSTEM_PROMPT: Final = (
    "You decide whether a task needs the agent's stored memory - its facts, its past episodes "
    "and its learned procedures - before the work starts. Answer with exactly one word: yes if "
    "the task would benefit from that memory, no if it is self-contained. No punctuation, no "
    "explanation, no other word."
)

#: A one-word answer needs very few tokens, and a ceiling this low keeps the gate cheap live.
MAX_TOKENS: Final = 8

#: The two vocabularies. Anything outside them is unparseable, which fails open.
AFFIRMATIVE: Final[frozenset[str]] = frozenset({"yes", "y", "true", "retrieve", "1"})
NEGATIVE: Final[frozenset[str]] = frozenset({"no", "n", "false", "skip", "0"})

#: A word of the answer. Trailing punctuation and surrounding whitespace are not disagreement.
_WORD_RE: Final = re.compile(r"[^\W_]+", re.UNICODE)


class Model(Protocol):
    """What the gate knows about a provider: it turns one request into one response.

    Structurally identical to ``providers.base.Provider`` and to ``loop.engine.Model``, and
    deliberately restated here rather than imported, so ``memory/`` reaches exactly one module
    under ``providers/`` - the normalized types every package already speaks. Anything satisfying
    one satisfies all three, which is what a Protocol is for.
    """

    def complete(self, request: ChatRequest) -> ChatResponse: ...


#: How assembly is invoked. Injected so a spy can count the calls the gate did or did not make.
Assembler = Callable[..., retrieval.Assembled]

#: How an event leaves this module: a name and a payload, exactly ``EventBus.publish``'s shape.
Publisher = retrieval.Publisher


def _no_publish(name: str, payload: Mapping[str, object]) -> None:
    """The default: a gate with nobody listening, which is a valid way to run."""


@dataclass(frozen=True, slots=True)
class Decision:
    """What the gate concluded, why, and whether it had to conclude it on its own.

    ``failed_open`` is the honest half: a ``True`` here means the model never gave a usable
    answer and retrieval is running because the gate refuses to be the reason a run loses its
    memory, not because anything decided it should.
    """

    retrieve: bool
    answer: str = ""
    failed_open: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RouterMemory:
    """The router's own retrieval verdict, already turned into a decision by its caller (decision
    7 as amended, ticket 169): handing one to :func:`decide` or :func:`assemble_if_gated` makes
    **0** ``_gate`` calls - ``retrieve`` is trusted as given, never parsed from a model's text.

    ``retrieve`` is ``None`` for a router answer with no ``needs_memory`` probability at all: the
    gate fails open exactly as it does for an unparseable v4 answer, only here the decision was
    never made rather than garbled. ``answer`` is the decimal string the caller compared against
    the threshold (or ``""`` when there was none), carried straight into ``retrieval.gate.decided``
    so the published payload keeps its ``answer`` key either way. ``kinds`` narrows
    :func:`~harnessed.memory.retrieval.assemble` the same way ``assemble_if_gated``'s own ``kinds``
    keyword does.

    Built by a caller that reads a router ``Verdict`` and ``router/questions.py``'s
    ``NEEDS_MEMORY_THRESHOLD`` - this module imports neither, so ``memory/`` learns no router type
    and no threshold.
    """

    retrieve: bool | None
    answer: str
    kinds: Sequence[str]


def decide(
    model: Model,
    task: str,
    *,
    small_model: str,
    publish: Publisher = _no_publish,
    answered: RouterMemory | None = None,
) -> Decision:
    """Ask once whether ``task`` needs retrieval. Never raises; publishes what it concluded.

    ``answered`` is the router's own verdict (decision 7 as amended, ticket 169): given one, this
    makes **0** calls to ``model`` at all - ``retrieve`` is trusted as given, or the gate fails
    open when the router had no ``needs_memory`` probability to give. ``answered=None`` (every
    fallback, ``HARNESSED_ROUTER=off``, and every eval path) runs the v4 request unchanged.
    """
    if answered is not None:
        if answered.retrieve is None:
            return _fail_open(publish, answered.answer, "router answered no needs_memory")
        return _decided(publish, Decision(retrieve=answered.retrieve, answer=answered.answer))

    request = ChatRequest(
        model=small_model,
        system=SYSTEM_PROMPT,
        messages=(Message(role="user", content=task),),
        max_tokens=MAX_TOKENS,
        metadata={"scope": GATE_SCOPE},
    )
    try:
        response = model.complete(request)
    except ProviderError as exc:
        return _fail_open(publish, "", f"the gate's provider raised {type(exc).__name__}: {exc}")

    verdict = _verdict(response.content)
    if verdict is None:
        return _fail_open(
            publish,
            response.content,
            f"the gate answered {response.content!r}, which is not yes or no",
        )
    return _decided(publish, Decision(retrieve=verdict, answer=response.content))


def assemble_if_gated(
    model: Model,
    conn: sqlite3.Connection,
    query: str,
    *,
    budget: int,
    small_model: str,
    run_id: str | None = None,
    kinds: Sequence[str] = retrieval.ALL_KINDS,
    assemble: Assembler = retrieval.assemble,
    publish: Publisher = _no_publish,
    answered: RouterMemory | None = None,
) -> tuple[Decision, retrieval.Assembled]:
    """Consult the gate once, then either assemble the context or hand back an empty one.

    A declined run does not assemble a shorter context, it assembles none: ``assemble`` is never
    called, so a declining gate costs exactly one small request and zero searches.

    ``kinds`` is carried straight through to assembly and means nothing to the gate itself - the
    decision is about the task, not about which stores would answer it. ``answered`` overrides it
    with :attr:`RouterMemory.kinds` when given (ticket 169): the router's own caller is the one
    that knows which kinds it did not already ask assembly to skip.
    """
    decision = decide(model, query, small_model=small_model, publish=publish, answered=answered)
    if not decision.retrieve:
        return decision, retrieval.Assembled(text="", tokens=0, budget=budget)
    resolved_kinds = kinds if answered is None else answered.kinds
    assembled = assemble(
        conn, query, budget=budget, run_id=run_id, kinds=resolved_kinds, publish=publish
    )
    return decision, assembled


def _verdict(content: str) -> bool | None:
    """``True``, ``False``, or ``None`` for an answer no reader could call a boolean.

    Exactly one word is required, because the gate asked for exactly one word. Anything longer is
    the model explaining itself, and an explanation the gate would have to interpret is precisely
    what "cannot be parsed into a boolean" means. Being strict here is safe: the only thing an
    unparseable answer buys is retrieval running.
    """
    words = _WORD_RE.findall(content.casefold())
    if len(words) != 1:
        return None
    if words[0] in AFFIRMATIVE:
        return True
    if words[0] in NEGATIVE:
        return False
    return None


def _fail_open(publish: Publisher, answer: str, reason: str) -> Decision:
    """Record why the gate could not decide, then decide to retrieve."""
    publish(FAILED_OPEN, {"reason": reason, "answer": answer, "retrieve": True})
    open_anyway = Decision(retrieve=True, answer=answer, failed_open=True, reason=reason)
    return _decided(publish, open_anyway)


def _decided(publish: Publisher, decision: Decision) -> Decision:
    """One publication for both outcomes, so a listener counts decisions rather than branches."""
    publish(
        DECIDED,
        {
            "retrieve": decision.retrieve,
            "failed_open": decision.failed_open,
            "answer": decision.answer,
            "reason": decision.reason,
        },
    )
    return decision
