"""Assemble the retrieval context, once per run, under a hard token ceiling (FR-11).

One run asks this module one question and gets one string back. The string is built from the
three stores in a fixed order, it is cut to fit the budget, and the cut is a **prefix** of that
order: a lower budget keeps strictly less, never something different. That is what makes
"truncation is deterministic" a property a reader can see rather than a claim a test makes.

**The order is a priority, and it is deliberate.**

1. **Procedures.** Promoted, durable knowledge. If anything survives a small budget it is this.
2. **Facts.** Searched, so they are already the ones the task matched.
3. **Episodes.** What this same run just did. They truncate first because they are the most
   recoverable: the transcript still holds them.

**The ceiling is measured on the finished text, not on the parts.** Each candidate is admitted by
rendering the whole context with it included and counting that, so the headings and the
separators are inside the budget rather than overhead nobody counted. It costs one count per
candidate and it is the only way ``count_tokens(assembled.text) <= budget`` can be an assertion
about what the model is actually handed.

**Nothing here reaches the event bus directly.** ``publish`` is injected, the way the sandbox root
is injected into the tool registry and the call serializer into the run journal, so ``memory/``
keeps its own import list and a test can hand it the real ``EventBus.publish`` or nothing at all.

**The query never reaches FTS5 as the user typed it.** A task is arbitrary text and ``MATCH`` has
a syntax; assembly is not a place a run may die, so the query is rebuilt from its words.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from harnessed.errors import ConfigError
from harnessed.memory import episodic, procedural, semantic
from harnessed.memory.budget import count_tokens

__all__ = [
    "ASSEMBLED",
    "BUDGET_TRUNCATED",
    "Assembled",
    "Item",
    "Publisher",
    "assemble",
    "handed",
]

#: Published when at least one candidate did not fit, before the completion event.
BUDGET_TRUNCATED: Final = "retrieval.budget.truncated"

#: Published when assembly finishes, truncated or not. Both names live in ``events/names.py``.
ASSEMBLED: Final = "retrieval.assembled"

#: The default fan-in per store. Wide enough that the budget, not the query, is the binding limit.
DEFAULT_LIMIT: Final = 50

#: Every kind assembly may draw from, in the priority order above. A caller that already loads
#: one of them by another route narrows this instead of editing the finished text - text that
#: was rendered and then removed was still counted against the budget on the way through.
ALL_KINDS: Final[tuple[str, ...]] = ("procedure", "fact", "episode")

_HEADINGS: Final[Mapping[str, str]] = {
    "procedure": "# Procedures",
    "fact": "# Facts",
    "linked": "# Linked facts",
    "episode": "# Episodes",
}

#: How many links are followed out of a matched fact. Exactly one hop, and the number is here as
#: a constant so that raising it is a visible edit rather than a loop bound somebody widened.
#:
#: Two hops from a well-connected fact is the whole store, which is the problem link expansion
#: exists to solve rather than a stronger version of the solution.
_LINK_HOPS: Final = 1

#: A word of the task, for rebuilding the search query. Single characters are dropped as noise.
_WORD_RE: Final = re.compile(r"\w{2,}", re.UNICODE)

#: Words that carry no meaning for matching, dropped before the query is built (ticket 048).
#:
#: The terms are joined with ``OR``, so **one** of these left in makes every note containing it a
#: hit. Found by the judge tier: asked "What port does the ingest service listen on?" against a
#: store of four sentences, retrieval returned the answering fact and two unrelated ones, because
#: all three contain "the". Ranking did not save it - the answering fact came back first and the
#: token ceiling was wide enough to admit the rest anyway.
#:
#: **This list is short and it is English.** It covers the words that appear in ordinary questions
#: and nowhere in a fact worth storing. It is not a linguistic resource and must not grow into one:
#: a stemmer, a synonym table or a similarity threshold are ranking decisions dressed as matching
#: decisions, and v1's answer to ranking quality is embeddings in v2. A harness whose operator
#: writes tasks in another language should empty this set rather than translate it - a half-
#: translated stopword list silently drops meaningful words.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "about",
        "after",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "here",
        "him",
        "his",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "under",
        "until",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

#: How many words of the task reach the search. A bounded query keeps the plan bounded too.
_MAX_QUERY_TERMS: Final = 20


#: How an event leaves this module: a name and a payload, exactly ``EventBus.publish``'s shape.
Publisher = Callable[[str, Mapping[str, object]], object]


def _no_publish(name: str, payload: Mapping[str, object]) -> None:
    """The default: assembly with nobody listening, which is a valid way to run."""


@dataclass(frozen=True, slots=True)
class Item:
    """One candidate for the context: where it came from, what it is, and its text."""

    kind: str
    ref: str
    text: str


@dataclass(frozen=True, slots=True)
class Assembled:
    """What one assembly produced, plus the accounting FR-11 asserts on."""

    text: str
    tokens: int
    budget: int
    included: tuple[Item, ...] = ()
    dropped: tuple[Item, ...] = ()

    @property
    def truncated(self) -> bool:
        """True when the budget, not the store, decided where the context ended."""
        return bool(self.dropped)


def handed(text: str, *, budget: int) -> Assembled:
    """Context a caller already has, counted the way an assembly is, with nothing drawn from the
    store: a chain's Groq hop, whose user-turn context is the last agent's result (decision 60,
    ticket 176). Counted here so ``count_tokens`` keeps its closed set of callers (decision 6).
    """
    return Assembled(text=text, tokens=count_tokens(text), budget=budget)


def assemble(
    conn: sqlite3.Connection,
    query: str,
    *,
    budget: int,
    run_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    kinds: Sequence[str] = ALL_KINDS,
    tag: str | None = None,
    publish: Publisher = _no_publish,
) -> Assembled:
    """Build one run's retrieval context, never exceeding ``budget`` tokens.

    ``budget`` is passed in rather than read here, because this package reads no process
    variable of its own; the caller resolves it from ``Settings.retrieval_token_budget``, which
    ``--token-budget`` and ``HARNESSED_TOKEN_BUDGET`` both feed.

    ``kinds`` narrows which stores are drawn from, defaulting to all three. It exists for a
    caller that already loads one kind by another route: ``harnessed run`` puts the active
    procedures into the system prompt wholesale, so assembling them again would put the same
    sentence in the prompt twice and charge the budget for the second copy. Narrowing here rather
    than editing the finished text matters, because a candidate is admitted by counting the
    whole rendered context - text that was rendered and then removed still displaced something.
    """
    if budget < 0:
        raise ConfigError(
            f"retrieval token budget must not be negative, got {budget}. "
            f"Zero is the way to say 'assemble nothing'."
        )
    unknown = sorted(set(kinds) - set(ALL_KINDS))
    if unknown:
        raise ConfigError(
            f"retrieval kinds {', '.join(unknown)} are not memory kinds. "
            f"The three are {', '.join(ALL_KINDS)}."
        )

    included: list[Item] = []
    dropped: list[Item] = []
    text = ""
    tokens = 0

    for item in _candidates(conn, query, run_id=run_id, limit=limit, kinds=kinds, tag=tag):
        if dropped:
            # The boundary is a prefix: once one candidate did not fit, the rest go with it, so
            # a smaller budget can only ever keep less rather than a different selection.
            dropped.append(item)
            continue
        candidate = _render((*included, item))
        counted = count_tokens(candidate)
        if counted > budget:
            dropped.append(item)
            continue
        included.append(item)
        text, tokens = candidate, counted

    if dropped:
        publish(
            BUDGET_TRUNCATED,
            {"budget": budget, "tokens": tokens, "kept": len(included), "dropped": len(dropped)},
        )
    publish(
        ASSEMBLED,
        {"budget": budget, "tokens": tokens, "kept": len(included), "dropped": len(dropped)},
    )
    return Assembled(
        text=text,
        tokens=tokens,
        budget=budget,
        included=tuple(included),
        dropped=tuple(dropped),
    )


def _candidates(
    conn: sqlite3.Connection,
    query: str,
    *,
    run_id: str | None,
    limit: int,
    kinds: Sequence[str] = ALL_KINDS,
    tag: str | None = None,
) -> tuple[Item, ...]:
    """Every candidate, in priority order: procedures, facts, followed facts, this run's episodes.

    A kind the caller did not ask for is not searched at all, rather than searched and then
    filtered - the cheapest query is the one that never runs.

    **Followed facts are part of the ``fact`` kind, not a fourth one.** A caller asking for facts
    is asking what the store knows about the task, and a link is one of the two ways the store
    answers: the search finds a fact sharing words with the task, a link finds one sharing none.
    They are separated by :attr:`Item.kind` so a reader of the assembled context can tell how each
    arrived, and ordered after the matched facts so the prefix rule admits a match before a
    follow.

    **Retired knowledge stays retired, in both stores.** Archived facts are excluded on both
    routes: ``semantic.search`` leaves them out by default, and expansion re-reads each target
    rather than trusting the link, so a fact retired after something linked to it stays retired.
    Procedures are drawn from the active set for the same reason. ``procedural.all`` is every
    version ever written, oldest first, which would offer a procedure the improve loop demoted -
    under the label the assembler presents as promoted, durable knowledge - and, past ``limit``,
    would prefer the earliest versions over the ones that replaced them.
    """
    wanted = set(kinds)
    procedures = (
        tuple(Item("procedure", row.proc_id, row.text) for row in procedural.active(conn)[:limit])
        if "procedure" in wanted
        else ()
    )
    search = _search_query(query) if "fact" in wanted else None
    matched = () if search is None else tuple(semantic.search(conn, search, limit, tag=tag))
    facts = tuple(Item("fact", row.mem_id, row.text) for row in matched)
    followed = tuple(
        Item("linked", row.mem_id, row.text) for row in _one_hop(conn, matched, limit=limit)
    )
    episodes = (
        tuple(
            Item("episode", row.episode_id, row.text)
            for row in episodic.for_run(conn, run_id)[:limit]
        )
        if run_id is not None and "episode" in wanted
        else ()
    )
    return procedures + facts + followed + episodes


def _one_hop(
    conn: sqlite3.Connection, matched: Sequence[semantic.Fact], *, limit: int
) -> tuple[semantic.Fact, ...]:
    """The facts one link out from ``matched``, in the order their sources matched.

    Each target is re-read rather than taken on trust, which is what excludes a fact archived
    after something linked to it, and what keeps a link from resurrecting a retired fact.
    Already-matched ids are skipped so a mutual link cannot put the same fact in twice.
    """
    seen = {fact.mem_id for fact in matched}
    out: list[semantic.Fact] = []
    for fact in matched:
        for to_id in fact.links[:limit]:
            if to_id in seen:
                continue
            seen.add(to_id)
            target = semantic.get(conn, to_id)
            if target is not None and target.status == semantic.ACTIVE:
                out.append(target)
    return tuple(out[:limit])


def _search_query(query: str) -> str | None:
    """Rebuild ``query`` out of its meaningful words, or ``None`` when it holds none.

    An operator the user never meant to type - a quote, ``NEAR/``, a bare ``AND`` - is a syntax
    error to FTS5 and would end the run. Words joined by ``OR`` say what the caller meant and
    cannot be malformed. That reasoning is unchanged; what ticket 048 added is that a query which
    cannot be malformed can still be meaningless.

    :data:`_STOPWORDS` comes out first, because the terms are joined with ``OR`` and one ``the``
    left in the query matches every note that contains one.

    **A task made entirely of small words falls back to all of them.** "What is it on the" drops
    to nothing, and a query with no terms retrieves nothing at all - which is a worse answer to a
    short question than retrieving loosely. The fallback keeps such a run's memory attached, at
    exactly the cost this function otherwise removes, in the rarer case.
    """
    words = list(dict.fromkeys(_WORD_RE.findall(query.lower())))
    meaningful = [word for word in words if word not in _STOPWORDS]
    chosen = (meaningful or words)[:_MAX_QUERY_TERMS]
    return " OR ".join(chosen) if chosen else None


def _render(items: Sequence[Item]) -> str:
    """One heading per kind, then one bullet per item. Dull on purpose: the model reads it."""
    lines: list[str] = []
    heading = ""
    for item in items:
        if _HEADINGS[item.kind] != heading:
            heading = _HEADINGS[item.kind]
            lines.append(heading)
        lines.append(f"- {item.text}")
    return "\n".join(lines)
