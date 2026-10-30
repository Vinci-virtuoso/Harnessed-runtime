"""Build the wheel once, install it offline, hand tests the installed interpreter (ticket 314).

Decision 106: a test of the installed package never imports from the checkout. It runs the
interpreter of a venv that holds only the built wheel, so a file the wheel forgot to ship, or a
path that still walks back to a repository root (MUST rule 43), fails here and not on an operator's
machine. Tickets 315 and 329 import :func:`installed_wheel` the same way.

Everything is ``--offline``: the build backend and the wheel's dependencies come from uv's cache, so
the fixture opens no socket of its own and the session socket guard stays on in the test process.
The session needs a warm uv cache (any prior ``uv sync`` provides one).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

__all__ = ["installed_wheel", "package_dir", "run_installed", "scrubbed_env"]

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The variables the installed harness reads, cleared so the host's setup cannot leak into it.
_CLEARED_PREFIX = "HARNESSED_"


def _uv(*argv: str, cwd: Path) -> None:
    completed = subprocess.run(["uv", *argv], capture_output=True, text=True, cwd=cwd, check=False)
    assert completed.returncode == 0, (
        f"uv {' '.join(argv)} exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


@pytest.fixture(scope="session")
def installed_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The interpreter of a venv holding only the freshly built wheel."""
    root = tmp_path_factory.mktemp("installed-wheel")
    dist = root / "dist"
    _uv("build", "--wheel", "--offline", "--out-dir", str(dist), str(REPO_ROOT), cwd=root)
    wheels = sorted(dist.glob("harnessed_runtime-*.whl"))
    assert len(wheels) == 1, f"expected one wheel in {dist}, found {wheels}"

    venv = root / "venv"
    # The venv runs the interpreter that runs the tests: the project pins one minor version.
    _uv("venv", "--python", sys.executable, str(venv), cwd=root)
    interpreter = (
        venv
        / ("Scripts" if os.name == "nt" else "bin")
        / ("python.exe" if os.name == "nt" else "python")
    )
    _uv("pip", "install", "--offline", "--python", str(interpreter), str(wheels[0]), cwd=root)
    return interpreter


def scrubbed_env(home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The host environment minus every ``HARNESSED_*`` variable, plus the throwaway home."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_CLEARED_PREFIX)}
    env["HARNESSED_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # the package may be read-only
    env.update(extra or {})
    return env


def package_dir(interpreter: Path) -> Path:
    """Where the installed ``harnessed`` package lives, asked of the installed interpreter."""
    completed = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-c",
            "import harnessed, pathlib; " "print(pathlib.Path(harnessed.__file__).parent)",
        ],
        capture_output=True,
        text=True,
        cwd=interpreter.parent,
        check=True,
    )
    return Path(completed.stdout.strip())


def run_installed(
    interpreter: Path, argv: list[str], *, cwd: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """``python -m harnessed <argv>`` through the installed interpreter, from an empty ``cwd``."""
    return subprocess.run(
        [str(interpreter), "-m", "harnessed", *argv],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=dict(env),
        check=False,
    )
