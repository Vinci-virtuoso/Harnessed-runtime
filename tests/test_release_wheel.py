"""The release wheel carries what ships and nothing else (ticket 337, FR-187; decisions 90, 97).

The wheel is the one CI built when `HARNESSED_WHEEL` names it (the release path), else the session
fixture's fresh build. It is opened with `zipfile`, never installed here.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
import zipfile
from pathlib import Path

import pytest

from support.wheel import installed_wheel

__all__ = ["installed_wheel"]  # re-exported so pytest finds the session fixture in this module

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
CORPUS = "harnessed/corpus/"
WALKTHROUGHS = ("evals.md", "harness.md", "loops.md", "memory.md")
LICENCES = ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")


@pytest.fixture(scope="module")
def wheel(installed_wheel: Path) -> zipfile.ZipFile:
    given = os.environ.get("HARNESSED_WHEEL")
    if given:
        path = Path(given)
    else:
        (path,) = sorted((installed_wheel.parents[2] / "dist").glob("harnessed_runtime-*.whl"))
    return zipfile.ZipFile(path)


def _metadata(wheel: zipfile.ZipFile) -> list[str]:
    (name,) = (n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
    return wheel.read(name).decode("utf-8").splitlines()


def test_the_version_is_written_once_and_is_the_first_public_one() -> None:
    assert VERSION == "0.7.0"


def test_the_metadata_names_the_version_and_the_licence(wheel: zipfile.ZipFile) -> None:
    metadata = _metadata(wheel)
    assert "Name: harnessed-runtime" in metadata
    assert f"Version: {VERSION}" in metadata
    assert "License-Expression: Apache-2.0" in metadata


def test_the_licence_files_are_in_dist_info(wheel: zipfile.ZipFile) -> None:
    found = {n.split(".dist-info/licenses/", 1)[1] for n in wheel.namelist() if "/licenses/" in n}
    assert set(LICENCES) <= found


def test_the_dashboard_export_ships(wheel: zipfile.ZipFile) -> None:
    names = set(wheel.namelist())
    assert "harnessed/dashboard_static/index.html" in names
    assert "harnessed/dashboard_static/.stamp" in names


def test_every_manifest_entry_ships_with_its_hash(wheel: zipfile.ZipFile) -> None:
    manifest = wheel.read(CORPUS + "MANIFEST.sha256").decode("utf-8").splitlines()
    assert manifest
    for line in manifest:
        digest, relative = line.split("  ", 1)
        body = wheel.read(CORPUS + relative).replace(b"\r\n", b"\n")
        assert hashlib.sha256(body).hexdigest() == digest, relative


def test_the_four_walkthroughs_ship(wheel: zipfile.ZipFile) -> None:
    names = set(wheel.namelist())
    assert {f"{CORPUS}walkthroughs/{name}" for name in WALKTHROUGHS} <= names


def test_nothing_from_the_checkout_rides_along(wheel: zipfile.ZipFile) -> None:
    names = wheel.namelist()
    assert [n for n in names if n.startswith(("tests/", ".spec/", "evidence/"))] == []
    assert [n for n in names if "__pycache__" in n or n.endswith(".pyc")] == []


def test_distribution_identity_preserves_the_cli_and_import_package() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "harnessed-runtime"
    assert project["scripts"] == {"harnessed": "harnessed.cli.app:main"}
