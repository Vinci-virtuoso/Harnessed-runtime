"""
FR-129 (`D-7`): ``HUMAN_WORDS`` is complete against the words the ledger can actually emit, and
every event name has a trace sentence or is named as one that deliberately has none.

Ticket 184 wrote the ``HUMAN_WORDS`` half of the drift check below. Ticket 185 adds the
trace-sentence half, against ``events/names.py::NAMES``, over ``dashboard/lib/trace-steps.ts``.
Two claims, each able to fail on its own, for the older half:

- every word one of the eight named fields (stop reasons, guardrails, outcomes, delegation
  reasons, billing, kind, memory kind, key kind) can genuinely carry has a row in
  ``dashboard/lib/human-words.ts``'s ``HUMAN_WORDS``, checked against the Python source that emits
  each vocabulary (an import where one exists, a literal cited from source where it does not -
  ``ux/design.md``'s own table undercounts a few of these, recorded in the ticket's own "For the
  architecture node" note);
- **0** other static maps from one of those same ledger words to display text exist anywhere under
  ``dashboard/components`` or ``dashboard/lib`` outside ``human-words.ts`` itself (a word rendered a
  second time, by a second table, is exactly the drift this file exists to catch).

The table is read as text, the same way ``test_dashboard_stages.py`` reads ``stages.ts``: this
suite is Python and the table is TypeScript, so the file's own characters are the one thing both
sides can compare without importing a JS runtime.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from acp.schema import ToolKind

from harnessed.acp.config import BILLINGS, KINDS
from harnessed.acp.ledger import DIRTY_TREE, QUOTA_REFUSED
from harnessed.acp.pending import ANSWERED_VIA, ANSWERS, RULES, STATES
from harnessed.cli.routed import HOP_REASONS, HOP_RESULTS
from harnessed.cli.run_cmd import RAISED
from harnessed.evals.suite import KINDS as SUITE_KINDS
from harnessed.evals.suite import VERDICTS
from harnessed.events.names import NAMES
from harnessed.loop.guardrails import GUARDRAILS
from harnessed.loop.messages import OUTCOME_FAILED
from harnessed.loop.runs import KEY_KINDS
from harnessed.memory.consolidation import FAILURES as CONSOLIDATION_FAILURES
from harnessed.memory.consolidation import STATUSES as CONSOLIDATION_STATUSES
from harnessed.providers.types import STOP_REASONS
from harnessed.router.decisions import COMPARED_WORDS, ROUTING_WORDS
from harnessed.router.questions import MEMORY_KINDS
from harnessed.tasks.installer import WHERE_LAUNCHD, WHERE_SYSTEMD
from harnessed.tasks.store import STATES as TASK_STATES
from harnessed.tasks.store import STOP_REASONS as TASK_STOP_REASONS

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "dashboard"
HUMAN_WORDS_TS = DASHBOARD / "lib" / "human-words.ts"
TRACE_STEPS_TS = DASHBOARD / "lib" / "trace-steps.ts"
EVIDENCE = REPO_ROOT / "evidence" / "184" / "vocabulary.txt"
TRACE_EVIDENCE = REPO_ROOT / "evidence" / "185" / "vocabulary.txt"

#: One row of ``HUMAN_WORDS``: ``{ field: "...", word: "...", text: "..." }``.
_ROW = re.compile(
    r'\{\s*field:\s*"([^"]+)",\s*word:\s*"([^"]+)",\s*text:\s*"((?:[^"\\]|\\.)*)"\s*\}'
)

#: ``router/route.py::Outcome`` and ``StopReason`` are ``Literal`` aliases with no runtime tuple
#: (nothing else in the source needs one), so the two words are cited here instead of imported.
DECISION_OUTCOMES: tuple[str, ...] = ("routed", "uncertain", "fallback", OUTCOME_FAILED)
DECISION_STOP_REASONS: tuple[str, ...] = ("needs_human", "below_floor")

#: ``acp/delegate.py``'s ACP-kind ``status`` values (``_finish`` call sites, no exported tuple).
DELEGATION_STATUSES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "error",
    "timed_out",
    "version_mismatch",
    QUOTA_REFUSED,
)

#: ``acp/cli_runner.py``'s cli-kind failure-only ``reason`` values (no exported tuple).
DELEGATION_REASONS: tuple[str, ...] = ("timeout", "wrote_files", QUOTA_REFUSED, DIRTY_TREE)

#: Every (field, word) the ledger can emit today, across the eight AC3 areas. ``stop_reason``
#: collects both the provider vocabulary and ``run_cmd.RAISED``, which the dashboard shows under
#: the same field.
EMITTABLE: frozenset[tuple[str, str]] = frozenset(
    {("stop_reason", word) for word in (*STOP_REASONS, RAISED)}
    | {("decision_stop_reason", word) for word in DECISION_STOP_REASONS}
    | {("guardrail", word) for word in GUARDRAILS}
    | {("outcome", word) for word in DECISION_OUTCOMES}
    | {("delegation_status", word) for word in DELEGATION_STATUSES}
    | {("delegation_reason", word) for word in DELEGATION_REASONS}
    | {("hop_result", word) for word in HOP_RESULTS}
    | {("hop_reason", word) for word in HOP_REASONS}
    | {("billing", word) for word in BILLINGS}
    | {("kind", word) for word in KINDS}
    | {("memory_kind", word) for word in MEMORY_KINDS}
    | {("key_kind", word) for word in KEY_KINDS}
    | {("permission_state", word) for word in STATES}
    | {("permission_rule", word) for word in RULES}
    | {("permission_answer", word) for word in ANSWERS}
    | {("answered_via", word) for word in ANSWERED_VIA}
    | {("tool_kind", word) for word in get_args(ToolKind)}
    | {("suite_kind", word) for word in SUITE_KINDS}
    | {("case_verdict", word) for word in VERDICTS}
    | {("task_state", word) for word in TASK_STATES}
    | {("task_stop_reason", word) for word in TASK_STOP_REASONS}
    | {("consolidation_status", word) for word in CONSOLIDATION_STATUSES}
    | {("consolidation_failure", word) for word in CONSOLIDATION_FAILURES}
    | {("compared", word) for word in COMPARED_WORDS}
    | {("schedule_where", word) for word in (WHERE_LAUNCHD, WHERE_SYSTEMD)}
    | {("routing", word) for word in ROUTING_WORDS}
)

#: A field:word this table may hold with no ledger row backing it yet. Empty since ticket 194 emits
#: ``quota_refused``, which stays a row (ticket 232). The drift check below fails on any row that
#: is neither emittable nor named here, so a later ticket that adds an emitted word adds its row
#: and its constant to ``EMITTABLE`` in the same commit.
NOT_YET_EMITTABLE: frozenset[tuple[str, str]] = frozenset()

#: Every literal ledger word this file's ``EMITTABLE`` set names, for the "no other map" scan: a
#: second table translating any of these into display text - outside ``human-words.ts`` - is the
#: duplicate vocabulary AC3 forbids. Words too short or too generic to search for safely (none of
#: these are) would be excluded here; none are.
#: The consolidation fields' (ticket 249) are left out too, for the task fields' reason.
#: The task fields' words are left out of the scan: ``finished``, ``running`` and ``failed`` are
#: also the trace step and status vocabulary (`StepKind`, `StepStatus`), a different field with a
#: different meaning, so a literal `"running":` in a component is not a second task-state table.
#: The words are still held to a ``HUMAN_WORDS`` row by ``EMITTABLE`` above (ticket 239).
LEDGER_WORDS: frozenset[str] = frozenset(
    word for field, word in EMITTABLE if not field.startswith(("task_", "consolidation_"))
)


def human_word_rows(source: str) -> set[tuple[str, str]]:
    """Every ``(field, word)`` pair ``HUMAN_WORDS`` documents, read off the file's own text."""
    return {(field, word) for field, word, _text in _ROW.findall(source)}


def test_every_word_the_ledger_can_emit_has_a_row() -> None:
    rows = human_word_rows(HUMAN_WORDS_TS.read_text(encoding="utf-8"))

    missing = sorted(EMITTABLE - rows)
    assert missing == [], f"HUMAN_WORDS is missing a row for: {missing}"


def test_every_row_is_either_emittable_or_named_not_yet() -> None:
    rows = human_word_rows(HUMAN_WORDS_TS.read_text(encoding="utf-8"))
    fields = {
        "stop_reason",
        "decision_stop_reason",
        "guardrail",
        "outcome",
        "delegation_status",
        "delegation_reason",
        "billing",
        "kind",
        "memory_kind",
        "key_kind",
        "hop_result",
        "hop_reason",
        "permission_state",
        "permission_rule",
        "permission_answer",
        "answered_via",
        "tool_kind",
        "suite_kind",
        "case_verdict",
        "task_state",
        "task_stop_reason",
        "consolidation_status",
        "consolidation_failure",
        "compared",
        "routing",
    }
    covered_rows = {row for row in rows if row[0] in fields}

    unexplained = sorted(covered_rows - EMITTABLE - NOT_YET_EMITTABLE)
    assert unexplained == [], f"HUMAN_WORDS documents a row source cannot emit: {unexplained}"


def test_every_row_has_non_empty_display_text() -> None:
    source = HUMAN_WORDS_TS.read_text(encoding="utf-8")
    rows = _ROW.findall(source)
    assert len(rows) > 0
    empty = [f"{field}:{word}" for field, word, text in rows if text.strip() == ""]
    assert empty == []


def test_no_other_static_map_from_a_ledger_word_to_display_text() -> None:
    """A second translation table is exactly the drift this file exists to prevent (AC3)."""
    offenders: list[str] = []
    for tree in (DASHBOARD / "components", DASHBOARD / "lib"):
        for path in sorted(tree.rglob("*.ts*")):
            if path == HUMAN_WORDS_TS:
                continue
            if path.name.endswith(".test.ts") or path.name.endswith(".test.tsx"):
                continue
            text = path.read_text(encoding="utf-8")
            # A record/object literal mapping two or more of the ledger's own words to a string is
            # a second vocabulary table; `RuleBadge`'s gloss table keys on the promoter's own
            # `rule` words, none of which are in `LEDGER_WORDS`, so it is never a false positive.
            hits = {word for word in LEDGER_WORDS if re.search(rf'"{re.escape(word)}"\s*:', text)}
            if len(hits) >= 2:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {sorted(hits)}")

    assert offenders == [], f"a second word -> text map exists outside human-words.ts: {offenders}"

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    rows = human_word_rows(HUMAN_WORDS_TS.read_text(encoding="utf-8"))
    EVIDENCE.write_text(
        "\n".join(
            [
                "FR-129, ticket 184: HUMAN_WORDS against every word the ledger can emit",
                f"emittable: {len(EMITTABLE)}; table rows: {len(rows)}",
                f"missing: {sorted(EMITTABLE - rows)}",
                f"not-yet-emittable rows kept for design.md fidelity: {sorted(NOT_YET_EMITTABLE)}",
                "0 other word -> text maps found under dashboard/components, dashboard/lib",
                "",
            ]
        ),
        encoding="utf-8",
    )


# ---- Ticket 185: the trace-sentence half -------------------------------------------------------
#
# ``lib/trace-steps.ts`` keys its sentence table (``TRACE_STEPS``) on the event name, one entry
# per quoted key at the start of a row inside the object literal - the same textual reading
# ``test_dashboard_stages.py`` gives ``stages.ts``'s ``STAGE`` table, for the same reason: this
# suite is Python and the table is TypeScript. ``VERBATIM`` is a plain string-array literal.

#: One quoted key opening a row inside an object literal: ``  "name.here": {``.
_TRACE_KEY = re.compile(r'^\s+"([^"]+)":\s*\{', re.MULTILINE)

#: One quoted string inside the ``VERBATIM`` array literal.
_VERBATIM_ARRAY = re.compile(r"export const VERBATIM:.*?\[(.*?)\];", re.DOTALL)
_VERBATIM_ITEM = re.compile(r'"([^"]+)"')


def trace_step_names(source: str) -> set[str]:
    """Every event name ``TRACE_STEPS`` holds a row for, read off the file's own text."""
    table = source[source.index("export const TRACE_STEPS") : source.index("export const VERBATIM")]
    return set(_TRACE_KEY.findall(table))


def verbatim_names(source: str) -> set[str]:
    """Every event name ``VERBATIM`` names, read off the file's own text."""
    match = _VERBATIM_ARRAY.search(source)
    assert match is not None, f"{TRACE_STEPS_TS} holds no `export const VERBATIM = [...]` literal"
    return set(_VERBATIM_ITEM.findall(match.group(1)))


def test_every_frozen_name_has_a_sentence_or_is_named_verbatim() -> None:
    source = TRACE_STEPS_TS.read_text(encoding="utf-8")
    sentenced = trace_step_names(source)
    verbatim = verbatim_names(source)

    missing = sorted(NAMES - sentenced - verbatim)
    assert missing == [], f"lib/trace-steps.ts has no sentence or VERBATIM row for: {missing}"


def test_a_name_is_never_both_sentenced_and_verbatim() -> None:
    source = TRACE_STEPS_TS.read_text(encoding="utf-8")
    both = sorted(trace_step_names(source) & verbatim_names(source))
    assert both == [], f"lib/trace-steps.ts gives both a sentence and VERBATIM to: {both}"


def test_no_row_names_an_unfrozen_event() -> None:
    source = TRACE_STEPS_TS.read_text(encoding="utf-8")
    sentenced = trace_step_names(source)
    verbatim = verbatim_names(source)

    unfrozen = sorted((sentenced | verbatim) - NAMES)
    assert (
        unfrozen == []
    ), f"lib/trace-steps.ts names an event the taxonomy does not hold: {unfrozen}"


def test_the_deleted_row_component_leaves_no_file_or_import_behind() -> None:
    """AC3: **0** EventRow files or imports remain, anywhere `dashboard/` is scanned."""
    deleted_file = DASHBOARD / "components" / "EventRow.tsx"
    deleted_style = DASHBOARD / "components" / "EventRow.module.css"
    assert not deleted_file.exists()
    assert not deleted_style.exists()

    # The banned spelling is assembled rather than written whole, the same discipline ticket 024's
    # own quarantine scan uses, so this assertion is not itself a hit of its own sweep.
    banned = "".join(["Event", "Row"])
    offenders: list[str] = []
    for path in sorted(DASHBOARD.rglob("*")):
        if path.suffix not in {".ts", ".tsx"} or not path.is_file():
            continue
        parts = path.relative_to(DASHBOARD).parts
        if parts[0] in {"node_modules", ".next", "out"}:
            continue
        if banned in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == [], f"{banned} still appears in: {offenders}"

    TRACE_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    source = TRACE_STEPS_TS.read_text(encoding="utf-8")
    TRACE_EVIDENCE.write_text(
        "\n".join(
            [
                "FR-129, ticket 185: lib/trace-steps.ts against events/names.py::NAMES",
                f"frozen names: {len(NAMES)}",
                f"sentenced: {len(trace_step_names(source))}",
                f"verbatim: {sorted(verbatim_names(source))}",
                f"missing: {sorted(NAMES - trace_step_names(source) - verbatim_names(source))}",
                f"0 {banned} files or imports remain under dashboard/",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_the_one_event_consolidation_session_reads_memory_consolidated() -> None:
    """Ticket 249 (FR-152): the sessions listing's words for a batch's own session."""
    source = TRACE_STEPS_TS.read_text(encoding="utf-8")
    assert "memory.consolidation.finished" in trace_step_names(source)
    row = source[source.index('"memory.consolidation.finished": {') :]
    assert 'text("Memory consolidated")' in row.split("\n  },\n", 1)[0]
