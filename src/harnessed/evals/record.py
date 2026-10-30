"""Record a work suite from a real session, and set the operator's verdict on each case.

FR-136 (G-54, ``V6-3``), decision 83. A work suite is ``corpus/suites/<suite>.yaml`` with
``kind: work``: one case per message a session served, the operator's own words as ``input``,
the reply as served as ``reference``, and a ``verdict`` the operator sets afterwards.

**Recording only ever appends.** A case already in the suite - found by the ``(session, seq)`` it
was recorded from - is never touched: its id, its verdict and its fixtures stay byte for byte
what they were, and a new case takes the next number after the highest one the suite holds, so an
id is authored once and never renumbered (``suite.py``'s rule).

**Everything written is scrubbed first** (``memory/redact.py``, MUST rule 7): the input, the
reference, the slug that names the case and every byte of the fixture reach the disk only through
:func:`~harnessed.memory.redact.scrub`.

A Groq-route case also gets its FakeProvider fixture under ``<fixtures>/<case id>/``, one
``base`` response per recorded turn of that message, so ``eval run`` can replay the case offline.
An agent-route case has none: its reference is the synthesis write-up and nothing replays an agent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# See suite.py: no stub package is pinned, so the untyped import is acknowledged here.
import yaml  # type: ignore[import-untyped]

from harnessed.errors import SuiteLoadError
from harnessed.evals import suite as suites
from harnessed.loop import messages
from harnessed.memory import redact

__all__ = [
    "RecordResult",
    "RemoveResult",
    "corpus_roots",
    "pending_cases",
    "pending_payload",
    "record",
    "record_payload",
    "remove",
    "remove_payload",
    "set_verdict",
]

#: Tool-call arguments are stored as canonical JSON text by ``loop/journal.py``; decoded with the
#: decoder object, not the module-level loader, because ``test_providers`` reserves that spelling
#: for the modules that parse a chat wire format, and this reads our own ledger row.
_LEDGER_JSON: Final = json.JSONDecoder()

#: Suites that ship with the package: their cases are the operator's and ``--remove`` leaves them.
SHIPPED_SUITES: Final = frozenset({"smoke", "holdout", "memory", "work-email"})

_GROQ: Final = "groq"
_CHAT_ROUTE: Final = "groq_chat"
_SLUG_WORDS: Final = 5
_SLUG_LENGTH: Final = 40
_NUMBER: Final = re.compile(r"^.+?-(\d{3,})(?:-|$)")

_SELECT_ROUTE: Final = (
    "SELECT route FROM decisions WHERE run_id = ? AND message_seq = ? AND route IS NOT NULL "
    "ORDER BY hop LIMIT 1"
)
_SELECT_TURNS: Final = (
    "SELECT iteration, response, stop_reason, input_tokens, output_tokens, model FROM turns "
    "WHERE run_id = ? AND message_seq = ? ORDER BY iteration"
)
_SELECT_TOOL_CALLS: Final = (
    "SELECT call_id, tool_name, arguments FROM tool_results WHERE run_id = ? AND iteration = ? "
    "ORDER BY rowid"
)


@dataclass(frozen=True, slots=True)
class RecordResult:
    """What one recording did: the cases it appended and the ones it found already there."""

    path: Path
    added: tuple[str, ...]
    kept: tuple[str, ...]


# ---- where a recording lands ---------------------------------------------------------------


def corpus_roots(to: Path) -> tuple[Path, Path]:
    """The suites and fixtures directories of the corpus laid out under ``to`` (``--to``).

    ``to`` is a corpus directory (``<to>/suites``, ``<to>/wire/fake``), which is how a checkout's
    ``src/harnessed/corpus`` receives a case. A directory that cannot be written is a
    :class:`~harnessed.errors.SuiteLoadError` naming it, raised before anything is touched.
    """
    roots = (to / "suites", to / "wire" / "fake")
    try:
        for each in roots:
            each.mkdir(parents=True, exist_ok=True)
            if not os.access(each, os.W_OK):
                raise PermissionError(each)
    except OSError as exc:
        raise SuiteLoadError(f"cannot write to {to}: {exc.strerror or exc}") from exc
    return roots


# ---- recording -----------------------------------------------------------------------------


def record(
    conn: sqlite3.Connection,
    suite: str,
    session_id: str,
    *,
    seqs: Sequence[int] = (),
    suites_root: Path | None = None,
    fixtures_root: Path | None = None,
) -> RecordResult:
    """Append one case per served message of ``session_id`` (only ``seqs`` when given).

    Raises :class:`~harnessed.errors.SurfaceNotFound` for an unknown session (``for_session``'s
    own rule) and :class:`~harnessed.errors.SuiteLoadError` for an existing suite that is not a
    work suite or a ``seq`` the session never served.
    """
    root = suites.home_suites_dir() if suites_root is None else suites_root
    fixtures = suites.home_fixtures_dir() if fixtures_root is None else fixtures_root
    suites.refuse_shadow(suite, root)
    served = messages.for_session(conn, session_id)
    wanted = _select(_operator_only(served), seqs, session_id)

    path = root / f"{suite}{suites.SUITE_SUFFIX}"
    document = _read_document(path, suite)
    cases: list[dict[str, Any]] = document["cases"]
    known = {(case.get("session"), case.get("seq")) for case in cases}
    number = max((_number(str(case["id"])) for case in cases), default=0)

    added: list[str] = []
    kept: list[str] = []
    for row in wanted:
        if (session_id, row.message_seq) in known:
            kept.append(_id_of(cases, session_id, row.message_seq))
            continue
        built = _case_for(conn, suite, session_id, row, number + 1)
        if built is None:
            continue
        case, turns = built
        number += 1
        cases.append(case)
        added.append(str(case["id"]))
        if turns:
            _write_fixture(fixtures / str(case["id"]), turns)

    if added or not path.exists():
        _write_document(path, document)
    return RecordResult(path=path, added=tuple(added), kept=tuple(kept))


def _operator_only(served: tuple[messages.MessageRow, ...]) -> tuple[messages.MessageRow, ...]:
    """Only the messages whose served ``author`` is ``operator`` (decision 77, MUST rule 40): a
    task's first message and its continuations are not the operator's words."""
    return tuple(row for row in served if row.author == messages.AUTHOR_OPERATOR)


def _select(
    served: tuple[messages.MessageRow, ...], seqs: Sequence[int], session_id: str
) -> tuple[messages.MessageRow, ...]:
    if not seqs:
        return served
    have = {row.message_seq for row in served}
    for seq in seqs:
        if seq not in have:
            raise SuiteLoadError(
                f"session {session_id!r} served no message with seq {seq}. "
                f"Served: {', '.join(str(n) for n in sorted(have)) or 'none'}."
            )
    return tuple(row for row in served if row.message_seq in set(seqs))


def _case_for(
    conn: sqlite3.Connection,
    suite: str,
    session_id: str,
    row: messages.MessageRow,
    number: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """One message as a case and its fixture responses, or ``None`` when it has no text or reply
    to record (a message the router never saw, or one that stopped before answering)."""
    if row.text is None or row.reply is None or row.reply.text is None:
        return None
    text = str(redact.scrub(row.text))
    route = conn.execute(_SELECT_ROUTE, (session_id, row.message_seq)).fetchone()
    handler = _handler(None if route is None else str(route[0]), row.handler)
    case: dict[str, Any] = {
        "id": f"{suite}-{number:03d}-{_slug(text)}",
        "input": text,
        "handler": handler,
        "reference": str(redact.scrub(row.reply.text)),
        "verdict": suites.VERDICT_PENDING,
        "session": session_id,
        "seq": row.message_seq,
    }
    turns = _turns(conn, session_id, row.message_seq) if handler in suites.GROQ_HANDLERS else []
    return case, turns


def _handler(route: str | None, handler: str | None) -> str:
    """``groq_chat``, ``groq_memory`` or the entry's own name, as served."""
    if route:
        return route
    return _CHAT_ROUTE if handler == _GROQ or handler is None else handler


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:_SLUG_WORDS]
    return "-".join(words)[:_SLUG_LENGTH].strip("-") or "message"


def _number(case_id: str) -> int:
    found = _NUMBER.match(case_id)
    return int(found.group(1)) if found else 0


def _id_of(cases: Iterable[Mapping[str, Any]], session_id: str, seq: int) -> str:
    return next(
        str(case["id"])
        for case in cases
        if (case.get("session"), case.get("seq")) == (session_id, seq)
    )


# ---- removal -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoveResult:
    """What a removal deleted: the suite file and the fixture directories its cases named."""

    path: Path
    fixtures: tuple[str, ...]


def remove(
    suite: str, *, suites_root: Path | None = None, fixtures_root: Path | None = None
) -> RemoveResult:
    """Delete a recorded work suite and each fixture directory its cases name, nothing else.

    A shipped suite is refused unless ``suites_root`` names the corpus, and so is a suite that is
    not a work suite or does not exist, each as a :class:`~harnessed.errors.SuiteLoadError` naming
    the suite (decision 88).
    """
    if suite in SHIPPED_SUITES and suites_root is None:
        raise SuiteLoadError(f"suite {suite!r} ships with harnessed and cannot be removed")
    root = suites.home_suites_dir() if suites_root is None else suites_root
    fixtures = suites.home_fixtures_dir() if fixtures_root is None else fixtures_root
    path = root / f"{suite}{suites.SUITE_SUFFIX}"
    loaded = suites.load_named_suite(suite, suites.suite_roots(root))
    if loaded.kind != suites.KIND_WORK:
        raise SuiteLoadError(f"{path}: suite {suite!r} is a {loaded.kind} suite, not a work suite")
    gone: list[str] = []
    for case in loaded.cases:
        directory = fixtures / case.id
        if directory.parent == fixtures and directory.is_dir():
            shutil.rmtree(directory)
            gone.append(case.id)
    path.unlink()
    return RemoveResult(path=path, fixtures=tuple(gone))


def remove_payload(done: RemoveResult) -> dict[str, object]:
    """The ``--json`` twin of the removal summary."""
    return {"removed": done.path.name, "fixtures": list(done.fixtures)}


# ---- fixtures ------------------------------------------------------------------------------


def _turns(conn: sqlite3.Connection, run_id: str, seq: int) -> list[dict[str, Any]]:
    """The message's recorded responses, scrubbed, one per turn, in the corpus's own shape."""
    out: list[dict[str, Any]] = []
    for ordinal, row in enumerate(conn.execute(_SELECT_TURNS, (run_id, seq)).fetchall(), start=1):
        iteration, content, stop, inputs, outputs, model = row
        calls = [
            {"id": call_id, "name": name, "arguments": _LEDGER_JSON.decode(arguments)}
            for call_id, name, arguments in conn.execute(_SELECT_TOOL_CALLS, (run_id, iteration))
        ]
        response: dict[str, Any] = {
            "response_id": f"recorded_{ordinal}",
            "model": str(model or "recorded"),
            "content": str(content),
            "stop_reason": str(stop or "end_turn"),
            "usage": {"input_tokens": int(inputs or 0), "output_tokens": int(outputs or 0)},
        }
        if calls:
            response["tool_calls"] = calls
        out.append(redact.scrub(response))
    return out


def _write_fixture(directory: Path, turns: Sequence[Mapping[str, Any]]) -> None:
    """One scope directory: the index (always ``when: always``) and one file per turn."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "_index.yaml").write_text(
        f"# Recorded from a real session (ticket 235). Reviewed code (MUST rule 8).\n"
        f"scope: {directory.name}\nvariants:\n  - name: base\n    when: always\n",
        encoding="utf-8",
    )
    for ordinal, response in enumerate(turns, start=1):
        name = "base.json" if ordinal == 1 else f"base.{ordinal}.json"
        (directory / name).write_text(
            json.dumps(response, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


# ---- the suite document --------------------------------------------------------------------


def _read_document(path: Path, suite: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "suite": suite,
            "description": "A work suite, recorded from real sessions.",
            "kind": suites.KIND_WORK,
            "cases": [],
        }
    loaded = suites.load_suite(path)
    if loaded.kind != suites.KIND_WORK:
        raise SuiteLoadError(f"{path}: suite {suite!r} is a {loaded.kind} suite, not a work suite")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["cases"] = document.get("cases") or []
    return dict(document)


def _write_document(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )


# ---- verdicts ------------------------------------------------------------------------------


def set_verdict(
    suite: str,
    case_id: str,
    *,
    verdict: str | None = None,
    traits: Sequence[str] = (),
    suites_root: Path | None = None,
) -> None:
    """Set ``case_id``'s verdict to ``accept`` or ``reject``, or to the given traits.

    Traits are appended to any already set; ``accept``/``reject`` replace whatever was there.
    An unknown suite or case is a :class:`~harnessed.errors.SuiteLoadError` naming it.
    """
    root = suites.home_suites_dir() if suites_root is None else suites_root
    loaded = suites.load_named_suite(suite, suites.suite_roots(root))
    if loaded.path.parent != root:
        raise SuiteLoadError(
            f"suite {suite!r} ships with harnessed at {loaded.path}; pass --to <corpus> to set a "
            f"verdict there"
        )
    loaded.case(case_id)
    path = loaded.path
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for case in document["cases"]:
        if case["id"] != case_id:
            continue
        if traits:
            kept = (
                list(case.get("traits") or [])
                if case.get("verdict") == suites.VERDICT_TRAITS
                else []
            )
            case["traits"] = [*kept, *(str(redact.scrub(trait)) for trait in traits)]
            case["verdict"] = suites.VERDICT_TRAITS
        else:
            case["verdict"] = verdict
            case.pop("traits", None)
    _write_document(path, document)


def record_payload(done: RecordResult) -> dict[str, object]:
    """The ``--json`` twin of the recording summary."""
    return {"added": list(done.added), "kept": list(done.kept)}


def pending_cases(suite: str, suites_root: Path | None = None) -> tuple[suites.Case, ...]:
    """The cases of ``suite`` still awaiting a verdict, in suite order."""
    loaded = suites.load_named_suite(suite, suites.suite_roots(suites_root))
    return tuple(case for case in loaded.cases if case.pending)


def pending_payload(cases: Sequence[suites.Case]) -> dict[str, object]:
    """The ``--json`` twin of the ``--pending`` listing."""
    return {
        "pending": [
            {"case_id": case.id, "input": case.prompt, "reference": case.reference}
            for case in cases
        ]
    }
