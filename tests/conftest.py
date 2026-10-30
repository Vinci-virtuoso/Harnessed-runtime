"""Shared determinism fixtures.

FR-1: this file owns the socket guard, the frozen clock, the seeded id generator, the temporary
``HARNESSED_HOME`` and a clean environment. It is the single place they live, so no test ever
patches a global itself.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# Ticket 230: the operator's setup is out of bounds for every test not marked live (decision 71).
# Imported before anything below clears HARNESSED_HOME, so the session-start value is captured.
from tests.support.isolation import isolation_guard  # noqa: F401

# Ticket 090: a per-test finaliser and a session-end check, so no stub agent outlives its run.
from tests.support.reaper import (  # noqa: F401
    pytest_sessionfinish,
    pytest_sessionstart,
    stub_reaper,
)
from tests.support.stub_acp_agent import COVERAGE_VARS

from harnessed import clock, ids
from harnessed import config as app_config

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every environment variable documented in .env.example. A test that wants a clean process
# environment clears exactly these, so nothing leaks in from the developer's shell.
DOCUMENTED_ENV_VARS = (
    "HARNESSED_HOME",
    "HARNESSED_PROVIDER",
    "HARNESSED_MODEL",
    "HARNESSED_SMALL_MODEL",
    "HARNESSED_BASE_URL",
    "HARNESSED_TOKEN_BUDGET",
    "HARNESSED_MAX_ITERATIONS",
    "HARNESSED_PERMISSION_CEILING_S",
    "HARNESSED_CONSOLIDATE_AFTER",
    "HARNESSED_EMBED_BASE_URL",
    "HARNESSED_EMBED_MODEL",
    "HARNESSED_ROUTER_MODEL",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


#: The provider the offline suite runs on, named here rather than defaulted to anywhere. FR-27
#: allows exactly three ways to select ``fake`` - the flag, the variable, or a pytest fixture -
#: and this constant is the third one.
OFFLINE_PROVIDER = "fake"

#: What ``pytest_configure`` below scrubs (ticket 056). The union of this file's documented set and
#: the one variable ``evals/helpers.py`` adds - ``HARNESSED_RUN_LIVE_EVALS`` - because that is the
#: variable the live tier turns on, and clearing only this file's tuple would leave the gate open.
#:
#: ``DEEPEVAL_TELEMETRY_OPT_OUT`` is deliberately NOT here. It arrives by the same route, but it is
#: an opt-OUT: removing it would switch a vendor's telemetry back on. Scrubbing is for variables
#: that make this process do more, never less.
GUARDED_ENV_VARS = (*DOCUMENTED_ENV_VARS, "HARNESSED_RUN_LIVE_EVALS")


def dotenv_on_disk() -> dict[str, str]:
    """What this repository's own ``.env`` sets, or an empty mapping when there is none.

    Read through ``config.parse_env_text`` rather than a second parser: the point is to compare
    against exactly what the application itself would resolve from that file.
    """
    try:
        return app_config.parse_env_text((REPO_ROOT / ".env").read_text(encoding="utf-8"))
    except OSError:
        return {}


#: What ``.env`` set when this module was imported, read once while no test is running. A test that
#: wants to compare against the file reads this: opening ``.env`` itself would be exactly the read
#: the isolation guard exists to refuse (ticket 230).
DOTENV_AT_START = dotenv_on_disk()


def scrub_injected_dotenv() -> None:
    """Scrub the repository's ``.env`` back out of the process (ticket 056, finding 9).

    ``deepeval`` ships an entry-point pytest plugin, so it is imported on every ``pytest``
    invocation whether or not a test uses it, and importing it calls python-dotenv. This
    repository's ``.env`` therefore reached ``os.environ`` before collection began, which opened
    the live eval tier and made a bare ``uv run pytest`` place billable API calls on a machine
    where nobody had exported anything. FR-1 gives this file the clean environment; that guarantee
    was true when written and was voided by a dependency, with no test standing on it.

    Not keyed on ``deepeval``. The failure is "anything can put a key in this process and nothing
    notices", so naming the culprit would close it for one library and leave the next one that
    calls ``load_dotenv`` on import to reopen it silently.

    **An explicit export still wins, and that is decided by evidence rather than by guessing.**
    ``envsnapshot`` is loaded through ``-p`` in ``addopts``, which pytest imports before the
    setuptools entry-point plugins, so it holds the environment as it was *before* anything could
    inject. A variable is removed only when it is absent from that snapshot: present-before means
    the operator put it there, appeared-since means a plugin did. Value-matching was tried first
    and is not good enough - an operator exporting ``HARNESSED_RUN_LIVE_EVALS=1`` when ``.env``
    also says ``1`` is indistinguishable by value, and scrubbing it broke the second acceptance
    criterion outright.
    """
    try:
        from envsnapshot import PRE_PLUGIN_ENV
    except ImportError:  # pragma: no cover - the -p plugin is in addopts; this is the safety net
        # Without the snapshot, fall back to matching the file's value. Weaker: it cannot tell an
        # operator who exported exactly what `.env` already says, and scrubs it. That direction is
        # deliberate - a shut live tier and a printed reason, never an unasked-for bill.
        from_file = dotenv_on_disk()
        for name in GUARDED_ENV_VARS:
            if name in os.environ and os.environ[name] == from_file.get(name):
                del os.environ[name]
        return
    for name in GUARDED_ENV_VARS:
        if name in os.environ and name not in PRE_PLUGIN_ENV:
            del os.environ[name]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every documented variable, then name the offline provider on purpose.

    Clearing alone used to be enough because ``config.load`` defaulted to ``fake``. FR-27 took
    that default away: an unconfigured harness is an error, so a fixture that only cleared would
    leave every CLI test unable to build a provider at all. Setting the variable here keeps the
    selection deliberate and visible, which is the whole point of removing the default - a test
    that wants the unconfigured state deletes it again (see
    ``tests/test_provider_resolution.py``).
    """
    for name in DOCUMENTED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HARNESSED_PROVIDER", OFFLINE_PROVIDER)


@pytest.fixture(scope="session", autouse=True)
def child_coverage_config() -> None:
    """Children started under ``--cov`` measure branches wherever their cwd is (230 follow-up).

    pytest-cov exports ``COV_CORE_CONFIG`` to child processes only when its ``--cov-config`` file
    (``.coveragerc`` by default) exists; this repository keeps ``[tool.coverage.run]`` in
    ``pyproject.toml``, so children got ``os.pathsep`` ("no config") and found the branch setting
    only by searching their own cwd. Ticket 230 moved every spawned harness into a temporary cwd,
    so each child wrote statement-only data that ``coverage combine`` refuses to merge with the
    parent's branch data (``DataError``). Pointing the children at the repository's own
    ``pyproject.toml`` keeps them in branch mode and keeps their lines in the ratchet.
    """
    exported = os.environ.get("COV_CORE_CONFIG", os.pathsep)
    if "COV_CORE_SOURCE" in os.environ and exported == os.pathsep:
        os.environ["COV_CORE_CONFIG"] = str(Path(__file__).resolve().parents[1] / "pyproject.toml")


@pytest.fixture
def no_child_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep coverage out of the processes this test starts (ticket 117).

    ``verify.ps1`` runs pytest with ``--cov``, and pytest-cov's and coverage's ``.pth`` hooks start
    coverage in any child whose environment names these variables. A stub agent started that way
    cold-starts past a 5 s delegation ceiling. The stub lives outside ``--cov=src/harnessed``, so
    the total loses nothing.
    """
    for name in COVERAGE_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def own_cwd(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run every test that is not marked ``live`` from a directory of its own (ticket 230).

    ``config.load`` reads ``.env`` from the working directory, so a test that ran from the
    repository root read the operator's file whether or not it knew. A directory per test is the
    fix, not an exemption: ``tests/support/isolation.py`` still fails any test that opens the
    repository's ``.env``, ``agents.toml`` or ``.harnessed/`` by an absolute path.
    """
    if request.node.get_closest_marker("live") is None:
        monkeypatch.chdir(tmp_path)


@pytest.fixture
def tmp_harnessed_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``HARNESSED_HOME`` at a throwaway directory for the duration of one test."""
    home = tmp_path / ".harnessed"
    home.mkdir()
    monkeypatch.setenv("HARNESSED_HOME", str(home))
    yield home
    os.environ.pop("HARNESSED_HOME", None)


# ---- the socket guard (FR-1) -----------------------------------------------------------------

# The instant every frozen-clock test sees. Arbitrary, fixed, and never near a boundary.
FROZEN_INSTANT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# The seed every seeded-ids test starts from.
ID_SEED = 20260101


class SocketGuardError(RuntimeError):
    """An offline test tried to open an outbound connection.

    Deliberately outside the ``HarnessError`` tree: the CLI catches ``HarnessError`` broadly, so
    a guard violation inside a CLI test would be rendered as a tidy error message instead of
    failing the test. This one has nowhere to hide.
    """


def _describe(address: object) -> str:
    """``host:port`` for the usual AF_INET/AF_INET6 tuple, a repr for anything else."""
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return repr(address)


def _blocked(entry_point: str, original: Callable[..., Any] | None = None) -> Callable[..., Any]:
    """Build the replacement for one entry point: loopback goes through, anything else raises.

    ``original`` is what an admitted call is forwarded to. Left unset, it is looked up from the
    entry point's current value, which is the real one as long as the guard is not yet installed;
    that is the shape ``evals/conftest.py`` relies on when it installs this same guard by name.
    """
    if original is None:
        original = next(getattr(t, n) for t, n, ep in _GUARDED if ep == entry_point)

    def guard(*args: Any, **kwargs: Any) -> Any:
        # Bound methods arrive as (self, address); the module function as (address,).
        address = args[1] if entry_point.startswith("socket.socket.") else args[0]
        if _is_loopback(address):
            return original(*args, **kwargs)
        raise SocketGuardError(
            f"blocked outbound connection to {_describe(address)} "
            f"via {entry_point}. This test is not marked live, and the default selection is "
            f'offline (-m "not live"). Use FakeProvider and the fixture corpus, or mark the '
            f"test @pytest.mark.live if it genuinely needs the network."
        )

    guard._harnessed_socket_guard = True  # type: ignore[attr-defined]
    return guard


def _is_loopback(address: object) -> bool:
    """The whole decision, on the host and nothing else (MUST rule 18).

    Only a literal loopback address passes: ``127.0.0.0/8`` and ``::1``. A hostname is not
    loopback, whatever it would resolve to, so it is refused before any lookup happens. The port
    is never read: a port allowlist would admit a remote host that happened to listen on it.
    """
    if not isinstance(address, tuple) or not address or not isinstance(address[0], str):
        return False
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except ValueError:
        return False


# The three entry points that can open a connection. connect_ex returns an errno rather than
# raising, and create_connection builds its own socket, so none of the three covers another.
_GUARDED: tuple[tuple[object, str, str], ...] = (
    (socket.socket, "connect", "socket.socket.connect"),
    (socket.socket, "connect_ex", "socket.socket.connect_ex"),
    (socket, "create_connection", "socket.create_connection"),
)


@contextmanager
def guard_installed(guarded: tuple[tuple[object, str, str], ...] = _GUARDED) -> Iterator[None]:
    """Install the guard on every entry point in ``guarded``, and restore the exact shape after.

    Whether each name was the target's own or inherited is remembered, so teardown deletes the
    shadow it added rather than leaving a permanent one on the subclass, and puts an own attribute
    back by identity.
    """
    saved = [(target, name, vars(target).get(name, _MISSING)) for target, name, _ in guarded]
    for target, name, entry_point in guarded:
        setattr(target, name, _blocked(entry_point, getattr(target, name)))
    try:
        yield
    finally:
        for target, name, original in saved:
            if original is _MISSING:
                delattr(target, name)
            else:
                setattr(target, name, original)


@pytest.fixture(autouse=True)
def socket_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Admit loopback and refuse every other outbound connection, for every non-``live`` test.

    v2 has a network listener: the gateway is exercised through a ``TestClient`` bound on
    loopback, and on Windows ``asyncio`` builds its self-pipe on a ``127.0.0.1`` socket pair. The
    decision is a host predicate and never a port allowlist (MUST rule 18), and the claim that
    loopback works is carried by ``tests/test_socket_guard.py`` opening a real loopback connection,
    not by this docstring (MUST rule 12).
    """
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    with guard_installed():
        yield


class _Missing:
    """Sentinel: the attribute was inherited, not the target's own."""


_MISSING = _Missing()


# ---- frozen clock and seeded ids ---------------------------------------------------------------


@pytest.fixture
def frozen_clock() -> Iterator[datetime]:
    """Pin :func:`harnessed.clock.now`. Patches ``clock`` and nothing else (MUST rule 3)."""
    clock.freeze(FROZEN_INSTANT)
    yield FROZEN_INSTANT
    clock.unfreeze()


@pytest.fixture
def seeded_ids() -> Iterator[int]:
    """Make :mod:`harnessed.ids` reproducible for one test, then return it to entropy."""
    ids.seed(ID_SEED)
    yield ID_SEED
    ids.seed(None)


# ---- the ACP background loop (FR-1 meets FR-22) ------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def acp_loop_started() -> None:
    """Start ``acp/runtime.py``'s background loop once, before the socket guard is ever installed.

    Windows builds an ``asyncio`` event loop on a loopback socket pair, so the very first
    ``submit()`` inside a test would open a 127.0.0.1 connection and the guard would - correctly -
    refuse it. Weakening the guard to permit loopback would blunt it for every test in the suite,
    so instead the one loop the whole session shares is created here: session-scoped fixtures are
    set up before function-scoped ones, which puts the self-pipe outside every guarded window.
    The guard itself is untouched and still blocks loopback.
    """
    from harnessed.acp import runtime

    async def nothing() -> None:
        return None

    runtime.submit(nothing, timeout=30.0)


# ---- the dashboard fixture recorder's one switch (decision 33, MUST rule 24) --------------------

#: Ticket 100's option. Declared here because pytest only reads ``pytest_addoption`` from the
#: rootdir conftest, and nowhere else in this file changes: the recorder is one flag, not a
#: fixture and not an environment variable (which ``DOCUMENTED_ENV_VARS`` above would then owe
#: an entry).
RECORD_DASHBOARD_FIXTURES = "--record-dashboard-fixtures"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Declare the flag that rewrites ``dashboard/tests/fixtures/gateway/**``.

    Without it nothing under that directory is ever written at test time: the recordings are
    committed, reviewed like code, and compared byte for byte by
    ``tests/test_dashboard_fixtures.py``. A drift is a change to the emitter, so the honest fix
    is to re-record deliberately and read the diff (MUST rule 24).
    """
    parser.addoption(
        RECORD_DASHBOARD_FIXTURES,
        action="store_true",
        default=False,
        help=(
            "Rewrite the recorded gateway fixtures the dashboard unit tests read, from the "
            "emitters as they stand. Review the diff like code."
        ),
    )
