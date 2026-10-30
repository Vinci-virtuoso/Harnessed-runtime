"""What a candidate is: append-only text, content-addressed, written once (FR-17).

A candidate is the only thing the flywheel is allowed to change about the agent, and it may
only **append text to the system prompt**. Never code, never a tool schema, never a policy
field (locked decision 5). Two mechanisms carry that, and both live here rather than at a call
site:

* **Application is structural.** :func:`apply` rebuilds the request with
  ``dataclasses.replace(request, system=...)`` and names no other field, so "only the system
  prompt changed" is something a reader counts in the file rather than something a comment
  claims. The envelope itself comes from ``loop/prompt.py`` as an injected builder: this
  package may not reach ``loop/``, and injection is the standing answer to an import-table wall
  here (the sandbox root in ``tools/``, the call serializer in ``loop/journal.py``).
* **Content is refused at write time.** :func:`validate` scans the normalized text against a
  small table of shapes that are not prose - object syntax, a schema keyword, a code fence, a
  policy field - and raises :class:`InvalidCandidateError` before anything reaches the store.
  A rejection names what it hit, because "invalid candidate" alone is not diagnosable.

**The id is the content.** ``cand_`` plus the first 16 hex characters of a sha256 over the
canonical JSON ``{"text": ..., "cases": [...]}``. The improvement run id is deliberately not in
the hash - it is a column for provenance, not a part of identity - which is what makes a
resumed improvement run mint the same id and ``harnessed improve promote`` idempotent.

The text is scrubbed **before** it is normalized and hashed, never after: two different leaked
credentials of the same kind scrub to one identical string, and hashing first would file them
as two candidates that read back as one (ticket 008).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

from harnessed import clock, ids
from harnessed.errors import InvalidCandidateError
from harnessed.memory import db, redact
from harnessed.providers.types import ChatRequest

__all__ = [
    "CANDIDATE_CREATED",
    "Build",
    "Candidate",
    "Publish",
    "apply",
    "get",
    "normalize",
    "procedure_id",
    "candidate_id",
    "validate",
    "write",
]

#: Published once per candidate that actually reached the store.
CANDIDATE_CREATED: Final = "improve.candidate.created"

_COLUMNS: Final = "cand_id, text, source_case_ids, provenance, created_at"

# The conflict clause is what makes a repeated write a no-op rather than a second row: same
# text and same sources, same id, first write stands. It is not an upsert - a candidate is
# never overwritten, so the provenance recorded is the run that first produced it.
_INSERT: Final = (
    f"INSERT INTO candidates ({_COLUMNS}) VALUES (?, ?, ?, ?, ?) "
    f"ON CONFLICT (cand_id) DO NOTHING"
)
_SELECT_ONE: Final = f"SELECT {_COLUMNS} FROM candidates WHERE cand_id = ?"


#: How an event reaches the bus. Injected so this module never holds one, which is what lets a
#: test read the published names off a spy with nothing patched.
Publish = Callable[[str, Mapping[str, object]], object]


class Build(Protocol):
    """How procedure text becomes a system prompt: ``loop.prompt.build_system_prompt``.

    Restated here as a one-method Protocol rather than imported, exactly as ``loop/engine.py``
    and ``memory/gate.py`` restate ``Provider``. ``improve/`` may reach ``evals``, ``memory``,
    ``providers.types`` and ``events`` and nothing else, and widening that table to borrow one
    function would cost more than declaring its shape.
    """

    def __call__(self, base: str, procedures: Iterable[str] = ()) -> str: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    """One candidate procedure: the text, where it came from, and the id that is its content."""

    cand_id: str
    text: str
    source_case_ids: tuple[str, ...]
    provenance: str
    created_at: str


# ---- normalization -------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Unicode NFC, stripped, newlines normalized, every run of whitespace one space.

    Normalization is what makes the id a function of what the procedure *says* rather than of
    how the model happened to wrap it, so two reflections that differ only in line breaks are
    one candidate. It is idempotent by construction: ``str.split`` with no argument splits on
    every whitespace run, including the newlines, so the second pass has nothing left to do.
    """
    return unicodedata.normalize("NFC", " ".join(text.split()))


# ---- the append-only rule ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Rule:
    """One shape a candidate may not carry, and the words the rejection explains it with."""

    kind: str
    why: str
    pattern: re.Pattern[str]


#: The whole append-only rule, as a table. A candidate is prose appended to a system prompt;
#: anything below is a different kind of thing wearing prose's clothes. Ordered narrowest
#: intent first so a rejection names the most specific reason it could (ticket 008's lesson
#: about ordering a pattern table), and every entry carries its own kind so the message is
#: diagnosable rather than a bare "invalid candidate".
_RULES: Final[tuple[_Rule, ...]] = (
    _Rule(
        kind="a tool schema",
        why="a schema keyword",
        pattern=re.compile(
            r"\b(?:tool[ _-]?schema|json[ _-]?schema|input[ _-]?schema|"
            r"additionalproperties|register[ _-]?tool|parameters\s*[:=])",
            re.IGNORECASE,
        ),
    ),
    _Rule(
        kind="a tool schema",
        why="a quoted field name followed by a colon, which is object syntax and not prose",
        pattern=re.compile(r"\"[A-Za-z_][A-Za-z0-9_-]*\"\s*:"),
    ),
    _Rule(
        kind="a tool schema",
        why="a brace, which starts an object literal rather than a sentence",
        pattern=re.compile(r"[{}]"),
    ),
    _Rule(
        kind="executable code",
        why="a fenced code block",
        pattern=re.compile(r"`{3}|~{3}"),
    ),
    _Rule(
        kind="executable code",
        why="a function or class definition",
        pattern=re.compile(
            r"\bdef\s+\w+\s*\(|\bclass\s+\w+\s*[:(]|\bfunction\s+\w+\s*\(|=>|\blambda\b"
        ),
    ),
    _Rule(
        kind="executable code",
        why="an import statement",
        pattern=re.compile(r"\bimport\s+[A-Za-z_]|\bfrom\s+[\w.]+\s+import\b"),
    ),
    _Rule(
        kind="executable code",
        why="a call into the shell or the interpreter",
        pattern=re.compile(
            r"\bsubprocess\b|\bos\.system\b|\b(?:exec|eval|compile)\s*\(|#!/|<script",
            re.IGNORECASE,
        ),
    ),
    _Rule(
        kind="a policy field",
        why="a policy instruction",
        pattern=re.compile(r"\bpolic(?:y|ies)\b", re.IGNORECASE),
    ),
    _Rule(
        kind="a policy field",
        why="a setting the operator owns",
        pattern=re.compile(
            r"\b(?:allowlist|denylist|sandbox|max_iterations|max_tokens|token_budget|"
            r"stop_sequences)\b",
            re.IGNORECASE,
        ),
    ),
)


def validate(text: str, source_case_ids: Sequence[str]) -> None:
    """Raise :class:`InvalidCandidateError` unless ``text`` is append-only prose about failures.

    Called on the normalized, already-scrubbed text, so a rule reasons about one canonical form
    and a candidate cannot smuggle a shape past the table by wrapping it in whitespace.
    """
    if not text:
        raise InvalidCandidateError(
            "a candidate needs procedure text. Empty text appends nothing to the system "
            "prompt, so there would be nothing to evaluate and nothing to promote."
        )
    if not source_case_ids:
        raise InvalidCandidateError(
            "a candidate records the case ids it was written in response to, and this one "
            "names none. A candidate with no observed failure behind it is not gated on "
            "anything (FR-17)."
        )
    for rule in _RULES:
        if rule.pattern.search(text):
            raise InvalidCandidateError(
                f"a candidate may only append text to the system prompt, and this one carries "
                f"{rule.kind}: it contains {rule.why}. Never code, never a tool schema, never "
                f"a policy field (locked decision 5)."
            )


# ---- the two content-addressed ids ---------------------------------------------------------


def candidate_id(text: str, source_case_ids: Sequence[str]) -> str:
    """``cand_`` plus 16 hex of sha256 over ``{"text": ..., "cases": [...]}``.

    ``text`` and ``source_case_ids`` arrive already normalized and already sorted; the caller
    that scrubbed the text is the caller that hashes it, so there is one canonical form.
    """
    return ids.content_hash("cand", {"text": text, "cases": list(source_case_ids)})


def procedure_id(text: str) -> str:
    """``proc_`` plus 16 hex of sha256 over the normalized text alone.

    The same scheme with a different prefix and a narrower payload: a procedure is text, and
    the cases that motivated it belong to the candidate that proposed it, not to the procedure
    itself.
    """
    return ids.content_hash("proc", normalize(text))


def sources(source_case_ids: Iterable[str]) -> tuple[str, ...]:
    """The case ids in canonical form: deduplicated and sorted.

    Sorted because the hash is taken over them, so the order a caller happened to collect
    failures in must not mint a second id for identical content. Deduplicated for the same
    reason: one failure named twice is one source.
    """
    return tuple(sorted({case_id for case_id in source_case_ids if case_id}))


# ---- the store -----------------------------------------------------------------------------


def write(
    conn: sqlite3.Connection,
    text: str,
    source_case_ids: Iterable[str],
    *,
    provenance: str = "",
    publish: Publish | None = None,
) -> Candidate:
    """Scrub, normalize, validate, hash, insert - in that order - and publish once.

    Returns the candidate already stored under the same content hash when there is one, so a
    resumed improvement run re-issuing this call writes nothing and reports the recorded row.
    """
    text = normalize(redact.scrub(text))
    cases = sources(source_case_ids)
    validate(text, cases)

    cand_id = candidate_id(text, cases)
    existing = get(conn, cand_id)
    if existing is not None:
        return existing

    written = Candidate(
        cand_id=cand_id,
        text=text,
        source_case_ids=cases,
        provenance=redact.scrub(provenance),
        created_at=clock.isoformat(clock.now()),
    )
    db.write(
        conn,
        (
            (
                _INSERT,
                (
                    written.cand_id,
                    written.text,
                    _dumps(list(written.source_case_ids)),
                    written.provenance,
                    written.created_at,
                ),
            ),
        ),
    )
    if publish is not None:
        publish(
            CANDIDATE_CREATED,
            {
                "cand_id": written.cand_id,
                "source_case_ids": list(written.source_case_ids),
                "provenance": written.provenance,
                "text": written.text,
            },
        )
    return written


def get(conn: sqlite3.Connection, cand_id: str) -> Candidate | None:
    """One candidate by id, or ``None``. A miss is an answer, not an error."""
    row = conn.execute(_SELECT_ONE, (cand_id,)).fetchone()
    if row is None:
        return None
    stored_id, text, raw_cases, provenance, created_at = row
    return Candidate(
        cand_id=stored_id,
        text=text,
        source_case_ids=tuple(_decode_cases(raw_cases)),
        provenance=provenance,
        created_at=created_at,
    )


def _dumps(payload: object) -> str:
    """Canonical JSON, the same form :func:`candidate_id` hashes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _decode_cases(raw: str) -> list[str]:
    """The stored case-id array, back as a list.

    Decoded through an explicit decoder rather than the module-level shorthand: that shorthand
    is quarantined to ``providers/translate.py``, the one module permitted to parse model
    output, and a package-wide scan enforces it. A column this module wrote is not model
    output, but the scan reads text and not intent (ticket 017).
    """
    decoded = json.JSONDecoder().decode(raw)
    return [str(entry) for entry in decoded]


# ---- application ---------------------------------------------------------------------------


def apply(request: ChatRequest, candidate: Candidate, *, build: Build) -> ChatRequest:
    """The candidate appended to ``request``'s system prompt, and nothing else touched.

    One ``replace`` naming one field. That is the whole of FR-17's first consequence: there is
    no path through this function that reaches ``tools``, ``max_tokens``, ``temperature`` or
    ``metadata``, so diffing the effective run configuration before and after can only ever
    show ``system``.
    """
    return replace(request, system=build(request.system, (candidate.text,)))
