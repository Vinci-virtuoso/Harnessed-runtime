"""
FR-84: the diagram's ``STAGE`` table is drift-tested against the frozen taxonomy.

Ticket 142's stage-table criterion in executable form (MUST rule 4, architecture "The contracts the
gateway consumes"):

- **every** name in ``events/names.py::NAMES`` has a row in ``dashboard/lib/stages.ts``, including
  the names that light nothing, which are named explicitly rather than left out;
- **0** rows name a name the taxonomy does not hold;
- adding a name to ``names.py`` without a row **fails**: the seeded case copies the real module,
  adds one name, loads the copy and shows the same comparison reporting it (MUST rule 12).

The table is read as text. The dashboard is TypeScript and this suite is Python, so the one thing
both sides share is the file: a row is a quoted event name at the start of a line inside the
``STAGE`` literal, which is exactly how the table is written.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

from harnessed.events.names import NAMES

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGES_TS = REPO_ROOT / "dashboard" / "lib" / "stages.ts"
NAMES_PY = REPO_ROOT / "src" / "harnessed" / "events" / "names.py"
EVIDENCE = REPO_ROOT / "evidence" / "142" / "stages.txt"

#: The ``STAGE`` literal, from its declaration to the line that closes it.
_TABLE = re.compile(r"^export const STAGE\b.*?^\};$", re.MULTILINE | re.DOTALL)
#: One row: a quoted name opening a line, then the stage it lights.
_ROW = re.compile(r'^\s+"([^"]+)":\s*([^,\n]+)', re.MULTILINE)

#: The names that light no region (design.md's stage table). Each still has a row, spelled out.
LIGHTS_NOTHING = frozenset(
    {
        "loop.guardrail.tripped",
        "error.raised",
        "observer.failed",
        "task.started",
        "task.paused",
        "task.finished",
        "memory.consolidation.finished",
    }
)

#: The name the seeded copy of ``names.py`` gains, and that no row can hold.
SEEDED = "seeded.name.added"


def stage_rows(source: str) -> dict[str, str]:
    """Every row of the ``STAGE`` table: the event name, and the text of what it lights."""
    table = _TABLE.search(source)
    assert table is not None, f"{STAGES_TS} holds no `export const STAGE = {{ ... }};` literal"
    return {name: lights.strip() for name, lights in _ROW.findall(table.group(0))}


def drift(names: frozenset[str], rows: set[str]) -> list[str]:
    """What stands between the frozen names and the table's rows, one line per disagreement."""
    missing = [f"no row for frozen name {name!r}" for name in sorted(names - rows)]
    unfrozen = [f"row for unfrozen name {name!r}" for name in sorted(rows - names)]
    return missing + unfrozen


def seeded_names(tmp_path: Path) -> ModuleType:
    """A copy of the real ``names.py`` with one name added, loaded as its own module."""
    source = NAMES_PY.read_text(encoding="utf-8")
    anchor = '        "run.started",\n'
    assert anchor in source, "the seed's anchor moved in names.py"
    copy = tmp_path / "seeded_names.py"
    copy.write_text(source.replace(anchor, f'{anchor}        "{SEEDED}",\n', 1), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("seeded_names", copy)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_frozen_name_has_a_row_and_no_row_names_an_unfrozen_one() -> None:
    rows = stage_rows(STAGES_TS.read_text(encoding="utf-8"))

    assert drift(NAMES, set(rows)) == []
    assert len(rows) == len(NAMES)


def test_the_names_that_light_nothing_are_named_explicitly() -> None:
    rows = stage_rows(STAGES_TS.read_text(encoding="utf-8"))

    nothing = {name for name, lights in rows.items() if lights == "NOTHING"}
    assert nothing == LIGHTS_NOTHING
    assert LIGHTS_NOTHING <= NAMES


def test_a_name_added_to_names_py_without_a_row_fails(tmp_path: Path) -> None:
    rows = stage_rows(STAGES_TS.read_text(encoding="utf-8"))
    seeded = seeded_names(tmp_path)

    assert NAMES | {SEEDED} == seeded.NAMES
    found = drift(seeded.NAMES, set(rows))
    assert found == [f"no row for frozen name {SEEDED!r}"]

    # And the other direction: a row the taxonomy does not hold is reported too.
    assert drift(NAMES, set(rows) | {SEEDED}) == [f"row for unfrozen name {SEEDED!r}"]

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        "\n".join(
            [
                "FR-84, ticket 142: the STAGE table in dashboard/lib/stages.ts"
                " against events/names.py",
                f"frozen names: {len(NAMES)}; table rows: {len(rows)};"
                f" drift: {drift(NAMES, set(rows))}",
                f"rows that light nothing, named explicitly: {sorted(LIGHTS_NOTHING)}",
                f"names.py seeded with {SEEDED!r}: {found}",
                "",
                *(f"  {name:<30} {rows[name]}" for name in sorted(rows)),
                "",
            ]
        ),
        encoding="utf-8",
    )
