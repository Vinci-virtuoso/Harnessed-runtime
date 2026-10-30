"""A search box never hands raw text to FTS5 (ticket 121, FR-58).

``semantic.search`` is a thin store wrapper: it puts its argument straight into ``MATCH`` and
documents that it raises :class:`StorageError` on a malformed query. Building a valid FTS5
expression is the caller's job, and until this ticket neither caller did it - so ``who is Vinci?``
typed into the dashboard's search box was a 500.

This file owns the builder's proof and the corpus of hostile inputs the other two halves reuse:
``test_gateway_memory.py`` sends every one of them at the route, ``test_cli_memory.py`` types
every one of them at the CLI and compares the two answers byte for byte.

The corpus is generated rather than listed, because the defect was found by an operator typing an
ordinary question and not by any of the well-formed fixtures the suite already held. Punctuation
is crossed with four positions, FTS5's own operator vocabulary is included verbatim, and the
non-ASCII rows are here because the operator's notes are not all English.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessed.errors import StorageError
from harnessed.memory import db, retrieval, semantic

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "121"

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")

#: The two facts ticket 121 measured its table against. Both hold ``the``, ``vinci`` and
#: ``harnessed``, which is what makes the "2 rows" column reproducible here rather than a number
#: copied out of a ticket.
CORPUS: tuple[str, ...] = (
    "Vinci built the harnessed loop",
    "the vinci-harnessed dashboard is read-only",
)

#: The five inputs measured against real FTS5 on 2026-09-19, with the row count per-term quoting
#: returns over :data:`CORPUS`. ``None`` means the query held no searchable term at all, which is
#: a different answer from "no fact matched" and is reported as one.
MEASURED: tuple[tuple[str, int | None], ...] = (
    ("who is Vinci?", 2),
    ("vinci-harnessed", 2),
    ("the", 2),
    ('"unclosed', 0),
    ("???", None),
)

#: Every character an operator's keyboard puts next to a word. Each is tried alone, leading,
#: trailing and infixed, which is the position the raw query broke in each recorded report.
PUNCTUATION = "?!.,;:'\"`~@#$%^&*()[]{}<>/\\|-_=+"

#: FTS5's own expression vocabulary, typed as text rather than meant as syntax. A person asking
#: "what does NEAR do" means those four letters, not the operator.
FTS5_OPERATORS: tuple[str, ...] = (
    "AND",
    "OR",
    "NOT",
    "NEAR",
    "NEAR/",
    "NEAR/2",
    "a NEAR/ b",
    "NEAR(vinci harnessed, 2)",
    "vinci AND",
    "AND vinci",
    "text:vinci",
    "vinci*",
    "^vinci",
    "(vinci",
    "vinci)",
    '"vinci',
    'vinci"',
    '""',
    "{vinci}",
    "-vinci",
    "vinci - harnessed",
)

#: Not English, and not Latin script. A tokenizer rule written as ``[a-z]`` passes every test
#: above and drops every word here.
NON_ASCII: tuple[str, ...] = (
    "café",
    "naïve",
    "Ωmega",
    "日本語",
    "Привет",
    "vinci 🙂",
    "🙂",
    "ß",
)

#: Text that holds nothing to search for. Each must answer, not fault, and must say why.
TERMLESS: tuple[str, ...] = ("", " ", "\t\n", "???", "!!!", "---", "...", '"', "()")


def _punctuation_inputs() -> tuple[str, ...]:
    """Each punctuation mark in the four positions a typed query puts it in."""
    return tuple(
        form
        for mark in PUNCTUATION
        for form in (mark, f"{mark}vinci", f"vinci{mark}", f"vinci{mark}harnessed")
    )


#: The whole corpus of operator text, deduplicated with its order kept so the evidence file reads
#: in the order the groups are declared above.
HOSTILE_INPUTS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *(text for text, _ in MEASURED),
            *_punctuation_inputs(),
            *FTS5_OPERATORS,
            *NON_ASCII,
            *TERMLESS,
            "who is Vinci? and what did they build - anything?",
            "word " * 50,
        )
    )
)


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated database holding :data:`CORPUS` and nothing else."""
    connection = db.connect()
    for text in CORPUS:
        semantic.put(connection, text, "ticket 121")
    yield connection
    connection.close()


# ---- AC 1: no operator text faults ----------------------------------------------------------


def test_no_typed_text_produces_a_storage_error(conn: sqlite3.Connection) -> None:
    """The property, over the whole corpus: every input answers, none raises.

    Raw text is the control. The same corpus through ``semantic.search`` must fault on a good
    number of these, or this test is asserting nothing (MUST rule 12).
    """
    lines: list[str] = ["ticket 121 - every typed input, its FTS5 expression and its answer\n"]
    faults = 0
    for text in HOSTILE_INPUTS:
        answer = semantic.searched(conn, text)
        expression = semantic.match_query(text)
        try:
            semantic.search(conn, text)
        except StorageError:
            faults += 1
            raw = "StorageError"
        else:
            raw = "ok"
        lines.append(
            f"{text!r:44} raw={raw:13} match={expression!r:56} "
            f"facts={len(answer.facts)} note={answer.note is not None}"
        )
    assert faults >= 20, f"only {faults} of {len(HOSTILE_INPUTS)} inputs fault raw"
    capture("fts-inputs.txt", "\n".join(lines) + "\n")


def test_the_five_measured_inputs_answer_as_the_ticket_measured(conn: sqlite3.Connection) -> None:
    """The ticket's table, reproduced against real FTS5 rather than quoted."""
    for text, expected in MEASURED:
        answer = semantic.searched(conn, text)
        if expected is None:
            assert answer.facts == (), text
            assert answer.note == semantic.NO_TERMS_NOTE, text
        else:
            assert len(answer.facts) == expected, (text, answer)
            assert answer.note is None, text


def test_a_term_is_word_characters_in_any_script(conn: sqlite3.Connection) -> None:
    """Non-ASCII survives the builder: matching on ``[a-z]`` would drop every word here."""
    for text in NON_ASCII:
        expression = semantic.match_query(text)
        if any(character.isalnum() for character in text):
            assert expression is not None, text
            for word in text.split():
                if word.isalnum():
                    assert f'"{word}"' in expression, (text, expression)
        else:
            assert expression is None, text


def test_a_quote_inside_a_term_is_doubled_not_left_to_close_the_phrase() -> None:
    """Every term is one phrase literal, so a quote in one may never end it."""
    assert semantic.quoted('say "hi"') == '"say ""hi"""'
    assert semantic.match_query('say "hi"') == '"say" OR "hi"'


# ---- AC 2: a stopword still counts ----------------------------------------------------------


def test_a_stopword_still_counts_where_retrieval_would_drop_it(
    tmp_harnessed_home: Path,
) -> None:
    """048's decision, pinned: the search box is told what to look for, retrieval infers it.

    ``who is Vinci?`` is one string that the two must answer differently. Retrieval drops ``who``
    and ``is`` as stopwords and finds only the fact naming Vinci; the search box keeps them and
    also finds the fact naming who reviews. A future edit that reused ``_search_query`` here
    would make this test fail, which is the point of writing it.
    """
    conn = db.connect()
    about_vinci = semantic.put(conn, "Vinci built the harnessed loop", "ticket 121")
    about_who = semantic.put(conn, "the operator decides who reviews a promotion", "ticket 121")

    typed = "who is Vinci?"
    inferred = retrieval._search_query(typed)
    assert inferred == "vinci", inferred
    assert semantic.match_query(typed) == '"who" OR "is" OR "Vinci"'

    box = {fact.mem_id for fact in semantic.searched(conn, typed).facts}
    run = {fact.mem_id for fact in semantic.search(conn, inferred)}
    assert box == {about_vinci.mem_id, about_who.mem_id}
    assert run == {about_vinci.mem_id}
    assert box != run, "the search box and retrieval are deliberately different"
    conn.close()


def test_every_typed_word_reaches_the_index(conn: sqlite3.Connection) -> None:
    """Nothing is silently dropped: each stopword of the AC is its own term."""
    expression = semantic.match_query("who is the one")
    assert expression == '"who" OR "is" OR "the" OR "one"'
    assert len(semantic.searched(conn, "the").facts) == 2


# ---- AC 3: no searchable term is an answer, not a fault and not a bare empty list -------------


def test_text_with_no_searchable_term_says_so(conn: sqlite3.Connection) -> None:
    for text in TERMLESS:
        answer = semantic.searched(conn, text)
        assert answer.facts == (), text
        assert answer.note == semantic.NO_TERMS_NOTE, text
        assert "no searchable term" in answer.note


def test_no_facts_matching_is_told_apart_from_nothing_to_search_for(
    conn: sqlite3.Connection,
) -> None:
    """The distinction the note exists for: both answers are empty, only one carries a note."""
    missed = semantic.searched(conn, "unhelddictionaryword")
    assert missed.facts == ()
    assert missed.note is None
    assert semantic.searched(conn, "???").note is not None


# ---- The cap: a pasted paragraph is bounded, and the answer says so (rule D-1) ----------------


def test_a_paste_longer_than_the_cap_is_told_it_was_capped(conn: sqlite3.Connection) -> None:
    """Truncating quietly would be the browser being told one thing and shown another."""
    pasted = " ".join(f"word{index}" for index in range(semantic.MAX_SEARCH_TERMS + 5))
    expression = semantic.match_query(pasted)
    assert expression is not None
    assert expression.count(" OR ") == semantic.MAX_SEARCH_TERMS - 1
    answer = semantic.searched(conn, pasted)
    assert answer.note is not None
    assert str(semantic.MAX_SEARCH_TERMS) in answer.note
    assert str(semantic.MAX_SEARCH_TERMS + 5) in answer.note


def test_a_query_at_the_cap_is_not_told_anything(conn: sqlite3.Connection) -> None:
    exact = " ".join(f"word{index}" for index in range(semantic.MAX_SEARCH_TERMS))
    assert semantic.searched(conn, exact).note is None


def test_a_repeated_word_counts_once(conn: sqlite3.Connection) -> None:
    """Duplicates are collapsed, so a paste of one word repeated is not a capped query."""
    assert semantic.match_query("the the the") == '"the"'
    assert semantic.searched(conn, " ".join(["the"] * 100)).note is None


# ---- AC 6: the store stays strict ------------------------------------------------------------


def test_search_still_raises_on_a_malformed_query(conn: sqlite3.Connection) -> None:
    """The store's contract is unchanged. Its strictness is what made this visible at all."""
    with pytest.raises(StorageError) as raised:
        semantic.search(conn, "who is Vinci?")
    assert "semantic search for" in str(raised.value)
    with pytest.raises(StorageError):
        semantic.search(conn, "vinci-harnessed")


def test_the_builder_takes_the_facts_the_store_holds(conn: sqlite3.Connection) -> None:
    """``searched`` is ``search`` over a built expression, filters and limit included."""
    assert semantic.searched(conn, "vinci", limit=1).facts != ()
    assert len(semantic.searched(conn, "vinci", limit=1).facts) == 1
    archived = semantic.search(conn, semantic.match_query("vinci") or "", limit=1)[0]
    semantic.archive(conn, archived.mem_id)
    active = semantic.searched(conn, "vinci").facts
    assert archived.mem_id not in {fact.mem_id for fact in active}
    included = semantic.searched(conn, "vinci", include_archived=True).facts
    assert archived.mem_id in {fact.mem_id for fact in included}


# ---- The payload both twins render -----------------------------------------------------------


def test_the_payload_is_the_collection_key_and_a_note_only_when_there_is_one(
    conn: sqlite3.Connection,
) -> None:
    """``{"facts": [...]}`` is unchanged for an ordinary query; the note is an addition, not a
    re-shaping (MUST rule 16)."""
    answer = semantic.searched(conn, "vinci")
    ordinary = semantic.json_payload(answer)
    assert set(ordinary) == {"facts"}
    rows = ordinary["facts"]
    assert isinstance(rows, list)
    assert [row["mem_id"] for row in rows] == [fact.mem_id for fact in answer.facts]
    for row, fact in zip(rows, answer.facts, strict=True):
        assert set(row) == {field.name for field in dataclasses.fields(semantic.Fact)}
        assert row["text"] == fact.text
        assert row["tags"] == fact.tags
    termless = semantic.json_payload(semantic.searched(conn, "???"))
    assert set(termless) == {"facts", "note"}
    assert termless["facts"] == []
    assert termless["note"] == semantic.NO_TERMS_NOTE
