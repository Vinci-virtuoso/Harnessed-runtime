"""FR-40: a recorded improvement cycle can be read back by its id (ticket 074).

``steps.cycle_for(conn, improvement_run_id)`` is the one store callable that rebuilds a
:class:`~harnessed.improve.steps.Cycle` from the ledger without re-executing anything. Four
claims carry this file:

* **The read-back renders the same bytes.** ``json_payload`` of the cycle ``run_cycle`` returned
  and of the same cycle read back by id are byte-identical under ``serialize.canonical``. That is
  the FR-40 one-call rule made testable: a gateway handler that renders the read-back renders
  exactly what the CLI printed.
* **A miss is an answer.** An id no row carries returns ``None``; nothing raises.
* **The decision comes off the ledger.** ``promoted``, ``rule`` and the scores equal what the
  original cycle reported, and the solver is never called again to produce them.
* **Absence is absent.** A cycle interrupted before its reflection reads back with only the
  steps the ledger holds; nothing is defaulted into a step that never ran.

Fields that legitimately differ between a run and a read-back are pass flags, not cycle facts:
``resumed``, ``dry_run`` and each step's ``replayed`` describe how one pass was invoked, and the
ledger holds no column for any of them. The read-back reports a fresh full pass, so the payload
matches a fresh full run and the tests here assert the payload rather than the object.
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from harnessed import config, serialize
from harnessed.evals.suite import Suite, load_named_suite
from harnessed.improve import steps as steps_module
from harnessed.loop.engine import LoopResult, run_loop
from harnessed.loop.prompt import build_system_prompt
from harnessed.memory import db
from harnessed.providers.fake import FakeProvider
from harnessed.providers.types import ChatRequest
from harnessed.tools import default_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "074"
STEPS_SOURCE = REPO_ROOT / "src" / "harnessed" / "improve" / "steps.py"

MODEL = "fake-1"

#: The areas ``improve/steps.py`` reached before this ticket. The read must not widen them.
PERMITTED_AREAS = frozenset({"errors", "evals", "events", "improve", "memory", "providers"})


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


class Boom(RuntimeError):
    """The interruption. Deliberately not a ``HarnessError``: nothing may dress it up."""


# ---- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def smoke() -> Suite:
    return load_named_suite("smoke")


@pytest.fixture
def holdout() -> Suite:
    return load_named_suite("holdout")


def real_solver(home: Path) -> Callable[[ChatRequest], LoopResult]:
    """The real loop, the real ``FakeProvider`` and the real registry behind one callable."""
    provider = FakeProvider(config.load())
    registry = default_registry(home)

    def solve(request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        return run_loop(provider, registry, request, limit=6)

    return solve


def run(
    conn: sqlite3.Connection,
    home: Path,
    suite: Suite,
    holdout: Suite | None,
    *,
    on_step: Callable[[str], None] | None = None,
) -> steps_module.Cycle:
    """One fresh, full cycle with every seam wired the way a composition root wires it."""
    return steps_module.run_cycle(
        conn,
        suite=suite,
        holdout=holdout,
        solve=real_solver(home),
        reflector=FakeProvider(config.load()),
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
        on_step=on_step,
    )


def payload_bytes(cycle: steps_module.Cycle) -> str:
    return serialize.canonical(steps_module.json_payload(cycle))


# ---- the read ------------------------------------------------------------------------------------


def test_a_recorded_cycle_reads_back_byte_identical(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    ran = run(conn, tmp_harnessed_home, smoke, holdout)

    read = steps_module.cycle_for(conn, ran.improvement_run_id)

    assert read is not None
    assert isinstance(read, steps_module.Cycle)
    assert payload_bytes(read) == payload_bytes(ran)
    capture(
        "cycle-read-back.txt",
        "\n".join(
            (
                f"run_cycle  {payload_bytes(ran)}",
                f"cycle_for  {payload_bytes(read)}",
                f"identical  {payload_bytes(read) == payload_bytes(ran)}",
            )
        )
        + "\n",
    )


def test_an_id_no_row_carries_reads_back_as_none(conn: sqlite3.Connection) -> None:
    assert steps_module.cycle_for(conn, "imp_never_recorded") is None


def test_the_decision_is_read_off_the_ledger_not_re_run(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    ran = run(conn, tmp_harnessed_home, smoke, holdout)
    assert ran.promoted is True
    eval_runs_before = conn.execute("SELECT count(*) FROM eval_runs").fetchone()[0]
    steps_before = conn.execute("SELECT count(*) FROM improvement_steps").fetchone()[0]

    read = steps_module.cycle_for(conn, ran.improvement_run_id)

    assert read is not None
    assert read.has_candidate is ran.has_candidate is True
    assert read.promoted is ran.promoted
    assert read.rule == ran.rule
    assert read.score_before == ran.score_before
    assert read.score_after == ran.score_after
    assert read.scored == ran.scored
    assert read.cand_id == ran.cand_id
    assert read.suite == ran.suite
    assert [step.step for step in read.steps] == [step.step for step in ran.steps]
    assert read.before == ran.before
    assert read.after == ran.after
    assert read.holdout == ran.holdout
    assert read.outcome == ran.outcome
    assert conn.execute("SELECT count(*) FROM eval_runs").fetchone()[0] == eval_runs_before
    assert conn.execute("SELECT count(*) FROM improvement_steps").fetchone()[0] == steps_before


def test_a_cycle_that_never_reflected_reads_back_without_defaults(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    def interrupt(step: str) -> None:
        if step == steps_module.REFLECT:
            raise Boom(step)

    with pytest.raises(Boom):
        run(conn, tmp_harnessed_home, smoke, holdout, on_step=interrupt)
    interrupted = steps_module.latest(conn, smoke.name)
    assert interrupted is not None

    read = steps_module.cycle_for(conn, interrupted)

    assert read is not None
    assert read.improvement_run_id == interrupted
    assert read.suite == smoke.name
    assert [step.step for step in read.steps] == [steps_module.BASELINE]
    assert read.has_candidate is False
    assert read.cand_id == steps_module.NO_CANDIDATE
    assert read.before is not None
    stored = steps_module.get(conn, interrupted, steps_module.BASELINE, smoke.name)
    assert stored is not None
    assert read.before == steps_module.measurement(stored)
    assert read.after is None
    assert read.holdout is None
    assert read.outcome is None
    assert read.promoted is False
    assert read.rule == ""


def test_a_run_row_with_no_steps_is_an_empty_cycle_not_a_miss(conn: sqlite3.Connection) -> None:
    improvement_run_id, resumed = steps_module.start(conn, "smoke", fresh=True)
    assert resumed is False

    read = steps_module.cycle_for(conn, improvement_run_id)

    assert read is not None
    assert read.suite == "smoke"
    assert read.steps == ()
    assert read.before is None
    assert read.has_candidate is False


def test_the_read_widens_no_import(tmp_harnessed_home: Path) -> None:
    tree = ast.parse(STEPS_SOURCE.read_text(encoding="utf-8"))
    areas: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("harnessed.")
        ):
            areas.add(node.module.split(".")[1])
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("harnessed."):
                    areas.add(alias.name.split(".")[1])
    assert areas <= PERMITTED_AREAS
    assert "cycle_for" in steps_module.__all__
