"""The repository carries the Apache-2.0 licence and the wheel says so (ticket 335; FR-186, FR-187).

Operator ruling Q1: Apache-2.0. `LICENSE` is the published text byte for byte, `NOTICE` holds the
attribution the operator named at the gate, and the PEP 639 metadata (decision 90) puts both into
the wheel's `dist-info/licenses/`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

#: sha256 of https://www.apache.org/licenses/LICENSE-2.0.txt, the published Apache-2.0 text.
APACHE_2_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
#: The attribution the operator wrote into ticket 335 at the gate (2026-10-13).
COPYRIGHT = "Copyright 2026 Vinci-Virtuoso"
REPOSITORY = "https://github.com/Vinci-virtuoso/Harnessed-runtime"
LICENSE_FILES = ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"]
SDIST_FILES = [
    "src/harnessed",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
    "pyproject.toml",
]
EXPORT_ARTIFACTS = ["src/harnessed/dashboard_static/**"]

uv = shutil.which("uv")


def test_licence_is_the_published_apache_2_text_verbatim() -> None:
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == APACHE_2_SHA256


def test_notice_names_the_project_and_the_copyright_holder() -> None:
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert notice.splitlines()[0] == "Harnessed"
    assert COPYRIGHT in notice
    assert "Apache License, Version 2.0" in notice


def test_project_metadata_declares_the_licence_by_pep_639() -> None:
    project = PYPROJECT["project"]
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == LICENSE_FILES
    assert project["authors"]
    assert "Operating System :: OS Independent" in project["classifiers"]
    assert "Programming Language :: Python :: 3.12" in project["classifiers"]
    assert REPOSITORY in project["urls"].values()


def test_the_build_backend_reads_pep_639_metadata() -> None:
    assert "hatchling>=1.27" in PYPROJECT["build-system"]["requires"]


def test_the_sdist_carries_only_the_package_and_its_legal_files() -> None:
    sdist = PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert sdist["only-include"] == SDIST_FILES
    assert sdist["artifacts"] == EXPORT_ARTIFACTS


def test_the_readme_names_the_licence() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Licence", 1)[1].split("\n## ", 1)[0]
    assert "Apache License, Version 2.0" in section
    assert "](LICENSE)" in section


@pytest.mark.skipif(uv is None, reason="uv is not on PATH")
def test_the_wheel_carries_the_licence_expression_and_files(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    built = subprocess.run(
        [str(uv), "build", "--wheel", "--out-dir", str(dist)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    (wheel_path,) = dist.glob("*.whl")
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        (metadata,) = (n for n in names if n.endswith(".dist-info/METADATA"))
        text = wheel.read(metadata).decode("utf-8")
    assert "License-Expression: Apache-2.0" in text.splitlines()
    licences = {n.split(".dist-info/licenses/", 1)[1] for n in names if ".dist-info/licenses/" in n}
    assert {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"} <= licences


@pytest.mark.skipif(uv is None, reason="uv is not on PATH")
def test_no_runtime_dependency_moved() -> None:
    checked = subprocess.run(
        [str(uv), "lock", "--check", "--offline"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


# ---- 336: third-party notices from the two lockfiles ---------------------------------------------

NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
#: Bundled npm licences (decision 108, operator ruling Q1).
BUNDLED_ALLOWED = {"MIT", "ISC", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "OFL-1.1"}
#: Python licences: the ticket's MIT, BSD (any clause count), Apache-2.0, PSF-2.0 and MPL-2.0, plus
#: ISC, which `shellingham` carries and the bundled list already admits (flagged to the operator).
PYTHON_ALLOWED = {
    "MIT",
    "BSD",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache-2.0",
    "PSF-2.0",
    "MPL-2.0",
    "ISC",
}
ROW = re.compile(r"^\| (\S+) \| (\S+) \| (.+?) \| (python|dashboard) \|$", re.M)


def _rows(where: str) -> dict[str, tuple[str, str]]:
    text = NOTICES.read_text(encoding="utf-8")
    return {
        name: (version, licence) for name, version, licence, w in ROW.findall(text) if w == where
    }


def _allowed(expression: str, allowed: set[str]) -> bool:
    return all(part in allowed for part in re.split(r" (?:OR|AND) ", expression))


def _runtime_closure() -> dict[str, str]:
    packages = {p["name"]: p for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}
    root = packages["harnessed-runtime"]
    todo = [d["name"] for d in [*root["dependencies"], *root["optional-dependencies"]["gateway"]]]
    seen: dict[str, str] = {}
    while todo:
        name = todo.pop()
        if name not in seen:
            seen[name] = packages[name]["version"]
            todo += [d["name"] for d in packages[name].get("dependencies", [])]
    return seen


def test_the_notices_list_every_runtime_python_package_at_its_locked_version() -> None:
    listed = {name: version for name, (version, _) in _rows("python").items()}
    assert listed == _runtime_closure()


def test_no_dev_or_eval_package_is_listed_as_runtime() -> None:
    assert not {"pytest", "ruff", "mypy", "deepeval"} & set(_rows("python"))


def test_every_python_licence_is_on_the_allowlist() -> None:
    refused = {
        n: lic for n, (_, lic) in _rows("python").items() if not _allowed(lic, PYTHON_ALLOWED)
    }
    assert refused == {}


def test_every_bundled_licence_is_on_the_allowlist() -> None:
    refused = {
        n: lic for n, (_, lic) in _rows("dashboard").items() if not _allowed(lic, BUNDLED_ALLOWED)
    }
    assert refused == {}


def test_the_bundled_list_is_the_lockfiles_non_dev_set_at_its_locked_versions() -> None:
    lock = json.loads((ROOT / "dashboard" / "package-lock.json").read_text(encoding="utf-8"))
    rows = _rows("dashboard")
    for name, (version, _) in rows.items():
        assert lock["packages"][f"node_modules/{name}"]["version"] == version, name
        assert not lock["packages"][f"node_modules/{name}"].get("dev"), name


@pytest.mark.skipif(
    not (ROOT / "dashboard" / "node_modules").is_dir(), reason="ci: build (needs npm ci first)"
)
def test_the_notices_file_is_what_the_script_writes_now() -> None:
    spec = importlib.util.spec_from_file_location("notices", ROOT / "scripts" / "notices.py")
    assert spec and spec.loader
    notices = importlib.util.module_from_spec(spec)
    sys.modules["notices"] = notices  # a slotted dataclass resolves its module by name
    spec.loader.exec_module(notices)
    python = [notices.python_entry(n, *locked) for n, locked in notices.runtime_closure().items()]
    npm = [notices.npm_entry(path, locked) for path, locked in notices.bundled()]
    assert notices.render(python, npm) == NOTICES.read_text(encoding="utf-8")
