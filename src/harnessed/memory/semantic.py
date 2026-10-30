"""Semantic memory: facts, searchable (FR-10).

One fact is one row in ``mem_semantic`` plus one row in the ``mem_semantic_fts`` FTS5 index,
written in the same transaction. FTS5 is a hard requirement rather than a soft one: ``harnessed
doctor --offline`` asserts it is compiled in, so a CPython without it fails the gate instead of
degrading to a search that quietly means something different.

Reading is two layers, and which one a caller wants depends on what it holds. :func:`search`
takes an **FTS5 expression** and is strict: a malformed one is a :class:`StorageError`, which is
what made ticket 121's defect visible rather than silent. :func:`searched` takes **text an
operator typed** and builds the expression first, so a question mark, a hyphen or an unclosed
quote is something to look for rather than syntax to trip over. A search box calls the second.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Final

from harnessed import clock, ids
from harnessed.errors import StorageError
from harnessed.memory import db, redact

__all__ = [
    "ACTIVE",
    "ARCHIVED",
    "MAX_SEARCH_TERMS",
    "NO_TERMS_NOTE",
    "Fact",
    "Search",
    "archive",
    "by_source",
    "get",
    "json_payload",
    "match_query",
    "match_terms",
    "put",
    "put_replacing",
    "quoted",
    "recent_by_tag",
    "search",
    "searched",
]

#: A fact that retrieval may draw on. The default, and what every pre-005 row reads back as.
ACTIVE: Final = "active"

#: A fact that is kept and no longer retrieved (ticket 047).
#:
#: Nothing in memory is deleted. A procedure can be retired without being destroyed - that is
#: what ``active_procedures`` is for - and until ticket 047 a fact could not be. Since ticket
#: 041 put retrieval on the run path, a fact that has become false is not merely untidy: it is
#: carried into every future prompt at budget cost, forever.
ARCHIVED: Final = "archived"

_COLUMNS: Final = "mem_id, text, source, created_at, status"

_INSERT: Final = f"INSERT INTO mem_semantic ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)"
_INSERT_TAG: Final = "INSERT INTO mem_fact_tags (mem_id, tag) VALUES (?, ?)"
_INSERT_LINK: Final = "INSERT INTO mem_fact_links (mem_id, to_id) VALUES (?, ?)"
_INSERT_LINK_WHY: Final = (
    "INSERT INTO mem_fact_links (mem_id, to_id, relation, reason) VALUES (?, ?, 'contradicts', ?)"
)
#: By exact source, through ``mem_semantic_source_idx`` (migration 023): never full-text search.
_BY_SOURCE: Final = f"SELECT {_COLUMNS} FROM mem_semantic WHERE source = ? ORDER BY rowid"
_SELECT_TAGS: Final = "SELECT tag FROM mem_fact_tags WHERE mem_id = ? ORDER BY tag"
_SELECT_LINKS: Final = "SELECT to_id FROM mem_fact_links WHERE mem_id = ? ORDER BY to_id"
_SET_STATUS: Final = "UPDATE mem_semantic SET status = ? WHERE mem_id = ?"
_EXISTS: Final = "SELECT 1 FROM mem_semantic WHERE mem_id = ?"
_INDEX: Final = "INSERT INTO mem_semantic_fts (mem_id, text) VALUES (?, ?)"
_SELECT_ONE: Final = f"SELECT {_COLUMNS} FROM mem_semantic WHERE mem_id = ?"
# ``ORDER BY rank`` is FTS5's own bm25. It has been here since ticket 007 and ticket 048's first
# draft wrongly claimed otherwise; anything that changes this query must leave the ordering alone.
_SEARCH: Final = (
    "SELECT m.mem_id, m.text, m.source, m.created_at, m.status FROM mem_semantic_fts f "
    "JOIN mem_semantic m ON m.mem_id = f.mem_id "
    "WHERE mem_semantic_fts MATCH ?{status}{tag} ORDER BY rank LIMIT ?"
)

#: Appended to the search when archived facts are not wanted, which is the default. Filtered on
#: read rather than by removing the index row: ``mem_semantic_fts`` is a standalone FTS5 table
#: with no trigger keeping it in step, so deleting from one side is how the two drift apart.
_ONLY_ACTIVE: Final = f" AND m.status = '{ACTIVE}'"

#: Appended when a tag narrows the candidates. The join happens before the ceiling counts
#: anything, which is the whole point of having a filter.
_WITH_TAG: Final = " AND m.mem_id IN (SELECT mem_id FROM mem_fact_tags WHERE tag = ?)"

# Recency, not rank: this is not a search, and a tag is the whole question rather than something
# to weigh against a match score (the router's operator digest, decision 58). Ties break on
# `rowid`, never on the timestamp alone - two facts written in the same frozen second must still
# order consistently (ticket 066's lesson).
_RECENT_BY_TAG: Final = (
    "SELECT m.mem_id, m.text, m.source, m.created_at, m.status FROM mem_semantic m "
    "JOIN mem_fact_tags t ON t.mem_id = m.mem_id "
    f"WHERE t.tag = ? AND m.status = '{ACTIVE}' ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?"
)


@dataclass(frozen=True, slots=True)
class Fact:
    """One remembered fact. ``created_at`` is ISO-8601 UTC, from :mod:`harnessed.clock`.

    The first four fields keep their order and their meaning. Ticket 047 added three, all with
    defaults, for the same reason ``LoopResult``'s first three are positional: a new field must
    never displace an old one, and a caller that names none of them writes what it always wrote.
    """

    mem_id: str
    text: str
    source: str
    created_at: str
    status: str = ACTIVE
    tags: tuple[str, ...] = ()
    links: tuple[str, ...] = ()


def put(
    conn: sqlite3.Connection,
    text: str,
    source: str,
    *,
    tags: Sequence[str] = (),
    links: Sequence[str] = (),
) -> Fact:
    """Write one fact, its index entry, its tags and its links, redacted first (FR-13).

    Every caller-supplied string is scrubbed, ``source`` and each tag included: there is no field
    here that is exempt and no exemption for short strings. The row and its FTS entry are written
    from the scrubbed values, so a search can never surface what the store does not hold.

    **Every link is checked before anything is written.** A link naming a fact that was never
    written is a dangling wiki-link - harmless in a vault, a retrieval bug here, because expansion
    would follow it and find nothing, and nothing is indistinguishable from a fact that did not
    match. The whole write is one transaction, so a refused link leaves no half-written fact.
    """
    missing = [to_id for to_id in links if conn.execute(_EXISTS, (to_id,)).fetchone() is None]
    if missing:
        raise StorageError(
            f"cannot link to {', '.join(sorted(missing))}: no such fact. A link is stored by "
            f"mem_id at both ends, and retrieval follows it - a link to nothing reads as a "
            f"connection and behaves as an absence."
        )

    fact = Fact(
        mem_id=ids.new_id("mem"),
        text=redact.scrub(text),
        source=redact.scrub(source),
        created_at=clock.isoformat(clock.now()),
        status=ACTIVE,
        tags=tuple(dict.fromkeys(redact.scrub(tag) for tag in tags)),
        links=tuple(dict.fromkeys(links)),
    )
    db.write(
        conn,
        (
            (_INSERT, (fact.mem_id, fact.text, fact.source, fact.created_at, fact.status)),
            (_INDEX, (fact.mem_id, fact.text)),
            *((_INSERT_TAG, (fact.mem_id, tag)) for tag in fact.tags),
            *((_INSERT_LINK, (fact.mem_id, to_id)) for to_id in fact.links),
        ),
    )
    return fact


def put_replacing(
    conn: sqlite3.Connection,
    entries: Sequence[tuple[str, str | None]],
    source: str,
    *,
    tags: Sequence[str] = (),
    entry_tags: Sequence[Sequence[str]] | None = None,
    reason: str,
) -> tuple[Fact, ...]:
    """Write several facts in **one** transaction, archiving what each one contradicts (FR-151).

    ``entries`` is ``(text, replaces)`` pairs. A fact with ``replaces`` links to that fact with
    ``relation`` ``contradicts`` and ``reason``, and the old fact is archived in the same
    transaction: nothing is deleted. A ``replaces`` naming no fact is refused before anything is
    written, like :func:`put`'s link check. Every string is scrubbed as :func:`put` scrubs it.
    ``entry_tags``, when given, holds one tag sequence per entry, added to the shared ``tags``.
    """
    missing = [
        r for _, r in entries if r is not None and conn.execute(_EXISTS, (r,)).fetchone() is None
    ]
    if missing:
        raise StorageError(f"cannot replace {', '.join(sorted(missing))}: no such fact.")
    scrubbed_source = redact.scrub(source)
    scrubbed_tags = tuple(dict.fromkeys(redact.scrub(tag) for tag in tags))
    stamp = clock.isoformat(clock.now())
    facts: list[Fact] = []
    statements: list[tuple[str, tuple[object, ...]]] = []
    for number, (text, replaces) in enumerate(entries):
        own = () if entry_tags is None else tuple(redact.scrub(tag) for tag in entry_tags[number])
        fact = Fact(
            mem_id=ids.new_id("mem"),
            text=redact.scrub(text),
            source=scrubbed_source,
            created_at=stamp,
            status=ACTIVE,
            tags=tuple(dict.fromkeys((*scrubbed_tags, *own))),
            links=() if replaces is None else (replaces,),
        )
        facts.append(fact)
        statements.append((_INSERT, (fact.mem_id, fact.text, fact.source, fact.created_at, ACTIVE)))
        statements.append((_INDEX, (fact.mem_id, fact.text)))
        statements.extend((_INSERT_TAG, (fact.mem_id, tag)) for tag in fact.tags)
        if replaces is not None:
            statements.append((_INSERT_LINK_WHY, (fact.mem_id, replaces, reason)))
            statements.append((_SET_STATUS, (ARCHIVED, replaces)))
    db.write(conn, tuple(statements))
    return tuple(facts)


def by_source(conn: sqlite3.Connection, source: str) -> tuple[Fact, ...]:
    """Every fact written under exactly ``source``, oldest first, by the source index."""
    return tuple(_hydrate(conn, row) for row in conn.execute(_BY_SOURCE, (redact.scrub(source),)))


def _hydrate(conn: sqlite3.Connection, row: tuple[object, ...]) -> Fact:
    """One row plus its tags and its links. The only place a ``Fact`` is built from the store."""
    mem_id = str(row[0])
    return Fact(
        mem_id=mem_id,
        text=str(row[1]),
        source=str(row[2]),
        created_at=str(row[3]),
        status=str(row[4]),
        tags=tuple(str(r[0]) for r in conn.execute(_SELECT_TAGS, (mem_id,))),
        links=tuple(str(r[0]) for r in conn.execute(_SELECT_LINKS, (mem_id,))),
    )


def archive(conn: sqlite3.Connection, mem_id: str) -> Fact | None:
    """Retire one fact from retrieval without destroying it. ``None`` when there is no such fact.

    Idempotent, like every other write path here: archiving an already-archived fact reports the
    state it is already in rather than failing. The row and its index entry both stay - what
    changes is that :func:`search` stops offering it.
    """
    fact = get(conn, mem_id)
    if fact is None:
        return None
    if fact.status == ARCHIVED:
        return fact
    db.write(conn, ((_SET_STATUS, (ARCHIVED, mem_id)),))
    return replace(fact, status=ARCHIVED)


def get(conn: sqlite3.Connection, mem_id: str) -> Fact | None:
    """One fact by id, or ``None``. A miss is an answer, not an error.

    Returns an archived fact like any other: archiving hides a fact from *search*, and a caller
    that names an id has already decided which fact it wants.
    """
    row = conn.execute(_SELECT_ONE, (mem_id,)).fetchone()
    return None if row is None else _hydrate(conn, row)


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    *,
    tag: str | None = None,
    include_archived: bool = False,
) -> tuple[Fact, ...]:
    """Full-text search, best match first. Raises :class:`StorageError` on a malformed query.

    Archived facts are left out unless asked for, and ``tag`` narrows the candidates before the
    ranking runs - both in SQL, so a caller never receives rows it is about to discard.
    """
    sql = _SEARCH.format(
        status="" if include_archived else _ONLY_ACTIVE,
        tag="" if tag is None else _WITH_TAG,
    )
    parameters: tuple[object, ...] = (query, limit) if tag is None else (query, tag, limit)
    try:
        rows = conn.execute(sql, parameters).fetchall()
    except sqlite3.Error as exc:
        raise StorageError(f"semantic search for {query!r} failed: {exc}") from exc
    return tuple(_hydrate(conn, row) for row in rows)


#: One searchable term of a typed query: a run of word characters, Unicode (ticket 121).
#:
#: ``\w`` with :data:`re.UNICODE` rather than ``[a-z]`` because the operator's notes are not all
#: English, and a builder that matched ASCII letters would answer every non-Latin query with
#: "no searchable terms" while looking correct in a test written in English.
#:
#: Single characters are kept, unlike ``retrieval``'s ``\w{2,}``. This module is being *told*
#: what to look for; dropping a typed character would be inferring.
_TERM_RE: Final = re.compile(r"\w+", re.UNICODE)

#: How many terms of one typed query reach FTS5. A pasted paragraph is a real input, and an
#: unbounded ``OR`` chain is both slow and a way to hit SQLite's expression-depth limit - which
#: would be this ticket's defect again, wearing a different error message.
#:
#: Truncation is never silent: :func:`searched` returns a note saying how many terms were typed
#: and how many were used, because a panel may not show a result for a query it was not told was
#: cut short (rule D-1).
MAX_SEARCH_TERMS: Final = 20

#: The answer to text that holds nothing to search for. An empty list alone would be
#: indistinguishable from "no fact matched", which is a different fact about the store.
NO_TERMS_NOTE: Final = (
    "that query held no searchable term, so nothing was looked for. A term is a run of letters "
    "or digits; punctuation on its own matches nothing."
)


@dataclass(frozen=True, slots=True)
class Search:
    """One answer to one typed query: what matched, and anything the operator must be told.

    ``note`` is ``None`` for an ordinary query and is the whole reason this is a dataclass
    rather than a tuple of facts: an empty answer has two causes, and the two are not the same
    news. :func:`json_payload` omits the key when there is nothing to say, so a response that
    answered before ticket 121 is byte-identical after it.
    """

    facts: tuple[Fact, ...] = ()
    note: str | None = None


def quoted(term: str) -> str:
    """``term`` as an FTS5 phrase literal: wrapped in quotes, with any quote inside doubled.

    A phrase literal has no operators inside it, which is what makes an arbitrary string safe to
    put in a ``MATCH``. :data:`_TERM_RE` cannot hand this function a quote today; the doubling is
    here so that widening the term pattern cannot turn a typed quote into an expression.
    """
    inner = term.replace('"', '""')
    return f'"{inner}"'


def match_terms(query: str) -> tuple[str, ...]:
    """Every searchable term of ``query``, in the order typed, without repeats.

    Nothing is dropped for being common: ticket 048 decided that somebody who types ``the`` into
    a search box means notes containing ``the``, and ``retrieval._search_query`` - which drops
    stopwords because it is inferring a query from a task - is deliberately the other function.
    """
    return tuple(dict.fromkeys(_TERM_RE.findall(query)))


def match_query(query: str) -> str | None:
    """``query`` as an FTS5 ``MATCH`` expression, or ``None`` when it holds no term.

    Each term becomes its own phrase literal and the literals are joined with ``OR``. The result
    says what the operator typed and cannot be malformed: a ``?``, a hyphen, an unclosed quote or
    a bare ``NEAR/`` is text inside a phrase rather than syntax around one.
    """
    terms = match_terms(query)[:MAX_SEARCH_TERMS]
    return " OR ".join(quoted(term) for term in terms) if terms else None


def _note(query: str) -> str | None:
    """What the operator must be told about their own query, or ``None`` when nothing."""
    terms = match_terms(query)
    if not terms:
        return NO_TERMS_NOTE
    if len(terms) > MAX_SEARCH_TERMS:
        return (
            f"that query held {len(terms)} terms and the first {MAX_SEARCH_TERMS} were looked "
            f"for. The rest were not searched."
        )
    return None


def searched(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    *,
    tag: str | None = None,
    include_archived: bool = False,
) -> Search:
    """Search for text an operator typed, rather than for an FTS5 expression (ticket 121).

    This is what a search box calls. :func:`search` stays strict - it is the thin store wrapper
    and its argument is an expression - and building a valid expression out of typed text is the
    job this function does on both callers' behalf, so that neither the gateway route nor the CLI
    does it a second, slightly different way.
    """
    expression = match_query(query)
    facts = (
        ()
        if expression is None
        else search(conn, expression, limit, tag=tag, include_archived=include_archived)
    )
    return Search(facts=facts, note=_note(query))


def recent_by_tag(conn: sqlite3.Connection, tag: str, limit: int = 3) -> tuple[Fact, ...]:
    """The newest ``limit`` active facts carrying ``tag``, most recent first.

    Not :func:`search`: there is no query to rank against, only a tag to narrow by, so this reads
    by recency directly rather than building an FTS5 expression that matches nothing.
    """
    rows = conn.execute(_RECENT_BY_TAG, (tag, limit)).fetchall()
    return tuple(_hydrate(conn, row) for row in rows)


def json_payload(answer: Search) -> dict[str, object]:
    """``answer`` as the one dict both twins render (decision 19, MUST rule 16).

    ``{"facts": [...]}`` is exactly what ``harnessed memory search --json`` printed and what
    ``GET /v1/memory/facts`` served before this ticket. ``note`` is added only when there is
    something to say, so no answer that was well-formed before changes shape.
    """
    payload: dict[str, object] = {"facts": [asdict(fact) for fact in answer.facts]}
    if answer.note is not None:
        payload["note"] = answer.note
    return payload
