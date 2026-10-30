"""The four-command thesis runs from an installed wheel with no checkout (ticket 314, FR-176).

The North Star: in an empty folder that is not a checkout, the installed ``harnessed`` runs
``eval run smoke --fresh``, ``improve run --dry-run``, ``improve promote <candidate>`` and
``eval run smoke --fresh`` again, and prints 0.8 then 1.0, 0 regressions and the ``provider=fake``
caveat. The candidate id is compared with the one ``tests/test_e2e_flywheel.py`` produces in
process (its ``test_scores_reproduce`` pins that the id is content-addressed and reproducible), so
the install and the checkout cannot drift apart. ``evidence/`` is unpublished and is never the
source of an expected value.

``test_proof_can_fail`` is repeated from the install: the reflector is replaced inside the child
(the one seam ``cli/improve_cmd.py`` documents) and the score must not move.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessed.improve import promoter
from support.stub_reflector import VAGUE
from support.wheel import installed_wheel, package_dir, run_installed, scrubbed_env
from test_e2e_flywheel import four_commands

__all__ = ["installed_wheel"]  # re-exported so pytest finds the session fixture in this module

pytestmark = pytest.mark.slow

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "evidence" / "314"

#: Runs ``harnessed`` with the reflection model replaced by the vague one, inside the installed
#: interpreter. Everything else (corpus, graders, arithmetic, CLI) is the installed code.
FALSIFY = textwrap.dedent(
    r"""
    import sys
    from harnessed.cli import improve_cmd
    from harnessed.cli.app import main
    from harnessed.providers.types import ChatResponse, Usage

    TEXT = sys.argv.pop(1)

    class Vague:
        model = "fake-1"
        def complete(self, request):
            return ChatResponse(response_id="stub_reflect_vague", model=self.model,
                content=TEXT, stop_reason="end_turn",
                usage=Usage(input_tokens=1, output_tokens=1))

    improve_cmd.reflector = lambda _session: Vague()
    sys.argv[0] = "harnessed"
    main()
    """
)


def _sequence(interpreter: Path, home: Path, cwd: Path, *, falsify: bool) -> dict[str, object]:
    """The four commands through the installed interpreter; returns what they printed."""
    env = scrubbed_env(home, {"HARNESSED_PROVIDER": "fake"})

    def issue(argv: list[str], *, injected: bool = False) -> subprocess.CompletedProcess[str]:
        if injected:
            done = subprocess.run(
                [str(interpreter), "-c", FALSIFY, VAGUE, *argv],
                capture_output=True,
                text=True,
                cwd=cwd,
                env=env,
                check=False,
            )
        else:
            done = run_installed(interpreter, argv, cwd=cwd, env=env)
        assert (
            done.returncode == 0
        ), f"{argv} exited {done.returncode}\n{done.stdout}\n{done.stderr}"
        return done

    before = json.loads(issue(["eval", "run", "smoke", "--fresh", "--json"]).stdout)
    cycle = json.loads(issue(["improve", "run", "--dry-run", "--json"], injected=falsify).stdout)
    promote = issue(["improve", "promote", cycle["cand_id"]]).stdout
    after = json.loads(issue(["eval", "run", "smoke", "--fresh", "--json"]).stdout)
    return {"before": before, "cycle": cycle, "promote": promote, "after": after}


@pytest.fixture
def work(tmp_path: Path) -> tuple[Path, Path]:
    """An empty folder that is not a checkout, and an empty home beside it."""
    cwd = tmp_path / "empty"
    cwd.mkdir()
    home = tmp_path / "home" / ".harnessed"
    home.mkdir(parents=True)
    return home, cwd


def test_the_installed_thesis_runs_from_an_empty_folder(
    installed_wheel: Path, work: tuple[Path, Path], clean_env: None, tmp_harnessed_home: Path
) -> None:
    home, cwd = work
    seen = _sequence(installed_wheel, home, cwd, falsify=False)

    assert seen["before"]["score"] == 0.8
    assert seen["after"]["score"] == 1.0
    assert seen["after"]["fresh"] is True
    assert "regressions: 0" in seen["promote"]
    assert "promoted" in seen["promote"]
    assert promoter.CAVEAT in seen["promote"]
    assert seen["cycle"]["provider"] == "fake"

    # The id is the one the in-process proof in test_e2e_flywheel.py produces.
    assert seen["cycle"]["cand_id"] == four_commands().cand_id

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "installed-thesis.txt").write_text(
        "314 - the four commands from the installed wheel\n\n"
        f"1. eval run smoke --fresh       -> score {seen['before']['score']}\n"
        f"2. improve run --dry-run        -> candidate {seen['cycle']['cand_id']}\n"
        f"3. improve promote\n{seen['promote']}\n"
        f"4. eval run smoke --fresh       -> score {seen['after']['score']}\n",
        encoding="utf-8",
    )


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def read_only_package(installed_wheel: Path) -> Iterator[Path]:
    """The installed ``harnessed/`` with write bits removed, restored afterwards."""
    root = package_dir(installed_wheel)
    paths = [root, *root.rglob("*")]
    modes = {p: p.stat().st_mode for p in paths}
    for p in paths:
        p.chmod(modes[p] & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        yield root
    finally:
        for p in paths:
            p.chmod(modes[p])


def test_the_thesis_passes_with_the_installed_package_read_only(
    installed_wheel: Path,
    read_only_package: Path,
    work: tuple[Path, Path],
    clean_env: None,
    tmp_harnessed_home: Path,
) -> None:
    """Writes go to the home. Root ignores mode bits, so the tree is also compared afterwards."""
    home, cwd = work
    snapshot = _tree(read_only_package)
    seen = _sequence(installed_wheel, home, cwd, falsify=False)
    assert (seen["before"]["score"], seen["after"]["score"]) == (0.8, 1.0)
    assert _tree(read_only_package) == snapshot
    assert any(home.iterdir()), "the run wrote nothing to the home"
    assert os.listdir(cwd) == []


def test_proof_can_fail_from_the_install(
    installed_wheel: Path, work: tuple[Path, Path], clean_env: None, tmp_harnessed_home: Path
) -> None:
    home, cwd = work
    seen = _sequence(installed_wheel, home, cwd, falsify=True)
    assert seen["before"]["score"] == seen["after"]["score"] == 0.8
    assert "rejected" in seen["promote"]
    assert "no gain" in seen["promote"] or "no_gain" in seen["promote"]
    assert "promoted" not in seen["promote"].replace("not promoted", "")
