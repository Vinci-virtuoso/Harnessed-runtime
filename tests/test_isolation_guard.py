"""FR-159 - no test reads the operator's setup (decision 71, MUST rule 35).

Each case runs an inner pytest session (``pytester``, in a subprocess so the audit hook and the
session-start ``HARNESSED_HOME`` are the inner run's own) with ``tests.support.isolation`` as a
plugin. A guard that is only ever shown source which already obeys would pass against anything, so
every rule has a probe that breaks it and one that does not.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest
from tests.support import isolation

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parents[1]

_CONFTEST = """
import pytest
from pathlib import Path
from tests.support.isolation import isolation_guard  # noqa: F401


@pytest.fixture
def tmp_harnessed_home(tmp_path, monkeypatch):
    home = tmp_path / ".harnessed"
    home.mkdir()
    monkeypatch.setenv("HARNESSED_HOME", str(home))
    return home
"""


def _inner(pytester: pytest.Pytester, body: str, **env: str) -> pytest.RunResult:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(textwrap.dedent(body))
    for name, value in env.items():
        pytester._monkeypatch.setenv(name, value)
    pytester._monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(REPO_ROOT), os.environ.get("PYTHONPATH", "")])
    )
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly")


READS_REAL_AGENTS_TOML = f"""
from pathlib import Path

REAL = Path({str(REPO_ROOT)!r}) / "agents.toml"


def test_probe():
    REAL.read_text(encoding="utf-8")
"""


def test_a_probe_reading_the_real_agents_toml_fails_with_isolation_breach(
    pytester: pytest.Pytester,
) -> None:
    result = _inner(pytester, READS_REAL_AGENTS_TOML)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*IsolationBreach*"])
    assert "agents.toml" in result.stdout.str()
    assert "test_probe" in result.stdout.str()


def test_the_same_probe_under_its_own_home_passes(pytester: pytest.Pytester) -> None:
    body = """
    def test_probe(tmp_harnessed_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "agents.toml").write_text("[agents]\\n", encoding="utf-8")
        (tmp_path / "agents.toml").read_text(encoding="utf-8")
        (tmp_harnessed_home / "harness.db").write_text("x", encoding="utf-8")
    """
    _inner(pytester, body).assert_outcomes(passed=1)


def test_a_broken_agents_toml_in_a_fake_real_home_changes_no_result(
    pytester: pytest.Pytester, tmp_path_factory: pytest.TempPathFactory
) -> None:
    body = """
    import os

    def test_probe(tmp_harnessed_home):
        assert not (tmp_harnessed_home / "agents.toml").exists()

    def test_reads_its_own(tmp_harnessed_home):
        (tmp_harnessed_home / "agents.toml").write_text("ok = 1", encoding="utf-8")
        assert "ok" in (tmp_harnessed_home / "agents.toml").read_text(encoding="utf-8")
    """
    fake_home = tmp_path_factory.mktemp("fake-real") / ".harnessed"
    fake_home.mkdir()
    clean = _inner(pytester, body, HARNESSED_HOME=str(fake_home))
    (fake_home / "agents.toml").write_text("[[[ not toml", encoding="utf-8")
    (fake_home / "harness.db").write_bytes(b"not a database")
    broken = _inner(pytester, body, HARNESSED_HOME=str(fake_home))
    assert clean.parseoutcomes() == broken.parseoutcomes()
    clean.assert_outcomes(passed=2)


def test_the_session_start_harnessed_home_is_protected(
    pytester: pytest.Pytester, tmp_path_factory: pytest.TempPathFactory
) -> None:
    home = tmp_path_factory.mktemp("operator") / "home"
    home.mkdir()
    (home / "agents.toml").write_text("x = 1", encoding="utf-8")
    body = f"""
    from pathlib import Path

    def test_probe():
        (Path({str(home)!r}) / "agents.toml").read_text(encoding="utf-8")
    """
    result = _inner(pytester, body, HARNESSED_HOME=str(home))
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*IsolationBreach*"])


def test_a_sqlite_connect_to_the_repository_home_is_a_breach(pytester: pytest.Pytester) -> None:
    body = f"""
    import sqlite3

    def test_probe():
        sqlite3.connect({str(REPO_ROOT / ".harnessed" / "harness.db")!r})
    """
    result = _inner(pytester, body)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*IsolationBreach*"])


def test_a_live_test_is_not_guarded(pytester: pytest.Pytester) -> None:
    body = f"""
    import pytest
    from pathlib import Path

    @pytest.mark.live
    def test_probe():
        try:
            (Path({str(REPO_ROOT)!r}) / "agents.toml").read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
    """
    _inner(pytester, body).assert_outcomes(passed=1)


@pytest.mark.skipif(
    sys.platform == "win32", reason="a symlink needs a privilege on Windows; ci: linux job"
)
def test_a_symlinked_directory_cannot_hide_a_breach(
    pytester: pytest.Pytester, tmp_path_factory: pytest.TempPathFactory
) -> None:
    link = tmp_path_factory.mktemp("links") / "innocent"
    link.symlink_to(REPO_ROOT, target_is_directory=True)
    body = f"""
    from pathlib import Path

    def test_probe():
        (Path({str(link)!r}) / "agents.toml").read_text(encoding="utf-8")
    """
    result = _inner(pytester, body)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*IsolationBreach*"])


def test_a_case_and_separator_variant_of_a_protected_path_is_still_inside() -> None:
    protected = isolation._canonical(REPO_ROOT / ".harnessed")
    variant = str(REPO_ROOT / ".harnessed" / "x").replace("/", os.sep)
    assert isolation._inside(isolation._canonical(variant), protected)
    assert not isolation._inside(isolation._canonical(REPO_ROOT / "src"), protected)
    # The comparison is normcase(realpath(...)): on a case-insensitive host the upper-cased
    # spelling is the same path, and elsewhere it names nothing, so only the first can breach.
    shouted = str(REPO_ROOT / ".HARNESSED" / "x")
    same = os.path.normcase(shouted) == os.path.normcase(str(REPO_ROOT / ".harnessed" / "x"))
    assert isolation._inside(isolation._canonical(shouted), protected) is same


SPAWNS = """
import subprocess
import sys

def test_spawn(tmp_path, tmp_harnessed_home):
    subprocess.run(
        [sys.executable, "-m", "harnessed", "--help"], capture_output=True, {kwargs}
    )
"""


@pytest.mark.parametrize(
    ("kwargs", "breach"),
    [
        ("env={**__import__('os').environ, 'HARNESSED_HOME': str(tmp_harnessed_home)}", True),
        ("cwd=__import__('os').getcwd(), env=None", True),
        (
            "cwd=tmp_path, env={**__import__('os').environ, 'HARNESSED_HOME': str(tmp_path)}",
            False,
        ),
        ("cwd=tmp_path, env={'PATH': __import__('os').environ['PATH']}", True),
        ("cwd=tmp_path, env=None", True),
    ],
    ids=["no-cwd", "repo-cwd", "own-cwd-and-home", "no-home", "inherited-env"],
)
def test_spawning_the_harness_needs_its_own_cwd_and_home(
    pytester: pytest.Pytester, kwargs: str, breach: bool
) -> None:
    result = _inner(pytester, SPAWNS.format(kwargs=kwargs))
    if breach:
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(["*IsolationBreach*"])
    else:
        result.assert_outcomes(passed=1)


# On Windows ``Popen`` audits the command line already joined into one string, so the guard must
# split it back into an argv before it can tell whether the harness is being spawned.
COMMAND_LINES = [
    [r"C:\Users\...\Harnessed\.venv\Scripts\python.exe", "-m", "harnessed", "--help"],
    [
        r"C:\Program Files\Python312\python.exe",
        "-c",
        'print("a b")\nx = "\\"q\\""',
        r"C:\Users\...",
    ],
    [r"C:\x\harnessed.exe", "run", "", "tail\\"],
]


@pytest.mark.parametrize("argv", COMMAND_LINES, ids=["module", "quoted-script", "script"])
def test_a_joined_command_line_splits_back_into_its_argv(argv: list[str]) -> None:
    import subprocess

    assert isolation._split_command_line(subprocess.list2cmdline(argv)) == argv


def test_a_joined_command_line_is_recognised_as_the_harness() -> None:
    import subprocess

    assert isolation._runs_harness(subprocess.list2cmdline(COMMAND_LINES[0]))
    assert isolation._runs_harness(subprocess.list2cmdline(COMMAND_LINES[2]))


def test_a_script_whose_last_argument_is_the_checkout_is_not_the_harness() -> None:
    import subprocess

    # A path ending in ``Harnessed`` is a directory, not the program: the first token decides.
    joined = subprocess.list2cmdline([r"C:\py\python.exe", "-c", "pass", r"C:\Users\...\Harnessed"])
    assert not isolation._runs_harness(joined)
