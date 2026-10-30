"""FR-183: ``harnessed learn`` lists the four pillars and prints one walkthrough (decision 106)."""

from __future__ import annotations

from typer.testing import CliRunner

from harnessed import shipped
from harnessed.cli.app import app

PILLARS = ("loops", "evals", "memory", "harness")


def test_bare_learn_lists_the_four_pillars_with_paths() -> None:
    result = CliRunner().invoke(app, ["learn"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    for pillar in PILLARS:
        matching = [ln for ln in lines if ln.lstrip().startswith(pillar)]
        assert len(matching) == 1, (pillar, result.output)
        assert str(shipped.walkthroughs_dir() / f"{pillar}.md") in matching[0].replace("\n", "")


def test_learn_pillar_prints_text_and_path() -> None:
    result = CliRunner().invoke(app, ["learn", "evals"])
    assert result.exit_code == 0, result.output
    path = shipped.walkthroughs_dir() / "evals.md"
    assert "# Evals: a score that moves only when the work got better" in result.output
    assert str(path) in result.output.replace("\n", "")


def test_unknown_pillar_exits_one_naming_the_four() -> None:
    result = CliRunner().invoke(app, ["learn", "bogus"])
    assert result.exit_code == 1
    for pillar in PILLARS:
        assert pillar in result.output
