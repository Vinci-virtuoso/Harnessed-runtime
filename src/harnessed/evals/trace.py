"""One run, joined back together out of the ledgers, as a file a person can open (ticket 045).

**A trace is not a log.** A log is what the harness chose to say about itself. A trace is the
request as the model actually received it, which makes it the one artefact that answers *why did
it do that* rather than *what happened*. Everything in it is already on disk - this module adds no
new recording, it joins three ledgers that were being written separately and had never been read
together.

The join is by ``run_id``, across ``turns`` and ``tool_results`` (the run journal, FR-7) and the
assembled retrieval the caller hands in. It lives in this package because the import table allows
``evals -> loop, memory, events`` and this is the only package that may reach all three: the CLI
renders rather than assembles, and ``memory/`` may not import ``loop``.

**Why retrieval is carried item by item rather than as its rendered text.** Since ticket 047 a
fact reaches the prompt by one of two routes - it matched the search, or something that matched
linked to it - and ticket 048 changed which facts match at all. A trace that flattens that into
one string answers what the model saw and not why it saw it, and the second question is the one a
trace exists for. ``retrieval.Item`` already carries ``kind`` and ``ref``; this does not throw
them away on the way to the file.

**Stored columns are carried as the strings they are, never re-parsed.** The journal serialised
the request and every tool's arguments on the way in; this module does not decode them coming
out. That is not laziness. ``providers/translate.py`` is the one module permitted to parse model
output, a source scan pins the decoding call to that file by name, and a second decoder here -
even one that only ever met text the harness itself had written - would make that scan unable to
tell a boundary from a convenience. (The scan is a plain text match, so this docstring must not
spell the call either: a first draft of this paragraph did, and tripped it.) A reader who wants
the request as an object decodes the string; the file stays a faithful copy of the ledger.

**Absence is reported, never rendered as emptiness.** A run from before the journal was wired has
no turns, and a trace that showed it as a run with no iterations would be describing a different
thing entirely. Such a trace says the journal holds nothing for that run and stops.

**Each iteration carries its ``usage`` and its ``seconds``, and computes neither** (FR-68,
decision 54). Both are projections of the same journal row this module already reads: the
three token counts come from ``loop/journal.py::turn_usage`` and the span from
``loop/runs.py::turn_spans``, which subtracts and rounds so the figure on the wire is the
store's. A turn from before migration 006 carries ``usage: null``, one from before 007 carries
``seconds: null``; neither is rendered as zero.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Final

from harnessed.loop import runs
from harnessed.loop.journal import turn_usage
from harnessed.memory import redact, retrieval

__all__ = ["FILE_SUFFIX", "TRACE_DIRNAME", "assemble", "path_for", "write"]

#: The directory a trace lands in, beside the eval result files that already live there.
TRACE_DIRNAME: Final = "evals"

#: What names a trace file. The run id is the whole of the name, so a trace and the run it traces
#: cannot be told apart by mistake.
FILE_SUFFIX: Final = ".trace.jsonl"

_TURNS: Final = (
    "SELECT iteration, request, response, stop_reason, created_at "
    "FROM turns WHERE run_id = ? ORDER BY iteration"
)

_RESULTS: Final = (
    "SELECT iteration, call_id, tool_name, arguments, output, ok, parse_error "
    "FROM tool_results WHERE run_id = ? ORDER BY iteration, rowid"
)


def path_for(home: Path, run_id: str) -> Path:
    """Where one run's trace goes. Resolved from ``Settings.home``, never from a literal."""
    return home / TRACE_DIRNAME / f"{run_id}{FILE_SUFFIX}"


def assemble(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    assembled: retrieval.Assembled | None = None,
) -> tuple[dict[str, Any], ...]:
    """One object per iteration: what was sent, what came back, and what the tools did.

    ``assembled`` is the run's retrieval, handed in rather than re-derived. Re-running assembly
    to describe a finished run would report what retrieval *would* do now, against a store that
    has changed since - which is a different claim and a misleading one.

    Returns an empty tuple when the journal holds nothing for ``run_id``. The caller decides what
    to say about that; this does not invent an iteration to have something to show.
    """
    turns = conn.execute(_TURNS, (run_id,)).fetchall()
    if not turns:
        return ()

    by_iteration: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute(_RESULTS, (run_id,)):
        by_iteration.setdefault(int(row[0]), []).append(
            {
                "call_id": str(row[1]),
                "tool": str(row[2]),
                "arguments": str(row[3]),
                "output": str(row[4]),
                "ok": bool(row[5]),
                "arguments_unparseable": str(row[6]) if row[6] else None,
            }
        )

    retrieved = _retrieval_block(assembled)
    usage = turn_usage(conn, run_id)
    seconds = runs.turn_spans(conn, run_id)
    objects: list[dict[str, Any]] = []
    for row in turns:
        iteration = int(row[0])
        objects.append(
            {
                "run_id": run_id,
                "iteration": iteration,
                "at": str(row[4]),
                # Only the first iteration carries the retrieval: it is assembled once per run
                # (FR-11), and repeating it on every turn would read as though it had not been.
                "retrieval": retrieved if iteration == turns[0][0] else None,
                "request": str(row[1]),
                "response": {"content": str(row[2]), "stop_reason": str(row[3])},
                "tool_results": by_iteration.get(iteration, []),
                "usage": runs.usage_payload(usage.get(iteration)),
                "seconds": seconds.get(iteration),
            }
        )
    return tuple(objects)


def _retrieval_block(assembled: retrieval.Assembled | None) -> dict[str, Any] | None:
    """What the run was given, and how each piece of it got there."""
    if assembled is None:
        return None
    return {
        "budget": assembled.budget,
        "tokens": assembled.tokens,
        "truncated": assembled.truncated,
        "items": [
            {"kind": item.kind, "ref": item.ref, "text": item.text} for item in assembled.included
        ],
        "dropped": [{"kind": item.kind, "ref": item.ref} for item in assembled.dropped],
    }


def write(
    conn: sqlite3.Connection,
    home: Path,
    run_id: str,
    *,
    assembled: retrieval.Assembled | None = None,
) -> Path | None:
    """Write one run's trace, scrubbed, and return where it went. ``None`` when there is nothing.

    **Scrubbed at the boundary rather than trusted.** A trace carries the full request, which is
    the widest surface in this repository for a credential to reach a file: the system prompt, the
    retrieved context, and every tool argument, all in one place. The ledgers it joins were each
    scrubbed on the way in (FR-13), so this is belt and braces - and belt and braces is the right
    posture for the one artefact an operator is most likely to paste into a review.
    """
    objects = assemble(conn, run_id, assembled=assembled)
    if not objects:
        return None

    destination = path_for(home, run_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(redact.scrub(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for obj in objects
    ]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination
