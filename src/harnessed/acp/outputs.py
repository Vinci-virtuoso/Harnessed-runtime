"""The delegation output ledger: the agent's text result and its output files (FR-127, G-28, G-29,
decision 65).

**The kind.** ``output_kind`` is one of :data:`KIND_TEXT`, :data:`KIND_UI`, :data:`KIND_IMAGE`,
:data:`KIND_DIAGRAM`, :data:`KIND_CODE`, :data:`KIND_RECORD`, or else the media type verbatim
(:func:`kind_for`). An entry's declared ``produces`` wins outright, except that an image media
type only becomes :data:`KIND_DIAGRAM` when ``produces`` names it - a bare ``produces`` of
anything else never turns an image into something a renderer cannot show as one. A Groq reply is
:data:`KIND_TEXT` by contract; that is stated here, never derived, because a Groq turn never
passes through this module.

**Where files come from.** :func:`record_hop` writes one hop's outputs in one transaction:
position 0 is always the delegation's own text result, and a file the entry's ``outputs`` glob
matched inside its working directory - present in ``after`` but not in ``before``, or changed
between them - fills position 1 and up, at most :data:`MAX_FILES`, sorted by name for a
deterministic cut. A file that resolves outside the working directory (a symlink escaping it) is
never stored, silently: it is not in the glob's honest answer to "what did this delegation write".
Each file's body is capped at :data:`OUTPUT_CAP_BYTES`; over the cap the row is written with
``too_large`` set and a NULL body rather than a truncated one.

**Writing, not opening.** :func:`record_hop` is the only place that reads a file off disk here;
:func:`body`, the read every request reaches, answers straight from the ``outputs`` table. No path
a request supplies ever reaches ``open()`` (decision 38's second named exception after the SSE
stream, ``fastapi.md``).
"""

from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from harnessed import clock
from harnessed.errors import SurfaceNotFound
from harnessed.memory import db, redact
from harnessed.serialize import canonical

__all__ = [
    "KIND_BY_MEDIA_TYPE",
    "KIND_CODE",
    "KIND_DIAGRAM",
    "KIND_GROQ_REPLY",
    "KIND_IMAGE",
    "KIND_RECORD",
    "KIND_TEXT",
    "KIND_UI",
    "MAX_FILES",
    "OUTPUT_CAP_BYTES",
    "Body",
    "Snapshot",
    "body",
    "kind_for",
    "latest_summary",
    "output_id",
    "record_hop",
    "snapshot",
]

#: The closed vocabulary FR-127 names. Anything else served is the media type verbatim.
KIND_TEXT: Final = "text"
KIND_UI: Final = "ui"
KIND_IMAGE: Final = "image"
KIND_DIAGRAM: Final = "diagram"
KIND_CODE: Final = "code"
KIND_RECORD: Final = "record"

#: A Groq reply's kind, by contract (decision 65). Stated as a constant because no Groq turn ever
#: reaches this module: there is no ``produces`` and no media type to derive it from.
KIND_GROQ_REPLY: Final = KIND_TEXT

#: The media types :func:`kind_for` names outright; ``image/*`` and the source types are matched
#: by rule, below.
KIND_BY_MEDIA_TYPE: Final[dict[str, str]] = {
    "text/plain": KIND_TEXT,
    "text/markdown": KIND_TEXT,
    "text/html": KIND_UI,
    "application/json": KIND_RECORD,
}

#: How many files one hop's ``outputs`` glob may contribute, beside position 0's text result.
MAX_FILES: Final = 8

#: The per-file serving cap. Over this, the row is ``too_large`` with a NULL body.
OUTPUT_CAP_BYTES: Final = 2 * 1024 * 1024

#: ``out_`` plus 16 hex of a sha256 over the fields that name a row (decision 65): reproducible,
#: never random.
_ID_PREFIX: Final = "out_"
_ID_HEX_CHARS: Final = 16

#: A media type stdlib ``mimetypes`` gives a source file, read as "code" (decision 65's "source
#: types"): the ``text/x-*`` family plus the one JavaScript spelling that falls outside it.
_CODE_PREFIX: Final = "text/x-"
_CODE_MEDIA_TYPES: Final = frozenset({"text/javascript"})

#: A media type whose bytes are decodable text, so its body is scrubbed on write rather than
#: stored as written (decision 65: "binary bodies are not scrubbed").
_TEXT_MEDIA_TYPES: Final = frozenset(
    {"text/plain", "text/markdown", "text/html", "application/json"}
)

_INSERT: Final = (
    "INSERT INTO outputs (output_id, run_id, message_seq, hop, position, task_hash, output_kind, "
    "media_type, name, caption, size_bytes, too_large, body, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SELECT_BODY: Final = "SELECT media_type, body, too_large FROM outputs WHERE output_id = ?"
_SELECT_LATEST_SUMMARY: Final = (
    "SELECT body FROM outputs WHERE run_id = ? AND position = 0 "
    "ORDER BY message_seq DESC, hop DESC LIMIT 1"
)


def kind_for(media_type: str, produces: str | None) -> str:
    """The ``output_kind`` one file, or the delegation's own text result, is served as.

    ``produces`` wins when declared, with one exception: an image media type stays
    :data:`KIND_IMAGE` unless ``produces`` names :data:`KIND_DIAGRAM` specifically - a
    ``produces`` of, say, ``"code"`` never turns a PNG into something ``CodeView`` cannot render.
    Absent a declared kind, a known media type answers by rule; anything else is the media type
    verbatim, which renders ``FileCard``.
    """
    if media_type.startswith("image/"):
        return KIND_DIAGRAM if produces == KIND_DIAGRAM else KIND_IMAGE
    if produces is not None:
        return produces
    if media_type in KIND_BY_MEDIA_TYPE:
        return KIND_BY_MEDIA_TYPE[media_type]
    if media_type in _CODE_MEDIA_TYPES or media_type.startswith(_CODE_PREFIX):
        return KIND_CODE
    return media_type


def output_id(run_id: str, message_seq: int | None, hop: int, position: int) -> str:
    """The reproducible id for one row: a hash over the fields that name it, never random."""
    digest = hashlib.sha256(
        canonical(
            {"run_id": run_id, "message_seq": message_seq, "hop": hop, "position": position}
        ).encode("utf-8")
    ).hexdigest()
    return f"{_ID_PREFIX}{digest[:_ID_HEX_CHARS]}"


#: One file's identity for the before/after diff: enough to catch a truncate-and-rewrite landing
#: on the same size, because mtime moves even when size does not (the same shape
#: ``acp/cli_runner.py::_snapshot`` already uses for its write jail).
Snapshot = Mapping[str, tuple[int, float]]


def latest_summary(conn: sqlite3.Connection, run_id: str) -> str | None:
    """The newest agent hop's own text result in ``run_id`` (position 0), or ``None`` when no agent
    hop has answered there (ticket 268): what a task's continuation shows the router, because the
    session's ``assistant`` messages hold Groq's synthesis and never the agent's own reply."""
    row = conn.execute(_SELECT_LATEST_SUMMARY, (run_id,)).fetchone()
    return None if row is None else bytes(row[0]).decode("utf-8", errors="replace")


def snapshot(cwd: Path) -> Snapshot:
    """Every regular file under ``cwd``, relative path to (size, mtime); ``{}`` if ``cwd`` is not
    a directory yet. Call once before a delegation starts and once after; :func:`record_hop`'s
    ``before`` is the first of that pair."""
    if not cwd.is_dir():
        return {}
    return {
        str(path.relative_to(cwd)): (path.stat().st_size, path.stat().st_mtime)
        for path in sorted(cwd.rglob("*"))
        if path.is_file()
    }


@dataclass(frozen=True, slots=True)
class Body:
    """What a stored output actually serves: bytes and the media type they carry."""

    content: bytes
    media_type: str


def body(conn: sqlite3.Connection, output_id_value: str) -> Body:
    """The stored bytes for ``output_id_value``, straight off the ``outputs`` table.

    Raises :class:`SurfaceNotFound` for an id the table never held, and separately for one held
    but over the serving cap (``too_large``, a NULL body) - each names which, so the two causes
    read differently on a 404 (AC4). Never opens a file: the bytes are the row.
    """
    row = conn.execute(_SELECT_BODY, (output_id_value,)).fetchone()
    if row is None:
        raise SurfaceNotFound(
            f"no output {output_id_value!r}: the ledger holds nothing under that id."
        )
    media_type, blob, too_large = row
    if too_large:
        raise SurfaceNotFound(
            f"the output {output_id_value!r} is over the serving cap and its body was not stored."
        )
    return Body(content=bytes(blob), media_type=str(media_type))


def _is_text(media_type: str) -> bool:
    return media_type in _TEXT_MEDIA_TYPES


def _scrubbed(raw: bytes, media_type: str) -> bytes:
    if not _is_text(media_type):
        return raw
    return redact.scrub(raw.decode("utf-8", errors="replace")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _Collected:
    name: str
    media_type: str
    kind: str
    size_bytes: int
    too_large: bool
    body: bytes | None


def _collect_files(
    cwd: Path, pattern: str, before: Snapshot, produces: str | None
) -> tuple[_Collected, ...]:
    """The entry's ``outputs`` glob, changed since ``before``, confined to ``cwd``, capped."""
    if not cwd.is_dir():
        return ()
    root = cwd.resolve()
    changed: list[tuple[str, Path]] = []
    for candidate in sorted(cwd.glob(pattern)):
        if not candidate.is_file():
            continue
        relative = str(candidate.relative_to(cwd))
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            continue  # a symlink escaping the working directory: never stored (AC3)
        stat = candidate.stat()
        if before.get(relative) == (stat.st_size, stat.st_mtime):
            continue  # unchanged during the delegation
        changed.append((relative, resolved))

    collected = []
    for relative, resolved in changed[:MAX_FILES]:
        raw = resolved.read_bytes()
        media_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        too_large = len(raw) > OUTPUT_CAP_BYTES
        collected.append(
            _Collected(
                name=relative,
                media_type=media_type,
                kind=kind_for(media_type, produces),
                size_bytes=len(raw),
                too_large=too_large,
                body=None if too_large else _scrubbed(raw, media_type),
            )
        )
    return tuple(collected)


def record_hop(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    message_seq: int | None,
    hop: int,
    task_hash: str | None,
    summary: str,
    cwd: Path,
    before: Snapshot,
    produces: str | None,
    outputs_glob: str | None,
) -> tuple[str, ...]:
    """Write one hop's outputs and return the ids written, position 0 first.

    Position 0 is always ``summary``, the delegation's own text result, kind :func:`kind_for`
    against ``"text/plain"``. When ``outputs_glob`` is declared, every file it matches under
    ``cwd`` that changed since ``before`` fills position 1 and up (:func:`_collect_files`). One
    transaction: a caller sees either the whole hop's outputs or none of them.
    """
    at = clock.isoformat(clock.now())
    text_media_type = "text/plain"
    text_body = _scrubbed(summary.encode("utf-8"), text_media_type)
    rows: list[tuple[str, tuple[object, ...]]] = [
        (
            _INSERT,
            (
                output_id(run_id, message_seq, hop, 0),
                run_id,
                message_seq,
                hop,
                0,
                task_hash,
                kind_for(text_media_type, produces),
                text_media_type,
                None,
                None,
                len(text_body),
                0,
                text_body,
                at,
            ),
        )
    ]
    ids = [output_id(run_id, message_seq, hop, 0)]

    files = () if outputs_glob is None else _collect_files(cwd, outputs_glob, before, produces)
    for position, collected in enumerate(files, start=1):
        row_id = output_id(run_id, message_seq, hop, position)
        ids.append(row_id)
        rows.append(
            (
                _INSERT,
                (
                    row_id,
                    run_id,
                    message_seq,
                    hop,
                    position,
                    task_hash,
                    collected.kind,
                    collected.media_type,
                    collected.name,
                    None,
                    collected.size_bytes,
                    int(collected.too_large),
                    collected.body,
                    at,
                ),
            )
        )

    db.write(conn, tuple(rows))
    return tuple(ids)
