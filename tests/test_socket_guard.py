"""FR-1 and FR-32 - the socket guard admits loopback and nothing else.

Ticket 002's acceptance criteria in executable form, narrowed by ticket 063: the guard raises on
each patched entry point for a non-loopback host, with the attempted host and the entry point in
the message, and admits a loopback connection through each of the same three entry points. The
loopback claim is carried by a real round trip against a listener bound on port 0 inside the test,
never by an assertion on the fixture's docstring (MUST rule 12). The two selection-level
properties are asserted by running a child pytest over a generated test file with this
repository's own configuration (``-c pyproject.toml``), which is what carries ``addopts``, the
declared markers and ``--strict-markers``.

The generated files live under ``tmp_path`` rather than ``tests/fixtures/`` on purpose: a file
that is meant to fail collection must never be collected by the parent run.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import SocketGuardError, guard_installed

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# A host the guard must never let a non-live test reach. Never actually resolved: the guard
# raises before any name lookup happens.
FORBIDDEN = ("api.example.com", 443)

EVIDENCE = REPO_ROOT / "evidence" / "063" / "loopback-round-trip.txt"


def run_child_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    """Run pytest in a child process under this repository's own ini options."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(PYPROJECT),
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def write_test_file(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ---- the guard raises, and its error is its own ---------------------------------------------


def test_connect_is_blocked_and_names_the_host() -> None:
    """A non-live test that opens an outbound connection fails with the guard's own error."""
    sock = socket.socket()
    with pytest.raises(SocketGuardError) as caught:
        sock.connect(FORBIDDEN)
    sock.close()
    assert "api.example.com:443" in str(caught.value)


def test_the_guard_error_is_not_a_timeout_or_a_connection_error() -> None:
    """The failure must be legible as "the guard stopped you", never as a flaky network."""
    sock = socket.socket()
    with pytest.raises(SocketGuardError) as caught:
        sock.connect(FORBIDDEN)
    sock.close()
    assert not isinstance(caught.value, OSError)


@pytest.mark.parametrize("port", [80, 443, 8080])
def test_connect_ex_is_blocked(port: int) -> None:
    """``connect_ex`` returns an errno instead of raising, so it needs its own patch."""
    sock = socket.socket()
    with pytest.raises(SocketGuardError) as caught:
        sock.connect_ex(("api.example.com", port))
    sock.close()
    assert f"api.example.com:{port}" in str(caught.value)


def test_create_connection_is_blocked() -> None:
    """``socket.create_connection`` builds its own socket, so patching the methods is not enough."""
    with pytest.raises(SocketGuardError) as caught:
        socket.create_connection(FORBIDDEN, timeout=0.1)
    assert "api.example.com:443" in str(caught.value)


# ---- loopback is admitted, through every entry point, and proved by a round trip -------------


def _listener(family: int, host: str) -> socket.socket:
    """Bind on port 0 and let the OS pick: the guard must never pass by knowing a port."""
    server = socket.socket(family, socket.SOCK_STREAM)
    server.bind((host, 0))
    server.listen(1)
    server.settimeout(5)
    return server


def _ipv6_listener() -> socket.socket:
    if not socket.has_ipv6:
        pytest.skip("this interpreter was built without IPv6")
    try:
        return _listener(socket.AF_INET6, "::1")
    except OSError as error:
        pytest.skip(f"no IPv6 loopback on this host: {error}")


def _connect(entry_point: str, family: int, address: tuple[str, int]) -> socket.socket:
    """Open a client through exactly one of the three guarded entry points."""
    if entry_point == "create_connection":
        return socket.create_connection(address, timeout=5)
    client = socket.socket(family, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        if entry_point == "connect":
            client.connect(address)
        else:
            assert client.connect_ex(address) == 0
    except BaseException:
        client.close()
        raise
    return client


def _round_trip(entry_point: str, server: socket.socket) -> tuple[str, int]:
    """Connect, exchange one payload each way, and return the address that was reached."""
    host, port = server.getsockname()[:2]
    client = _connect(entry_point, server.family, (host, port))
    try:
        peer, _ = server.accept()
        try:
            client.sendall(b"ping")
            assert peer.recv(4) == b"ping"
            peer.sendall(b"pong")
            assert client.recv(4) == b"pong"
        finally:
            peer.close()
    finally:
        client.close()
    return host, port


ENTRY_POINTS = ["connect", "connect_ex", "create_connection"]
FULL_NAMES = {
    "connect": "socket.socket.connect",
    "connect_ex": "socket.socket.connect_ex",
    "create_connection": "socket.create_connection",
}


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_ipv4_loopback_round_trip_succeeds(entry_point: str) -> None:
    """A non-live test reaches a listener on 127.0.0.1 and completes a round trip."""
    server = _listener(socket.AF_INET, "127.0.0.1")
    try:
        host, port = _round_trip(entry_point, server)
    finally:
        server.close()
    assert host == "127.0.0.1"
    assert port != 0


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_ipv6_loopback_round_trip_succeeds(entry_point: str) -> None:
    """The same holds on ``::1``, or the test skips rather than passing vacuously."""
    server = _ipv6_listener()
    try:
        host, port = _round_trip(entry_point, server)
    finally:
        server.close()
    assert host == "::1"
    assert port != 0


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_non_loopback_is_refused_by_every_entry_point(entry_point: str) -> None:
    """Each entry point refuses a non-loopback host on its own; none covers another."""
    with pytest.raises(SocketGuardError) as caught:
        _connect(entry_point, socket.AF_INET, FORBIDDEN)
    assert not isinstance(caught.value, OSError)
    assert "api.example.com:443" in str(caught.value)
    assert f"via {FULL_NAMES[entry_point]}." in str(caught.value)


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_the_decision_is_on_the_host_never_the_port(entry_point: str) -> None:
    """A non-loopback host is refused on the very port a loopback listener is admitted on."""
    server = _listener(socket.AF_INET, "127.0.0.1")
    try:
        _, port = _round_trip(entry_point, server)
        with pytest.raises(SocketGuardError) as caught:
            _connect(entry_point, socket.AF_INET, ("api.example.com", port))
    finally:
        server.close()
    assert f"api.example.com:{port}" in str(caught.value)


def test_a_hostname_is_not_loopback() -> None:
    """Only a literal loopback address passes; a name is refused before it is ever resolved."""
    with pytest.raises(SocketGuardError):
        socket.create_connection(("localhost", 9), timeout=0.1)


def test_evidence_shows_the_exchange_and_the_refusal_side_by_side() -> None:
    """The completed loopback exchange and the refusal, written for a human to read."""
    server = _listener(socket.AF_INET, "127.0.0.1")
    try:
        host, port = _round_trip("create_connection", server)
        with pytest.raises(SocketGuardError) as caught:
            socket.create_connection(("api.example.com", port), timeout=0.1)
    finally:
        server.close()
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        "\n".join(
            [
                "ticket 063 - the socket guard admits loopback and nothing else",
                "",
                f"admitted: {host}:{port} via socket.create_connection - ping/pong completed",
                f"refused:  api.example.com:{port} via socket.create_connection - "
                f"{type(caught.value).__name__}",
                "",
                str(caught.value),
                "",
            ]
        ),
        encoding="utf-8",
    )
    written = EVIDENCE.read_text(encoding="utf-8")
    assert f"admitted: 127.0.0.1:{port}" in written
    assert f"refused:  api.example.com:{port}" in written


# ---- teardown restores the exact shape ---------------------------------------------------------


def test_teardown_preserves_own_versus_inherited() -> None:
    """An inherited name is deleted on restore, an own name is put back, by identity."""

    class Base:
        def connect(self, address: object) -> str:
            return "base connect"

        def connect_ex(self, address: object) -> int:
            return 0

    class Sub(Base):
        def connect_ex(self, address: object) -> int:
            return 1

    class Module:
        @staticmethod
        def create_connection(address: object) -> str:
            return "module create_connection"

    own_connect_ex = vars(Sub)["connect_ex"]
    own_create = vars(Module)["create_connection"]
    table = (
        (Sub, "connect", "socket.socket.connect"),
        (Sub, "connect_ex", "socket.socket.connect_ex"),
        (Module, "create_connection", "socket.create_connection"),
    )
    with guard_installed(table):
        assert "connect" in vars(Sub)
        assert getattr(Sub.connect, "_harnessed_socket_guard", False)
        with pytest.raises(SocketGuardError):
            Sub().connect(FORBIDDEN)
        assert Sub().connect(("127.0.0.1", 1)) == "base connect"
        assert Sub().connect_ex(("127.0.0.1", 1)) == 1
        assert Module.create_connection(("::1", 1)) == "module create_connection"
    assert "connect" not in vars(Sub)
    assert Sub.connect is Base.connect
    assert vars(Sub)["connect_ex"] is own_connect_ex
    assert vars(Module)["create_connection"] is own_create


def test_all_three_entry_points_are_patched_together() -> None:
    """One partial guard is no guard: a caller would simply use the entry point left open."""
    guarded = {socket.socket.connect, socket.socket.connect_ex, socket.create_connection}
    assert len(guarded) == 3
    assert all(getattr(fn, "_harnessed_socket_guard", False) for fn in guarded)


# ---- properties of a whole selection ---------------------------------------------------------

LIVE_AND_OFFLINE = """
    import socket

    import pytest


    def test_offline_one():
        assert True


    @pytest.mark.live
    def test_needs_the_network():
        sock = socket.socket()
        sock.settimeout(0.5)
        try:
            with pytest.raises(OSError):
                sock.connect(("127.0.0.1", 9))
        finally:
            sock.close()
"""


def test_the_default_selection_collects_zero_live_tests(tmp_path: Path) -> None:
    """``addopts = -m "not live"`` is what makes offline the default, not a habit."""
    target = write_test_file(tmp_path, "test_generated_live.py", LIVE_AND_OFFLINE)
    result = run_child_pytest("--collect-only", "-q", str(target))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_offline_one" in result.stdout
    assert "test_needs_the_network" not in result.stdout


def test_a_live_test_is_deselected_and_unguarded(tmp_path: Path) -> None:
    """Selected explicitly, a live test runs against the real ``socket``, not the guard."""
    target = write_test_file(tmp_path, "test_generated_live.py", LIVE_AND_OFFLINE)
    result = run_child_pytest("-q", "-m", "live", str(target))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_an_undeclared_marker_is_an_error(tmp_path: Path) -> None:
    """Markers are declared in ``pyproject.toml``; a typo must fail, not silently do nothing."""
    target = write_test_file(
        tmp_path,
        "test_generated_marker.py",
        """
        import pytest


        @pytest.mark.slwo
        def test_typo_in_the_marker_name():
            assert True
        """,
    )
    result = run_child_pytest("-q", str(target))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "slwo" in result.stdout + result.stderr


def test_every_declared_marker_is_used_by_name(tmp_path: Path) -> None:
    """``live`` and ``slow`` are the only two markers, and both are declared."""
    result = run_child_pytest("--markers")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "@pytest.mark.live:" in result.stdout
    assert "@pytest.mark.slow:" in result.stdout
