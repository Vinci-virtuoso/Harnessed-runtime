"""AC 4 and AC 5 - where an eval run's results land, and what happens on the second run.

``results.py`` is the only module in this suite that names the results directory, and it resolves
it from ``Settings.home`` rather than from a literal, so ``HARNESSED_HOME`` moves it. That is what
lets a run against a ``tmp_path`` write only there and nothing into the repository tree.

The second-run rule is the one worth stating out loud: a new run writes a **new** file. Results are
evidence, and evidence that overwrites its predecessor cannot show a change.

There is no live tier in this file. A writer writes the same bytes whichever tier produced them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import results

# ---- offline tier ------------------------------------------------------------------------------


def cases() -> tuple[results.CaseResult, ...]:
    return (
        results.CaseResult(
            id="eval_write_note",
            tier="offline",
            passed=True,
            expected="fs_write",
            observed="fs_write",
            detail="one call, arguments carried",
        ),
        results.CaseResult(
            id="eval_plain_answer",
            tier="offline",
            passed=False,
            expected="no tool",
            observed="fs_read",
            detail="the model reached for a tool it was told not to use",
        ),
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HARNESSED_HOME", str(home))
    return home


def test_the_results_directory_is_created_on_the_first_run(isolated_home: Path) -> None:
    assert not isolated_home.exists()
    results.write(cases())
    assert (isolated_home / results.RESULTS_DIRNAME).is_dir()


def test_one_json_object_per_case_with_the_six_declared_keys(isolated_home: Path) -> None:
    path = results.write(cases())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert set(first) == {"id", "tier", "passed", "expected", "observed", "detail"}
    assert first["id"] == "eval_write_note"
    assert first["tier"] == "offline"
    assert first["passed"] is True


def test_the_file_is_named_for_the_eval_id_it_records(isolated_home: Path) -> None:
    path = results.write(cases())
    assert path.suffix == ".jsonl"
    assert path.stem.startswith("eval_")


def test_a_summary_of_the_counts_sits_beside_the_lines(isolated_home: Path) -> None:
    results.write(cases())
    summary = json.loads(
        (isolated_home / results.RESULTS_DIRNAME / results.SUMMARY_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["by_tier"]["offline"] == 2


def test_a_second_run_adds_a_file_rather_than_overwriting_the_first(isolated_home: Path) -> None:
    first = results.write(cases())
    second = results.write(cases())
    assert first != second
    written = sorted((isolated_home / results.RESULTS_DIRNAME).glob("*.jsonl"))
    assert len(written) == 2


def test_a_run_against_a_tmp_home_writes_only_there(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing lands in the repository tree - the reason the writer never spells a literal path."""
    repo_root = Path(__file__).resolve().parents[2]
    before = {path.name for path in repo_root.iterdir()}
    monkeypatch.chdir(tmp_path)
    results.write(cases())
    assert {path.name for path in repo_root.iterdir()} == before
    assert list((isolated_home / results.RESULTS_DIRNAME).glob("*.jsonl"))


def test_every_recorded_tier_is_one_of_the_three_declared_names() -> None:
    assert results.TIERS == ("offline", "live", "judge")


def test_an_undeclared_tier_is_refused_rather_than_silently_recorded(isolated_home: Path) -> None:
    with pytest.raises(results.ResultError):
        results.write(
            (
                results.CaseResult(
                    id="eval_bogus", tier="vibes", passed=True, expected="x", observed="x"
                ),
            )
        )
