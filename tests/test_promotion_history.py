"""FR-41: promotion history across candidates, additive to ``procedural.promotion`` (ticket 066).

``procedural.promotion(cand_id)`` answers one question - the **first** decision for **one**
candidate - and the promoter's idempotency rests on that answer, so it is not widened here.
``promotions(conn, limit=N)`` is the history the gateway lists: every decision across every
candidate in decided order, each carrying both scores, the regression count and any soft drop,
as the same typed ``Promotion`` the single lookup already returns.

Nothing here re-derives a decision (MUST rule 1). The rows are seeded through
``record_promotion`` with the numbers already decided, and the listing reports them back.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from harnessed import clock
from harnessed.memory import db, procedural

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence" / "066" / "promotion-history.txt"

PROCEDURE = "Always run the test suite before declaring a change complete."
CANDIDATES = ("cand_alpha", "cand_beta", "cand_gamma")

pytestmark = pytest.mark.usefixtures("frozen_clock")


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection with the candidates the decisions will reference.

    ``promotions.cand_id`` is a foreign key onto ``candidates`` and foreign keys are on, so the
    candidate rows exist before any decision is recorded against them.
    """
    connection = db.connect()
    db.write(
        connection,
        tuple(
            (
                "INSERT INTO candidates (cand_id, text, source_case_ids, provenance, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (cand_id, PROCEDURE, '["b"]', "ticket 066", clock.isoformat(clock.now())),
            )
            for cand_id in CANDIDATES
        ),
    )
    yield connection
    connection.close()


def tick(seconds: int) -> None:
    """Advance the frozen clock so two decisions never share a ``decided_at``."""
    clock.freeze(clock.now() + timedelta(seconds=seconds))


def decide(
    conn: sqlite3.Connection,
    cand_id: str,
    *,
    promoted: bool,
    before: float,
    after: float,
    regressions: int = 0,
    soft_drops: int = 0,
) -> procedural.Promotion:
    """Record one already-decided outcome, one second after the previous one."""
    tick(1)
    return procedural.record_promotion(
        conn,
        cand_id=cand_id,
        text=f"{PROCEDURE} ({cand_id})",
        promoted=promoted,
        reason="score_gain" if promoted else "no_gain",
        score_before=before,
        score_after=after,
        passes_before=3,
        passes_after=4 if promoted else 3,
        regressions=regressions,
        soft_drops=soft_drops,
    )


def seed_history(conn: sqlite3.Connection) -> list[procedural.Promotion]:
    """Four decisions over three candidates, ``cand_alpha`` decided twice."""
    return [
        decide(conn, "cand_alpha", promoted=False, before=0.6, after=0.6),
        decide(conn, "cand_beta", promoted=True, before=0.6, after=0.8, soft_drops=1),
        decide(conn, "cand_gamma", promoted=False, before=0.8, after=0.7, regressions=2),
        decide(conn, "cand_alpha", promoted=True, before=0.8, after=0.9),
    ]


# ---- AC 7: decisions across candidates, in decided order, fully typed ----------------------------


def test_promotions_lists_every_decision_across_candidates_in_decided_order(
    conn: sqlite3.Connection,
) -> None:
    seeded = seed_history(conn)

    history = procedural.promotions(conn, limit=10)

    assert isinstance(history, tuple)
    assert list(history) == seeded
    assert [row.cand_id for row in history] == [
        "cand_alpha",
        "cand_beta",
        "cand_gamma",
        "cand_alpha",
    ]
    assert [row.decided_at for row in history] == sorted(row.decided_at for row in history)


def test_each_row_carries_both_scores_the_regression_count_and_any_soft_drop(
    conn: sqlite3.Connection,
) -> None:
    seed_history(conn)

    history = procedural.promotions(conn, limit=10)

    assert all(isinstance(row, procedural.Promotion) for row in history)
    assert all(dataclasses.is_dataclass(row) and not isinstance(row, dict) for row in history)
    assert [(row.score_before, row.score_after) for row in history] == [
        (0.6, 0.6),
        (0.6, 0.8),
        (0.8, 0.7),
        (0.8, 0.9),
    ]
    assert [row.regressions for row in history] == [0, 0, 2, 0]
    assert [row.soft_drops for row in history] == [0, 1, 0, 0]
    assert [row.promoted for row in history] == [False, True, False, True]
    with pytest.raises(dataclasses.FrozenInstanceError):
        history[0].promoted = True  # type: ignore[misc]

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "ticket 066 - memory/procedural.py::promotions, every decision in decided order",
        "",
        *(
            f"{row.decided_at}  {row.cand_id:<11} {'promoted' if row.promoted else 'rejected':<9} "
            f"score {row.score_before:.2f} -> {row.score_after:.2f}  "
            f"regressions {row.regressions}  soft_drops {row.soft_drops}  {row.reason}"
            for row in history
        ),
        "",
    ]
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")


def test_the_limit_bounds_the_history(conn: sqlite3.Connection) -> None:
    seeded = seed_history(conn)

    assert list(procedural.promotions(conn, limit=2)) == seeded[:2]
    assert len(procedural.promotions(conn, limit=100)) == 4


# ---- AC 8: the single-candidate lookup is unchanged and unwidened --------------------------------


def test_promotion_still_returns_the_first_decision_for_one_candidate(
    conn: sqlite3.Connection,
) -> None:
    seeded = seed_history(conn)
    first_alpha, _beta, _gamma, second_alpha = seeded

    recorded = procedural.promotion(conn, "cand_alpha")

    assert recorded == first_alpha
    assert recorded != second_alpha
    assert recorded is not None and recorded.promoted is False
    assert procedural.promotion(conn, "cand_nobody") is None


# ---- AC 9: an empty store lists nothing, never None and never an error ---------------------------


def test_an_empty_store_lists_nothing(conn: sqlite3.Connection) -> None:
    history = procedural.promotions(conn, limit=10)

    assert history == ()
    assert isinstance(history, tuple)
