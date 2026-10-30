"""FR-175: the installed ``harnessed`` prints its version and its home and takes ``--provider``.

Ticket 315 (decisions 89, 90, 91). The version is the installed metadata's and nothing in ``src/``
carries a second copy; ``doctor`` names the absolute home it resolved and the ``.env`` it read; the
three thesis verbs take ``--provider`` through ``config.PROVIDER_FLAG`` exactly as ``run`` does; and
the installed wheel carries the dashboard export. The wheel tests use the ``installed_wheel``
fixture (decision 106), so nothing here imports from the checkout inside the child.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harnessed import config, dashboard_app
from harnessed.cli.app import app, main
from harnessed.errors import ExportMissing
from support.wheel import installed_wheel, package_dir, run_installed, scrubbed_env

__all__ = ["installed_wheel"]  # re-exported so pytest finds the session fixture in this module

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The one place the version is written (ticket 337); the installed wheel must print it.
PYPROJECT_VERSION = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]
SRC = REPO_ROOT / "src" / "harnessed"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "315"

split = CliRunner(mix_stderr=False)


def evidence(name: str, text: str) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / name).write_text(text, encoding="utf-8")


def test_version_prints_the_installed_metadata_version() -> None:
    seen = split.invoke(app, ["--version"])
    assert seen.exit_code == 0
    assert seen.stdout.strip() == version("harnessed-runtime")


def test_no_version_literal_remains_in_src() -> None:
    pattern = re.compile(r"""^\s*__version__\s*(?::[^=]+)?=\s*["']""", re.MULTILINE)
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in SRC.rglob("*.py")
        if pattern.search(p.read_text("utf-8"))
    ]
    assert offenders == []


def test_doctor_names_the_resolved_home_and_the_env_file(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("HARNESSED_PROVIDER=fake\n", encoding="utf-8")
    seen = split.invoke(app, ["doctor", "--offline"])
    lines = seen.stdout.splitlines()
    assert lines[0] == f"home: {tmp_harnessed_home.resolve()}"
    assert lines[1] == f"env: {(tmp_path / '.env').resolve()}"
    assert Path(lines[0].removeprefix("home: ")).is_absolute()


def test_doctor_json_serves_home_and_env_file(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("HARNESSED_PROVIDER=fake\n", encoding="utf-8")
    payload = json.loads(split.invoke(app, ["doctor", "--offline", "--json"]).stdout)
    assert payload["home"] == str(tmp_harnessed_home.resolve())
    assert payload["env_file"] == str((tmp_path / ".env").resolve())


def test_doctor_without_an_env_file_says_so(clean_env: None, tmp_harnessed_home: Path) -> None:
    seen = split.invoke(app, ["doctor", "--offline"])
    assert seen.stdout.splitlines()[1] == "env: no .env in this folder"
    payload = json.loads(split.invoke(app, ["doctor", "--offline", "--json"]).stdout)
    assert payload["env_file"] is None


@pytest.mark.parametrize(
    "argv", [["eval", "run"], ["improve", "run"], ["improve", "promote"]], ids=" ".join
)
def test_thesis_verbs_take_the_provider_flag(argv: list[str]) -> None:
    seen = split.invoke(app, [*argv, "--help"], terminal_width=200)
    assert seen.exit_code == 0
    assert config.PROVIDER_FLAG in seen.stdout


def test_provider_flag_resolves_through_config(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HARNESSED_PROVIDER")
    seen = split.invoke(app, ["eval", "run", "smoke", "--fresh", "--json", "--provider", "fake"])
    assert seen.exit_code == 0, seen.stdout + seen.stderr
    assert json.loads(seen.stdout)["score"] == 0.8
    improved = split.invoke(app, ["improve", "run", "--dry-run", "--json", "--provider", "fake"])
    assert improved.exit_code == 0, improved.stdout + improved.stderr
    cand = json.loads(improved.stdout)["cand_id"]
    promoted = split.invoke(app, ["improve", "promote", cand, "--provider", "fake"])
    assert promoted.exit_code == 0, promoted.stdout + promoted.stderr


@pytest.fixture
def work(tmp_path: Path) -> tuple[Path, Path]:
    cwd = tmp_path / "empty"
    cwd.mkdir()
    home = tmp_path / "home" / ".harnessed"
    home.mkdir(parents=True)
    return home, cwd


@pytest.mark.slow
def test_the_installed_wheel_prints_its_version_and_checks_out_offline(
    installed_wheel: Path, work: tuple[Path, Path], clean_env: None, tmp_harnessed_home: Path
) -> None:
    home, cwd = work
    env = scrubbed_env(home, {"HARNESSED_PROVIDER": "fake"})

    shown = run_installed(installed_wheel, ["--version"], cwd=cwd, env=env)
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout.strip() == version("harnessed-runtime") == PYPROJECT_VERSION

    doctor = run_installed(installed_wheel, ["doctor", "--offline"], cwd=cwd, env=env)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert doctor.stdout.splitlines()[0] == f"home: {home.resolve()}"
    assert doctor.stdout.splitlines()[1] == "env: no .env in this folder"

    export = package_dir(installed_wheel) / dashboard_app.EXPORT_DIRECTORY
    assert (
        export / "index.html"
    ).is_file(), "no dashboard export in the wheel: run `npm --prefix dashboard run build` first"
    assert (export / ".stamp").is_file()

    evidence(
        "install-path.txt",
        f"$ harnessed --version\n{shown.stdout}\n$ harnessed doctor --offline\n{doctor.stdout}\n"
        f"export: {export / 'index.html'} and .stamp present\n",
    )


def test_a_checkout_with_no_export_staged_exits_with_export_missing(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    absent = tmp_path / "no-export"
    monkeypatch.setattr(dashboard_app, "export_directory", lambda: absent)
    monkeypatch.setattr(sys, "argv", ["harnessed", "dashboard"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert ExportMissing.remedy in err
    assert str(absent) in err
    assert "Traceback" not in err
