"""AC 3 - the offline tier runs behind the same socket guard the unit suite runs behind.

An offline eval that quietly reached the network would be the worst kind of green: it would report
that the harness works when what worked was somebody's API key. ``evals/conftest.py`` therefore
installs the guard from ``tests/conftest.py`` - the same code, imported, not a second copy - for
every test that is not marked ``live`` or ``live_eval``.

Importing ``tests.conftest`` from here is itself the proof that the repository root is on
``sys.path``, which is the other half of this criterion.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from tests.conftest import SocketGuardError

from evals.conftest import EXEMPT_MARKERS, LIVE_EVAL_MARKER

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---- offline tier ------------------------------------------------------------------------------


def test_the_repository_root_is_importable() -> None:
    assert str(REPO_ROOT) in sys.path
    assert Path(__import__("harnessed").__file__).is_file()


def test_the_guard_installed_here_is_the_one_tests_conftest_owns() -> None:
    assert getattr(socket.socket.connect, "_harnessed_socket_guard", False) is True
    assert getattr(socket.create_connection, "_harnessed_socket_guard", False) is True


def test_an_offline_eval_that_reaches_the_network_fails() -> None:
    with pytest.raises(SocketGuardError) as caught:
        socket.create_connection(("example.invalid", 443))
    assert "example.invalid:443" in str(caught.value)


def test_connect_ex_is_guarded_too_because_it_returns_an_errno_instead_of_raising() -> None:
    with pytest.raises(SocketGuardError):
        socket.socket().connect_ex(("example.invalid", 443))


def test_both_live_markers_are_exempt_and_both_are_declared(
    pytestconfig: pytest.Config,
) -> None:
    """``live`` is the repository's marker; ``live_eval`` is this suite's own.

    The live tier cannot carry ``live`` - ``addopts = -m "not live"`` would deselect it, and a
    deselected test never prints a reason. So it carries its own marker, and the guard exempts
    both or a real live run would be blocked by the thing that protects the offline one.
    """
    assert EXEMPT_MARKERS == ("live", LIVE_EVAL_MARKER)
    declared = "\n".join(pytestconfig.getini("markers"))
    assert f"{LIVE_EVAL_MARKER}:" in declared
