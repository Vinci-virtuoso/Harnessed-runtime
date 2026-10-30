"""What every eval file gets before it runs: the repository root, the marker, and the guard.

Three jobs, and each one is a criterion of its own.

* **The repository root is importable.** This directory sits beside ``src/``, not inside it, so
  ``evals.helpers`` and ``tests.conftest`` both resolve only once the root is on ``sys.path``.
* **``live_eval`` is a declared marker.** ``--strict-markers`` turns an undeclared marker into a
  collection error, and ``pyproject.toml`` may not gain one under this ticket - so it is declared
  here, which is where a directory-local marker belongs anyway.
* **The socket guard is installed.** The *same* guard ``tests/conftest.py`` owns, imported rather
  than copied, for every test that is not marked ``live`` or ``live_eval``. An offline eval that
  quietly reached the network would report that the harness works when what worked was somebody's
  API key.

It also owns the ``record`` fixture and the session hook that writes what was recorded. Results go
under ``HARNESSED_HOME`` and are written once, at the end of the run, by ``results.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.conftest import _GUARDED, _MISSING, _blocked  # noqa: E402

from evals.helpers import RUN_LIVE_VAR  # noqa: E402
from evals.results import CaseResult, write  # noqa: E402

#: This suite's own live marker. Not ``live``: ``addopts = -m "not live"`` would deselect a test
#: carrying that one, and a deselected test never prints the reason it was waiting on.
LIVE_EVAL_MARKER = "live_eval"

#: The markers the socket guard stands aside for. A live tier that could not open a socket would
#: be blocked by the thing that protects the offline one.
EXEMPT_MARKERS: tuple[str, ...] = ("live", LIVE_EVAL_MARKER)

#: What the run recorded, in the order it was recorded. Module-level rather than a session-scoped
#: fixture because ``pytest_sessionfinish`` is where it is written and hooks take no fixtures.
_RECORDED: list[CaseResult] = []


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{LIVE_EVAL_MARKER}: needs a real provider key and {RUN_LIVE_VAR}=1; skipped with a "
        f"reason rather than deselected, so the reason is printable",
    )


@pytest.fixture(autouse=True)
def eval_socket_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Block outbound connections for every test that is not one of the two live markers.

    The replacement functions and the entry points are ``tests/conftest.py``'s, imported here so
    there is exactly one guard in the repository rather than two that could drift. Teardown
    restores each name to whether it was the target's own or inherited, which is what keeps a
    subclass from carrying a permanent shadow.
    """
    if any(request.node.get_closest_marker(name) is not None for name in EXEMPT_MARKERS):
        yield
        return

    saved = [(target, name, vars(target).get(name, _MISSING)) for target, name, _ in _GUARDED]
    for target, name, entry_point in _GUARDED:
        setattr(target, name, _blocked(entry_point))
    try:
        yield
    finally:
        for target, name, original in saved:
            if original is _MISSING:
                delattr(target, name)
            else:
                setattr(target, name, original)


@pytest.fixture
def record() -> Callable[[CaseResult], CaseResult]:
    """Record one case's outcome. Returns it, so a test can assert on what it recorded."""

    def _record(case: CaseResult) -> CaseResult:
        _RECORDED.append(case)
        return case

    return _record


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write the run's results under ``HARNESSED_HOME``, then forget them.

    At session finish every ``monkeypatch`` has unwound, so ``results.py`` resolves the real
    ``HARNESSED_HOME`` - the operator's ``.harnessed/`` by default, or whatever a parent process
    set. That is the point: a run's results belong to the run, not to one test's ``tmp_path``.
    """
    if not _RECORDED:
        return
    try:
        write(tuple(_RECORDED))
    finally:
        _RECORDED.clear()
