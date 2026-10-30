"""No stub agent outlives the test, or the session, that started it (ticket 090).

Twenty-six stub processes were found alive hours after the runs that started them had ended, and
all of them came from runs that stopped early. A test's own cleanup is a ``finally`` in a helper or
a tree kill inside ``delegate``; neither runs for a delegation still waiting on a background thread
when the test is interrupted, and a ``hang`` stub never reads the stdin that would end it.

Two nets, both keyed on the registry every stub writes its pid into (``REGISTRY_VAR``):

- ``stub_reaper``, a per-test finaliser, kills the tree of any stub the test started that is still
  alive at teardown. Pytest runs finalisers on an interrupt too, from its own ``sessionfinish``.
- ``pytest_sessionfinish`` then reports any stub still alive at the very end, kills it, and fails
  a run that would otherwise have passed. That is the check that keeps the first net honest.

A pytest plugin rather than conftest code, so a test can run an inner pytest with it (``-p
tests.support.reaper``) and without it, and compare the survivors. Imported by that dotted name,
because the root conftest imports ``tests.conftest`` before ``tests/`` is on ``sys.path``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessed.acp.delegate import alive, kill_tree

from .stub_acp_agent import REGISTRY_VAR

__all__ = ["pytest_sessionfinish", "pytest_sessionstart", "stub_reaper"]

_folder: Path | None = None


def pytest_sessionstart(session: pytest.Session) -> None:
    """Give every stub this session starts a folder to register in, through the environment."""
    global _folder
    _folder = Path(tempfile.mkdtemp(prefix="harnessed-stubs-"))
    os.environ[REGISTRY_VAR] = str(_folder)


def _registered() -> set[int]:
    if _folder is None or not _folder.is_dir():
        return set()
    return {int(entry.name) for entry in _folder.iterdir() if entry.name.isdigit()}


def _forget(pid: int) -> None:
    if _folder is not None:
        (_folder / str(pid)).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def stub_reaper() -> Iterator[None]:
    """Kill the tree of every stub this test started that is still alive when it ends."""
    before = _registered()
    yield
    for pid in sorted(_registered() - before):
        if alive(pid):
            kill_tree(pid)
        if not alive(pid):
            _forget(pid)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    """Report, kill and fail on any stub still alive once every finaliser has run."""
    survivors = sorted(pid for pid in _registered() if alive(pid))
    for pid in survivors:
        kill_tree(pid)
    if _folder is not None:
        shutil.rmtree(_folder, ignore_errors=True)
    if not survivors:
        return
    message = (
        f"stub_acp_agent.py outlived the test that started it: pid "
        f"{', '.join(str(pid) for pid in survivors)} (killed at session end)"
    )
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(message, red=True)
    else:
        print(message)
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
