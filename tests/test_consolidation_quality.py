"""The consolidation quality set runs offline through ``harnessed memory consolidate --quality``.

Ticket 297, FR-169, decision 98. The set says which facts a correct batch writes and which it must
not; it is not an ``eval run`` suite, because FR-149 forbids consolidation inside ``eval run``.
Offline it replays its own ``wire/`` directory, one scope ``_consolidate-<case id>`` per case.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from harnessed.cli.app import app
from harnessed.memory import db, redact

pytestmark = pytest.mark.usefixtures("clean_env")

REPO_ROOT = Path(__file__).resolve().parents[1]
SET = REPO_ROOT / "tests" / "fixtures" / "consolidation-quality" / "set.yaml"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "297"
SUITE = "consolidation-quality"

#: The four classes of fact a correct batch never writes (FR-169), as the set names them.
FORBIDDEN_CLASSES = (
    "a one-off task outcome",
    "general knowledge",
    "an agent's work credited to the operator",
    "a fact naming no repository",
)

runner = CliRunner()


def load_set(path: Path = SET) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def quality(*arguments: str, path: Path = SET) -> Any:
    return runner.invoke(app, ["memory", "consolidate", "--quality", str(path), *arguments])


def runs(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT eval_run_id, finished_at FROM eval_runs WHERE suite = ? ORDER BY rowid", (SUITE,)
    ).fetchall()


# ---- AC 1: the set ---------------------------------------------------------------------------


def test_the_set_holds_five_cases_covering_the_four_forbidden_classes() -> None:
    cases = load_set()["cases"]
    assert len(cases) >= 5
    assert len({case["id"] for case in cases}) == len(cases)
    named = " | ".join(case["forbidden_class"] for case in cases)
    for forbidden in FORBIDDEN_CLASSES:
        assert forbidden in named
    for case in cases:
        assert "qa-v6/" in case["source"], case["id"]
        assert case["episodes"] and case["must_write"] and case["must_not_write"], case["id"]
        assert "awaiting the operator" in case["approval"], case["id"]
        for pattern in case["must_not_write"]:
            re.compile(pattern)
        for episode in case["episodes"]:
            assert redact.scrub(episode["text"]) == episode["text"], case["id"]


def test_each_case_has_its_own_wire_scope() -> None:
    wire = SET.parent / "wire"
    for case in load_set()["cases"]:
        scope = wire / f"_consolidate-{case['id']}"
        assert (scope / "_index.yaml").is_file() and (scope / "base.json").is_file(), case["id"]


# ---- AC 2 and 3: the offline run -------------------------------------------------------------


def test_the_offline_run_scores_the_set_and_writes_no_forbidden_fact(
    tmp_harnessed_home: Path,
) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence = EVIDENCE_DIR / "quality.txt"
    result = quality("--json", "--evidence", str(evidence))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    cases = load_set()["cases"]
    assert payload["suite"] == SUITE
    assert [item["id"] for item in payload["cases"]] == [case["id"] for case in cases]
    assert payload["forbidden"] == 0
    assert all(item["forbidden"] == 0 and item["facts"] >= 1 for item in payload["cases"])
    assert payload["score"] == 1.0

    plain = quality()
    assert plain.exit_code == 0 and "score" in plain.output and "forbidden 0" in plain.output
    assert "score" in evidence.read_text(encoding="utf-8")

    conn = db.connect()
    try:
        assert runs(conn)
        scored = conn.execute(
            "SELECT case_id, passed FROM eval_results WHERE eval_run_id = ? ORDER BY case_id",
            (payload["eval_run_id"],),
        ).fetchall()
        assert [row[0] for row in scored] == sorted(case["id"] for case in cases)
        assert all(row[1] == 1 for row in scored)
        # Every batch ran in a scratch home: the operator's own ledger holds none.
        assert conn.execute("SELECT COUNT(*) FROM consolidation_batches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mem_semantic").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_second_run_resumes_and_fresh_starts_another(tmp_harnessed_home: Path) -> None:
    first = json.loads(quality("--json").output)
    again = json.loads(quality("--json").output)
    assert again["eval_run_id"] == first["eval_run_id"]
    fresh = json.loads(quality("--json", "--fresh").output)
    assert fresh["eval_run_id"] != first["eval_run_id"]
    conn = db.connect()
    try:
        assert len(runs(conn)) == 2
    finally:
        conn.close()


def test_a_reply_that_writes_a_forbidden_fact_is_counted_and_fails_its_case(
    tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    copy = tmp_path / "set"
    shutil.copytree(SET.parent, copy)
    case = load_set()["cases"][0]["id"]
    reply = copy / "wire" / f"_consolidate-{case}" / "base.json"
    document = json.loads(reply.read_text(encoding="utf-8"))
    facts = json.loads(document["content"])["facts"]
    facts.append(
        {**facts[0], "text": "The operator appended a line to the notes and committed it."}
    )
    document["content"] = json.dumps({"facts": facts})
    reply.write_text(json.dumps(document), encoding="utf-8")
    result = quality("--json", path=copy / "set.yaml")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["forbidden"] == 1
    assert payload["score"] < 1.0
    assert payload["cases"][0]["passed"] is False


def test_a_missing_set_fails_in_words(tmp_harnessed_home: Path, tmp_path: Path) -> None:
    result = quality(path=tmp_path / "absent.yaml")
    assert result.exit_code != 0
    assert "Traceback" not in result.output
