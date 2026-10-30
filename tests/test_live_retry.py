"""FR-160 (ticket 231, decision 72): a live suite survives one dropped connection and never
overwrites its evidence.

Every claim is measured with injected faults over the offline doubles - ``tests/support/faults.py``
wraps FakeProvider and FakeDecider - so **0 sockets** are opened (MUST rule 18):

* ``is_transport`` answers true for a lost connection and false for an answer, a status, a read
  timeout and a parse failure.
* One drop is retried once after 1 s; three drops leave the case unscored with reason
  ``transport`` and the run completes; a 429 is never retried.
* ``--resume`` re-scores only the cases without a row; ``--evidence`` replaces the file only for a
  run that scored everything and otherwise leaves it byte-identical.
"""

from __future__ import annotations

import dataclasses
import http.client
import sqlite3
import ssl
import sys
import urllib.error
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from harnessed import config
from harnessed.cli import eval_cmd
from harnessed.cli.app import app, main
from harnessed.cli.evidence import incomplete_path, write_whole
from harnessed.errors import (
    DecisionDropped,
    EvalError,
    EvidenceIncomplete,
    ProviderError,
    SuiteMismatch,
    TransportDropped,
)
from harnessed.evals import runner
from harnessed.evals.suite import load_named_suite
from harnessed.loop.engine import LoopResult, run_loop
from harnessed.memory import db
from harnessed.providers.fake import FakeProvider
from harnessed.providers.fake_decider import FakeDecider
from harnessed.providers.jev import Decider
from harnessed.providers.openai_compat import openai
from harnessed.providers.transport import (
    BACKOFF_S,
    TRANSPORT_RETRIES,
    RetryingDecider,
    RetryingProvider,
    is_transport,
)
from harnessed.providers.types import ChatRequest, ChatResponse, Message
from harnessed.router import suite as routing
from harnessed.tools import default_registry
from support.faults import FaultyDecider, FaultyProvider, decision_drop, drop

cli = CliRunner(mix_stderr=False)

MODEL = "fake-1"


def request() -> ChatRequest:
    return ChatRequest(
        model=MODEL,
        messages=(Message(role="user", content="hi"),),
        metadata={"scope": "smoke-001-capital"},
    )


class Sleeps:
    """A ``sleep`` that records instead of waiting."""

    def __init__(self) -> None:
        self.waited: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


# ---- is_transport -------------------------------------------------------------------------------


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://example.invalid/v1")


def _status_error(status: int) -> openai.APIStatusError:
    response = httpx.Response(status, request=_request())
    return openai.APIStatusError("boom", response=response, body=None)


def _chained(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


DROPS: list[tuple[str, Callable[[], BaseException]]] = [
    ("ssl", lambda: ssl.SSLError("SSLV3_ALERT_BAD_RECORD_MAC")),
    ("reset", lambda: ConnectionResetError("reset")),
    ("aborted", lambda: ConnectionAbortedError("aborted")),
    ("pipe", lambda: BrokenPipeError("pipe")),
    ("remote", lambda: http.client.RemoteDisconnected("Remote end closed connection")),
    ("incomplete", lambda: http.client.IncompleteRead(b"x")),
    ("urlerror-ssl", lambda: urllib.error.URLError(ssl.SSLError("bad record mac"))),
    ("urlerror-connect-timeout", lambda: urllib.error.URLError(TimeoutError("timed out"))),
    ("sdk-connection", lambda: openai.APIConnectionError(request=_request())),
    (
        "sdk-connect-timeout",
        lambda: _chained(
            openai.APITimeoutError(request=_request()), httpx.ConnectTimeout("connect")
        ),
    ),
    (
        "wrapped-in-context",
        lambda: _chained(ValueError("outer"), ConnectionResetError("inner")),
    ),
]

ANSWERS: list[tuple[str, Callable[[], BaseException]]] = [
    ("http-429", lambda: _status_error(429)),
    ("http-500", lambda: _status_error(500)),
    ("urllib-http-error", lambda: urllib.error.HTTPError("u", 503, "x", None, None)),  # type: ignore[arg-type]
    ("provider-error", lambda: ProviderError("the endpoint answered 400")),
    ("read-timeout", lambda: TimeoutError("The read operation timed out")),
    (
        "sdk-read-timeout",
        lambda: _chained(openai.APITimeoutError(request=_request()), httpx.ReadTimeout("read")),
    ),
    ("parse-failure", lambda: ValueError("not json")),
    ("refused-connection", lambda: ConnectionRefusedError("refused")),
    ("urlerror-unreachable", lambda: urllib.error.URLError("Network is unreachable")),
]


@pytest.mark.parametrize(("name", "make"), DROPS, ids=[n for n, _ in DROPS])
def test_a_lost_connection_is_transport(name: str, make: Callable[[], BaseException]) -> None:
    assert is_transport(make()) is True


@pytest.mark.parametrize(("name", "make"), ANSWERS, ids=[n for n, _ in ANSWERS])
def test_an_answer_a_status_or_a_read_timeout_is_not_transport(
    name: str, make: Callable[[], BaseException]
) -> None:
    assert is_transport(make()) is False


def test_a_cyclic_cause_chain_terminates() -> None:
    first, second = ValueError("a"), ValueError("b")
    first.__cause__, second.__cause__ = second, first
    assert is_transport(first) is False


def test_the_policy_is_two_retries_at_one_then_three_seconds() -> None:
    assert (TRANSPORT_RETRIES, BACKOFF_S) == (2, (1.0, 3.0))


# ---- the retrying provider ----------------------------------------------------------------------


@pytest.fixture
def offline(clean_env: None, tmp_harnessed_home: Path) -> FakeProvider:
    return FakeProvider(config.load())


def test_one_drop_is_retried_once_after_one_second(offline: FakeProvider) -> None:
    faulty = FaultyProvider(offline, [drop()])
    sleeps, notes = Sleeps(), []
    provider = RetryingProvider(faulty, on_retry=lambda *note: notes.append(note), sleep=sleeps)
    response = provider.complete(request())
    assert isinstance(response, ChatResponse)
    assert faulty.calls == 2
    assert sleeps.waited == [1.0]
    assert notes == [("chat", 1, "SSLError", 1.0)]


def test_three_drops_raise_after_two_retries_waiting_one_then_three_seconds(
    offline: FakeProvider,
) -> None:
    faulty = FaultyProvider(offline, [drop(), drop(), drop()])
    sleeps = Sleeps()
    with pytest.raises(TransportDropped) as raised:
        RetryingProvider(faulty, sleep=sleeps).complete(request())
    assert faulty.calls == 3
    assert sleeps.waited == [1.0, 3.0]
    assert (raised.value.attempts, raised.value.error_class) == (3, "SSLError")


@pytest.mark.parametrize("fault", [ProviderError("the endpoint answered 429"), ValueError("x")])
def test_an_answer_is_never_retried(offline: FakeProvider, fault: BaseException) -> None:
    faulty = FaultyProvider(offline, [fault])
    sleeps = Sleeps()
    with pytest.raises(type(fault)):
        RetryingProvider(faulty, sleep=sleeps).complete(request())
    assert faulty.calls == 1
    assert sleeps.waited == []


def test_a_dropped_decision_is_retried_and_three_drops_raise(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    from harnessed.router.questions import ROUTE_QUESTION

    inner = FakeDecider(config.load())
    task = load_routing_task()
    questions = (ROUTE_QUESTION,)
    once = FaultyDecider(inner, [decision_drop()])
    sleeps = Sleeps()
    # The route question alone is not what the corpus recorded, so only the retry shape is read:
    # the second call reaches the wrapped decider (a corpus miss is not a drop).
    with pytest.raises(Exception) as missed:
        RetryingDecider(once, sleep=sleeps).decide(task, questions)
    assert not isinstance(missed.value, DecisionDropped)
    assert (once.calls, sleeps.waited) == (2, [1.0])

    thrice = FaultyDecider(inner, [decision_drop()] * 3)
    sleeps = Sleeps()
    with pytest.raises(DecisionDropped) as raised:
        RetryingDecider(thrice, sleep=sleeps).decide(task, questions)
    assert (thrice.calls, sleeps.waited, raised.value.attempts) == (3, [1.0, 3.0], 3)


def load_routing_task() -> str:
    return "Task: " + routing.load_routing_suite().tasks[0].task + "\n"


# ---- the runner: unscored, retries and resume ---------------------------------------------------


@pytest.fixture
def conn(offline: FakeProvider) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


def solver(provider: object, home: Path) -> Callable[..., LoopResult]:
    tools = default_registry(home)

    def solve(req: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        return run_loop(provider, tools, req, limit=6)  # type: ignore[arg-type]

    return solve


def run_smoke(
    conn: sqlite3.Connection,
    provider: object,
    home: Path,
    log: runner.RetryLog,
    **extra: object,
) -> runner.EvalRun:
    return runner.run_suite(
        load_named_suite("smoke"),
        solver(provider, home),
        conn,
        model=MODEL,
        provider="fake",
        fresh=True,
        retries=log,
        **extra,  # type: ignore[arg-type]
    )


def test_one_injected_drop_scores_the_case_after_one_retry_and_records_it(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, offline: FakeProvider
) -> None:
    log, sleeps = runner.RetryLog(), Sleeps()
    provider = RetryingProvider(
        FaultyProvider(offline, [drop()]), on_retry=log.on_retry, sleep=sleeps
    )
    run = run_smoke(conn, provider, tmp_harnessed_home, log)
    assert run.unscored == ()
    assert run.case_count == len(load_named_suite("smoke").cases)
    assert run.retried_calls == 1
    (row,) = conn.execute(
        "SELECT eval_run_id, case_id, call, attempt, error_class, backoff_s FROM eval_retries"
    ).fetchall()
    assert tuple(row) == (run.eval_run_id, "smoke-001-capital", "chat", 1, "SSLError", 1.0)
    assert conn.execute("SELECT finished_at FROM eval_runs").fetchone()[0] is not None


def test_three_injected_drops_leave_one_case_unscored_and_the_run_completes(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, offline: FakeProvider
) -> None:
    log = runner.RetryLog()
    provider = RetryingProvider(
        FaultyProvider(offline, [drop()] * 3), on_retry=log.on_retry, sleep=Sleeps()
    )
    run = run_smoke(conn, provider, tmp_harnessed_home, log)
    planned = len(load_named_suite("smoke").cases)
    assert run.unscored == (runner.Unscored("smoke-001-capital", "transport", 3, "SSLError"),)
    assert run.case_count == planned - 1
    assert run.retried_calls == 2
    scored = {row[0] for row in conn.execute("SELECT case_id FROM eval_results")}
    assert "smoke-001-capital" not in scored
    assert len(scored) == planned - 1
    assert conn.execute("SELECT finished_at FROM eval_runs").fetchone()[0] is None

    payload = runner.json_payload(run)
    assert payload["unscored"] == [
        {
            "case_id": "smoke-001-capital",
            "reason": "transport",
            "attempts": 3,
            "error_class": "SSLError",
        }
    ]
    assert payload["retried_calls"] == 2
    assert payload["score_over"] == "scored cases only"
    assert run.score == sum(c.score for c in run.cases) / len(run.cases)


def test_a_429_is_not_retried_and_propagates(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, offline: FakeProvider
) -> None:
    log, sleeps = runner.RetryLog(), Sleeps()
    faulty = FaultyProvider(offline, [ProviderError("the endpoint answered 429")])
    provider = RetryingProvider(faulty, on_retry=log.on_retry, sleep=sleeps)
    with pytest.raises(ProviderError):
        run_smoke(conn, provider, tmp_harnessed_home, log)
    assert faulty.calls == 1 and sleeps.waited == []
    assert conn.execute("SELECT COUNT(*) FROM eval_retries").fetchone()[0] == 0


def test_resume_rescored_only_the_cases_without_a_row(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, offline: FakeProvider
) -> None:
    log = runner.RetryLog()
    broken = RetryingProvider(
        FaultyProvider(offline, [drop()] * 3), on_retry=log.on_retry, sleep=Sleeps()
    )
    first = run_smoke(conn, broken, tmp_harnessed_home, log)
    counting = FaultyProvider(offline, [])
    second = runner.run_suite(
        load_named_suite("smoke"),
        solver(counting, tmp_harnessed_home),
        conn,
        model=MODEL,
        provider="fake",
        resume=first.eval_run_id,
        retries=log,
    )
    assert second.eval_run_id == first.eval_run_id
    assert second.unscored == ()
    assert second.scored_now == 1
    assert counting.calls >= 1
    assert conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0] == 1
    assert conn.execute("SELECT finished_at FROM eval_runs").fetchone()[0] is not None


def test_resume_refuses_an_unknown_run_and_another_suites_run(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, offline: FakeProvider
) -> None:
    solve = solver(offline, tmp_harnessed_home)
    smoke = load_named_suite("smoke")
    with pytest.raises(EvalError, match="no eval run"):
        runner.run_suite(smoke, solve, conn, model=MODEL, provider="fake", resume="eval_nope")
    other = dataclasses.replace(smoke, name="other")
    made = runner.run_suite(smoke, solve, conn, model=MODEL, provider="fake", fresh=True)
    with pytest.raises(SuiteMismatch):
        runner.run_suite(other, solve, conn, model=MODEL, provider="fake", resume=made.eval_run_id)


# ---- the routing suite --------------------------------------------------------------------------


def test_a_routing_task_whose_decision_drops_three_times_is_unscored_and_the_rest_score(
    conn: sqlite3.Connection,
) -> None:
    loaded = routing.load_routing_suite()
    faulty = FaultyDecider(FakeDecider(config.load()), [decision_drop()] * 3)
    decider: Decider = RetryingDecider(faulty, sleep=Sleeps())
    scored, unscored = eval_cmd._run_routing_tolerant(loaded, decider, conn, None)
    assert unscored == [(loaded.tasks[0].id, 3, "SSLError")]
    assert scored.total == len(loaded.tasks) - 1


# ---- evidence is written whole ------------------------------------------------------------------


def test_write_whole_replaces_the_file_only_for_a_complete_run(tmp_path: Path) -> None:
    target = tmp_path / "evidence" / "routing-live.txt"
    write_whole(target, "first\n", complete=True)
    assert target.read_text() == "first\n"
    assert not target.with_name(target.name + ".tmp").exists()

    with pytest.raises(EvidenceIncomplete) as raised:
        write_whole(target, "partial\n", complete=False)
    assert target.read_text() == "first\n"
    assert incomplete_path(target).read_text() == "partial\n"
    assert str(target) in str(raised.value) and str(incomplete_path(target)) in str(raised.value)
    assert not target.with_name(target.name + ".tmp").exists()


def _with_faults(monkeypatch: pytest.MonkeyPatch, script: list[BaseException | None]) -> None:
    def wrap(session: object, log: runner.RetryLog) -> object:
        provider = RetryingProvider(
            FaultyProvider(session.provider, script),  # type: ignore[attr-defined]
            on_retry=log.on_retry,
            sleep=Sleeps(),
        )
        return dataclasses.replace(session, provider=provider)  # type: ignore[type-var]

    monkeypatch.setattr(eval_cmd, "live_session", wrap)


def test_a_complete_run_writes_the_evidence_file_whole(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "out" / "smoke.txt"
    result = cli.invoke(app, ["eval", "run", "smoke", "--fresh", "--evidence", str(target)])
    assert result.exit_code == 0, result.stderr
    assert target.read_text(encoding="utf-8") == result.stdout
    assert "smoke" in target.read_text(encoding="utf-8")
    assert not incomplete_path(target).exists()


def test_a_run_with_an_unscored_case_leaves_the_evidence_byte_identical_and_exits_one(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "smoke.txt"
    target.write_bytes(b"the evidence of the last good run\r\n")
    before = target.read_bytes()
    _with_faults(monkeypatch, [drop()] * 3)
    argv = ["harnessed", "eval", "run", "smoke", "--fresh", "--evidence", str(target)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 1
    assert target.read_bytes() == before
    assert "unscored: transport" in incomplete_path(target).read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert str(target) in err and str(incomplete_path(target)) in err


def test_a_failing_command_leaves_the_evidence_byte_identical(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "smoke.txt"
    target.write_bytes(b"keep me")
    _with_faults(monkeypatch, [ProviderError("the endpoint answered 429")])
    result = cli.invoke(app, ["eval", "run", "smoke", "--fresh", "--evidence", str(target)])
    assert result.exit_code != 0
    assert target.read_bytes() == b"keep me"


def test_the_summary_names_the_unscored_cases(
    clean_env: None,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_faults(monkeypatch, [drop()] * 3)
    result = cli.invoke(app, ["eval", "run", "smoke", "--fresh"])
    assert result.exit_code == 0, result.stderr
    assert "4 of 5 scored, 1 unscored: transport" in result.stdout


def test_resume_through_the_cli_reopens_the_run(
    clean_env: None,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    _with_faults(monkeypatch, [drop()] * 3)
    first = cli.invoke(app, ["eval", "run", "smoke", "--fresh", "--json"])
    run_id = json.loads(first.stdout)["eval_run_id"]
    _with_faults(monkeypatch, [])
    second = cli.invoke(app, ["eval", "run", "smoke", "--resume", run_id, "--json"])
    payload = json.loads(second.stdout)
    assert payload["eval_run_id"] == run_id
    assert payload["unscored"] == [] and payload["case_count"] == 5
