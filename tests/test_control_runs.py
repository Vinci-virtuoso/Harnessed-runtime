"""FR-79: opening a run answers with its session id before the first model call (decision 49).

``POST /control/v1/runs`` answers ``202 {"session_id", "stream", "run"}`` while the run is still
executing: the worker opens the CLI's own session, publishes ``run.started`` on that session's
bus, and only then hands the id back to the request thread, so the stream the 202 names already
holds its seq 1 when the browser opens it. One run is in flight at a time, decided under one lock;
a run that raises publishes ``error.raised`` on its own bus and frees the worker.

The console page's rendering of the 202 is ticket 139's and is asserted in its unit suite, not
here. The route's thin form, the error map and the lifespan are ``tests/test_control_app.py``'s.
"""

from __future__ import annotations

import ast
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.support.scoped_decider import ScopedFakeDecider

from harnessed import config, control, gateway
from harnessed.cli import run_cmd
from harnessed.control import worker
from harnessed.errors import HarnessError, RunInFlight
from harnessed.events import read as events_read
from harnessed.events.bus import Event
from harnessed.memory import db, redact
from harnessed.providers.fake_decider import FakeDecider

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "137"
DASHBOARD_CMD = REPO_ROOT / "src" / "harnessed" / "cli" / "dashboard_cmd.py"

RUNS = "/control/v1/runs"

#: The ad-hoc prompt the fake corpus answers: its scope is ``sha256("ping")[:12]`` (ticket 005).
PING = "ping"

#: Ticket 119's knob, set wide enough that a run is visibly still going when the 202 lands.
SLOW_MS = 400

#: FR-79's ceiling on the 202.
ACCEPT_WITHIN_S = 0.100

#: How long a test waits on the worker's future before calling the run stuck.
RUN_TIMEOUT_S = 15.0

#: A key shape the redaction table knows, built in pieces so this file carries no credential.
SEEDED_KEY = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDDEEEEFFFF"


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


EVIDENCE_164 = REPO_ROOT / "evidence" / "164"


def capture_164(name: str, text: str) -> Path:
    EVIDENCE_164.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_164 / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def home(clean_env: None, tmp_harnessed_home: Path) -> Path:
    """A migrated home under the offline provider, FakeProvider itself answering."""
    db.connect().close()
    return tmp_harnessed_home


@pytest.fixture
def slow(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every FakeProvider completion takes ``SLOW_MS`` (ticket 119's fixture)."""
    monkeypatch.setenv(config.FAKE_LATENCY_VAR, str(SLOW_MS))
    return home


@pytest.fixture
def app(home: Path) -> Any:
    return control.create_app(home)


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def events_of(home: Path, session_id: str) -> list[tuple[int, str, dict[str, Any]]]:
    """``(seq, name, payload)`` for every event the session holds, read off the ledger."""
    with sqlite3.connect(home / db.DATABASE_FILENAME) as conn:
        rows = conn.execute(
            "SELECT seq, name, payload FROM events WHERE run_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
    return [(int(seq), str(name), json.loads(payload)) for seq, name, payload in rows]


def names_of(home: Path, session_id: str) -> list[str]:
    return [name for _seq, name, _payload in events_of(home, session_id)]


def settle(app: Any) -> Any:
    in_flight = app.state.worker.run
    assert in_flight is not None, "nothing was submitted"
    return in_flight.result(timeout=RUN_TIMEOUT_S)


# ---- AC 1: the 202 lands within 100 ms and before run.finished ----------------------------------


def test_the_202_lands_within_100_ms_and_before_run_finished(slow: Path) -> None:
    app = control.create_app(slow)
    with TestClient(app, raise_server_exceptions=False) as client:
        before = time.perf_counter()
        response = client.post(RUNS, json={"prompt": PING})
        elapsed = time.perf_counter() - before
        assert response.status_code == 202, response.text
        body = response.json()
        session_id = body["session_id"]
        at_202 = names_of(slow, session_id)
        in_flight = not app.state.worker.run.done()
        settle(app)
        at_end = names_of(slow, session_id)

    capture(
        "accept-timing.txt",
        f"202 after {elapsed * 1000:.1f} ms (ceiling {ACCEPT_WITHIN_S * 1000:.0f} ms, "
        f"fake latency {SLOW_MS} ms per completion)\n"
        f"body {response.text}\n"
        f"events at the 202: {at_202}\n"
        f"run still in flight at the 202: {in_flight}\n"
        f"events once settled: {at_end}\n",
    )
    assert set(body) == {"session_id", "stream", "run"}
    assert body["stream"] == f"/v1/sessions/{session_id}/stream"
    assert body["run"] == f"/v1/runs/{session_id}"
    assert elapsed < ACCEPT_WITHIN_S, f"the 202 took {elapsed * 1000:.1f} ms"
    assert in_flight, "the run was still executing when the 202 landed"
    assert at_202[:1] == [run_cmd.RUN_STARTED], "the stream the 202 names already exists"
    assert run_cmd.RUN_FINISHED not in at_202, "the 202 arrived before run.finished"
    assert at_end.count(run_cmd.RUN_STARTED) == 1, "run.started is published once, not twice"
    assert at_end[-1] == run_cmd.RUN_FINISHED


def test_the_id_is_handed_back_before_the_provider_is_built(
    app: Any, client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 202 waits on the bus and ``run.started``, never on building the provider."""
    built: list[str] = []
    real_build = run_cmd.build_provider

    def slow_build(settings: Any) -> Any:
        time.sleep(0.5)
        built.append("built")
        return real_build(settings)

    monkeypatch.setattr(run_cmd, "build_provider", slow_build)
    before = time.perf_counter()
    response = client.post(RUNS, json={"prompt": PING})
    elapsed = time.perf_counter() - before
    assert response.status_code == 202, response.text
    session_id = response.json()["session_id"]
    assert built == [], "the provider was not yet built when the 202 was sent"
    assert names_of(home, session_id) == [run_cmd.RUN_STARTED]
    assert elapsed < 0.5
    settle(app)
    assert built == ["built"]
    assert names_of(home, session_id)[-1] == run_cmd.RUN_FINISHED


def test_the_cli_path_still_announces_once(home: Path) -> None:
    """``harnessed run``'s own order is unchanged: ``solve_with_memory`` publishes seq 1 itself."""
    session = run_cmd.open_session()
    with closing(session.conn):
        run_cmd.solve_with_memory(session, PING)
    names = names_of(home, session.bus.run_id)
    assert names[0] == run_cmd.RUN_STARTED and names.count(run_cmd.RUN_STARTED) == 1
    assert names[-1] == run_cmd.RUN_FINISHED


# ---- AC 2: one run in flight, decided under one lock --------------------------------------------


def test_a_second_post_while_one_runs_is_a_409_naming_the_session(
    slow: Path,
) -> None:
    app = control.create_app(slow)
    with TestClient(app, raise_server_exceptions=False) as client:
        first = client.post(RUNS, json={"prompt": PING})
        second = client.post(RUNS, json={"prompt": PING})
        settle(app)
    assert first.status_code == 202, first.text
    session_id = first.json()["session_id"]
    capture("in-flight-409.txt", f"first {first.text}\nsecond {second.status_code} {second.text}\n")
    assert second.status_code == 409, second.text
    assert second.json()["error"] == "RunInFlight"
    assert session_id in second.json()["message"]
    assert app.state.worker.session_id == session_id, "the refused POST changed nothing"


def test_two_simultaneous_starts_cannot_both_win() -> None:
    """Check and submit under one lock: a slow opener widens the window the lock must close."""
    held = worker.Worker()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    guard = threading.Lock()

    class Bus:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def publish(self, name: str, payload: Any = None) -> None:
            pass

    def opener(run_id: str) -> Any:
        def open_it(opened: Any) -> Any:
            time.sleep(0.2)
            opening = run_cmd.Opening(
                settings=SimpleNamespace(provider="fake", routing_on=lambda: True),  # type: ignore[arg-type]
                conn=SimpleNamespace(close=lambda: None),  # type: ignore[arg-type]
                bus=Bus(run_id),  # type: ignore[arg-type]
            )
            opened(opening)
            return opening

        return open_it

    def contender(run_id: str) -> None:
        barrier.wait()
        try:
            got = held.start(opener(run_id), lambda session, event: time.sleep(0.3))
            outcome = f"202 {got}"
        except RunInFlight as exc:
            outcome = f"409 {exc}"
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=contender, args=(f"run_{n}",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=RUN_TIMEOUT_S)
    assert held.run is not None
    held.run.result(timeout=RUN_TIMEOUT_S)
    held.executor.shutdown(wait=True)

    capture("concurrent-race.txt", "\n".join(sorted(outcomes)) + "\n")
    winners = [line for line in outcomes if line.startswith("202 ")]
    losers = [line for line in outcomes if line.startswith("409 ")]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    assert winners[0].removeprefix("202 ") in losers[0], "the 409 names the winner's session"


# ---- AC 3: a run that raises says so on its own bus and frees the worker ------------------------


def test_a_raising_run_publishes_error_raised_scrubbed_and_frees_the_worker(
    app: Any, client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising(session: Any, prompt: str, **kwargs: Any) -> Any:
        raise HarnessError(f"the provider refused {SEEDED_KEY} mid-run")

    monkeypatch.setattr(run_cmd, "solve_with_memory", raising)
    response = client.post(RUNS, json={"prompt": PING})
    assert response.status_code == 202, response.text
    session_id = response.json()["session_id"]
    assert settle(app) is None, "the future completes rather than raising"
    assert app.state.worker.run.exception() is None

    events = events_of(home, session_id)
    raised = [payload for _seq, name, payload in events if name == "error.raised"]
    finished = [payload for _seq, name, payload in events if name == run_cmd.RUN_FINISHED]
    capture("raising-run.txt", "\n".join(f"{seq} {name} {p}" for seq, name, p in events) + "\n")
    assert [name for _seq, name, _p in events] == [
        run_cmd.RUN_STARTED,
        "error.raised",
        run_cmd.RUN_FINISHED,
    ], "decision 17's paired finish must fire, or the stream waits for IDLE_CEILING_S"
    assert raised == [
        {
            "class": "HarnessError",
            "message": f"the provider refused {redact.token('anthropic_key')} mid-run",
            "stop_reason": worker.RAISED,
        }
    ]
    assert finished == [{"guardrail": None, "stop_reason": worker.RAISED}]
    assert SEEDED_KEY not in json.dumps(events)

    with sqlite3.connect(home / db.DATABASE_FILENAME) as conn:
        listed = {row.session_id: row.terminal_event for row in events_read.sessions(conn, 50)}
    assert listed[session_id] == "run.finished", "a raising session closes like any other"

    monkeypatch.undo()
    # `undo` also drops the test's own home and cwd; put them back or the run reads the operator's.
    monkeypatch.setenv("HARNESSED_HOME", str(home))
    monkeypatch.chdir(home.parent)
    monkeypatch.setenv(config.PROVIDER_VAR, "fake")
    again = client.post(RUNS, json={"prompt": PING})
    assert again.status_code == 202, "the worker was not left busy"
    settle(app)


def test_a_fixture_miss_mid_run_is_error_raised_not_a_stuck_worker(
    app: Any, client: TestClient, home: Path
) -> None:
    """A real raise from inside the loop: FakeProvider has no fixture for this prompt."""
    response = client.post(RUNS, json={"prompt": "a prompt the corpus has never seen"})
    assert response.status_code == 202, response.text
    settle(app)
    events = events_of(home, response.json()["session_id"])
    raised = [payload for _seq, name, payload in events if name == "error.raised"]
    assert [payload["class"] for payload in raised] == ["FixtureMissError"]
    assert [name for _seq, name, _p in events][-1] == run_cmd.RUN_FINISHED
    assert app.state.worker.run.done()


# ---- AC 4: the prompt is in a body, never a URL, never an access-log line -----------------------


def test_the_prompt_is_in_no_url_and_no_log_line(
    app: Any, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "prompt-census-137"
    seen_urls: list[str] = []
    caplog.set_level(logging.DEBUG)
    response = client.post(
        RUNS, json={"prompt": f"{PING} {marker}"}, extensions={}, follow_redirects=False
    )
    seen_urls.append(str(response.request.url))
    settle(app)
    lines = [record.getMessage() for record in caplog.records]
    capture(
        "prompt-census.txt",
        f"urls {seen_urls}\nlog lines {len(lines)}\n"
        f"urls carrying the prompt {sum(marker in url for url in seen_urls)}\n"
        f"log lines carrying the prompt {sum(marker in line for line in lines)}\n",
    )
    assert json.loads(response.request.content)["prompt"].endswith(marker)
    assert [url for url in seen_urls if marker in url] == []
    assert [line for line in lines if marker in line] == []


def test_uvicorn_access_logging_stays_off_in_the_process_that_mounts_the_control_app() -> None:
    """Decision 22: the one ``uvicorn.run`` passes ``access_log=False`` and no log config."""
    tree = ast.parse(DASHBOARD_CMD.read_text(encoding="utf-8"))
    runs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
    ]
    assert len(runs) == 1
    keywords = {kw.arg: kw.value for kw in runs[0].keywords}
    assert (
        isinstance(keywords["access_log"], ast.Constant) and keywords["access_log"].value is False
    )
    assert isinstance(keywords["log_config"], ast.Constant) and keywords["log_config"].value is None


# ---- AC 5: a refused prompt, a missing key ------------------------------------------------------


@pytest.mark.parametrize("prompt", ["", 7, None, ["ping"]])
def test_an_empty_or_non_string_prompt_is_a_400(app: Any, client: TestClient, prompt: Any) -> None:
    response = client.post(RUNS, json={"prompt": prompt})
    assert response.status_code == 400, response.text
    assert set(response.json()) == {"error", "message"}
    assert response.json()["error"] == "PromptError"
    assert app.state.worker.run is None


def test_a_missing_provider_key_is_the_missing_credential_mapping(
    app: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no `.env` of the operator's can supply the key
    monkeypatch.setenv(config.PROVIDER_VAR, "groq")
    response = client.post(RUNS, json={"prompt": PING})
    capture("missing-credential.txt", f"{response.status_code} {response.text}\n")
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "MissingCredentialError"
    assert "Traceback" not in response.text
    assert app.state.worker.run is not None and app.state.worker.run.done(), "nothing is held"


# ---- AC 6: nothing is in flight after a restart -------------------------------------------------


def test_nothing_is_in_flight_after_a_restart(slow: Path) -> None:
    app = control.create_app(slow)
    with TestClient(app) as client:
        assert client.post(RUNS, json={"prompt": PING}).status_code == 202
    restarted = control.create_app(slow)
    assert restarted.state.worker.run is None and restarted.state.worker.session_id is None
    with TestClient(restarted) as client:
        assert client.post(RUNS, json={"prompt": PING}).status_code == 202
        settle(restarted)


# ---- ticket 140: a follow-up is more iterations of the same run (FR-82) --------------------------

#: The follow-up the fake corpus answers on a continued session (ticket 138's).
FOLLOW_UP = "ping again"

FOLLOW_UP_EVIDENCE = REPO_ROOT / "evidence" / "140"


def messages(session_id: str) -> str:
    return f"{RUNS}/{session_id}/messages"


def capture_140(name: str, text: str) -> Path:
    """Ticket 140's artefacts, beside 137's and never committed."""
    FOLLOW_UP_EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = FOLLOW_UP_EVIDENCE / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def reads(home: Path) -> Iterator[TestClient]:
    """The read gateway over the same home, the way the dashboard process mounts it."""
    with TestClient(gateway.create_app(home / db.DATABASE_FILENAME)) as client:
        yield client


def opened(app: Any, client: TestClient) -> str:
    """Open one run and let it finish: the session a follow-up continues."""
    response = client.post(RUNS, json={"prompt": PING})
    assert response.status_code == 202, response.text
    settle(app)
    return str(response.json()["session_id"])


def test_a_follow_up_answers_202_with_the_same_fields_and_the_same_session_id(
    app: Any, client: TestClient, reads: TestClient, home: Path
) -> None:
    session_id = opened(app, client)
    before = reads.get(f"/v1/runs/{session_id}").json()

    response = client.post(messages(session_id), json={"prompt": FOLLOW_UP})
    assert response.status_code == 202, response.text
    settle(app)
    after = reads.get(f"/v1/runs/{session_id}").json()
    listed = reads.get("/v1/sessions?limit=50").json()["sessions"]
    mine = [row for row in listed if row["session_id"] == session_id]

    capture_140(
        "follow-up.txt",
        f"POST {messages(session_id)} -> {response.status_code} {response.text}\n"
        f"/v1/runs/{session_id} iterations before {before['iterations']} "
        f"after {after['iterations']}\n"
        f"/v1/sessions?limit=50 rows for the id: {len(mine)} of {len(listed)}\n"
        f"{mine}\n",
    )
    assert response.json() == accepted_for(session_id)
    assert after["iterations"] > before["iterations"], (before, after)
    assert after["reply"], "the follow-up's reply is on the same run"
    assert len(listed) == 1 and len(mine) == 1, listed
    assert names_of(home, session_id).count(run_cmd.RUN_STARTED) == 2, "one pair per message"


def accepted_for(session_id: str) -> dict[str, str]:
    """FR-79's fields, spelled here for the id: the follow-up's 202 is the same shape."""
    return {
        "session_id": session_id,
        "stream": f"/v1/sessions/{session_id}/stream",
        "run": f"/v1/runs/{session_id}",
    }


def test_the_route_calls_the_one_continuation_the_cli_calls(
    app: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One composition, two callers (decision 50, MUST rule 16): no second path in the route."""
    session_id = opened(app, client)
    called: list[tuple[str, str, Event | None]] = []
    real = run_cmd.continue_session

    def spy(session: Any, run_key: str, prompt: str, *, announced: Event | None = None) -> Any:
        called.append((run_key, prompt, announced))
        return real(session, run_key, prompt, announced=announced)

    monkeypatch.setattr(run_cmd, "continue_session", spy)
    assert client.post(messages(session_id), json={"prompt": FOLLOW_UP}).status_code == 202
    settle(app)
    assert len(called) == 1
    called_key, called_prompt, called_event = called[0]
    assert (called_key, called_prompt) == (session_id, FOLLOW_UP)
    assert isinstance(called_event, Event)
    assert (called_event.run_id, called_event.name) == (session_id, run_cmd.RUN_STARTED)


def test_a_follow_up_while_a_run_is_in_flight_is_a_409(slow: Path) -> None:
    app = control.create_app(slow)
    with TestClient(app, raise_server_exceptions=False) as client:
        first = client.post(RUNS, json={"prompt": PING})
        session_id = first.json()["session_id"]
        second = client.post(messages(session_id), json={"prompt": FOLLOW_UP})
        settle(app)
    capture_140("follow-up-409.txt", f"{second.status_code} {second.text}\n")
    assert second.status_code == 409, second.text
    assert set(second.json()) == {"error", "message"}
    assert second.json()["error"] == "RunInFlight"
    assert session_id in second.json()["message"]
    assert names_of(slow, session_id).count(run_cmd.RUN_STARTED) == 1, "the 409 started nothing"


def test_a_follow_up_to_a_session_no_journal_holds_is_a_404_that_publishes_nothing(
    app: Any, client: TestClient, home: Path
) -> None:
    response = client.post(messages("run_nobody"), json={"prompt": FOLLOW_UP})
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "SurfaceNotFound"
    assert events_of(home, "run_nobody") == [], "a refused follow-up publishes nothing"


def test_a_follow_up_to_a_case_run_key_is_refused_as_a_400(
    app: Any, client: TestClient, home: Path
) -> None:
    key = "eval_x:smoke-001-capital:1"
    response = client.post(messages(key), json={"prompt": FOLLOW_UP})
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "SessionNotContinued"
    assert events_of(home, key) == []


@pytest.mark.parametrize("body", [{"prompt": ""}, {"prompt": 7}, {"messages": [PING]}, [PING]])
def test_a_follow_up_body_is_one_string_under_prompt(
    app: Any, client: TestClient, body: Any
) -> None:
    session_id = opened(app, client)
    response = client.post(messages(session_id), json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "PromptError"


# ---- ticket 164 (FR-103): the same uncertain stop, through the control app -----------------------


@pytest.fixture
def uncertain_decider(monkeypatch: pytest.MonkeyPatch) -> ScopedFakeDecider:
    """Force ticket 164's committed uncertain-stop fixture regardless of the prompt (its own
    fixture note: a declared scope, never the hash fallback ``ping`` already owns)."""
    decider = ScopedFakeDecider(fake=FakeDecider(config.load()), scope="uncertain-stop")
    monkeypatch.setattr(run_cmd, "build_decider", lambda settings, **_kwargs: decider)
    return decider


def test_the_control_app_settles_the_same_uncertain_stop_and_frees_the_composer(
    app: Any, client: TestClient, home: Path, uncertain_decider: ScopedFakeDecider
) -> None:
    response = client.post(RUNS, json={"prompt": PING})
    assert response.status_code == 202, response.text
    session_id = response.json()["session_id"]
    settle(app)

    names = names_of(home, session_id)
    assert names == ["run.started", "loop.route.decided", "run.finished"]
    assert "error.raised" not in names
    payload = events_of(home, session_id)[-1][2]
    assert payload == {"guardrail": None, "stop_reason": "below_floor"}

    # the composer is free for the next message: a second POST is accepted, not a 409.
    again = client.post(RUNS, json={"prompt": "hello"})
    assert again.status_code == 202, again.text
    settle(app)

    capture_164(
        "control-app-stream-close.txt",
        f"ticket 164 - control app, session {session_id}\n"
        + "\n".join(names)
        + f"\nrun.finished payload: {payload}\n"
        f"second POST after the stop: {again.status_code}\n",
    )


# ---- ticket 240 (FR-141, decision 77): a message on a task's session -----------------------------

from tests.support.task_world import CannedProvider, ScriptedDecider, answer, install  # noqa: E402

from harnessed.cli import task_run  # noqa: E402
from harnessed.tasks import store as task_store  # noqa: E402


def _task_rows(sql: str, *params: Any) -> list[tuple[Any, ...]]:
    conn = db.connect()
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _queue() -> str:
    conn = db.connect()
    try:
        return str(task_store.queue(conn, "tidy the notes", via="cli")["task_id"])
    finally:
        conn.close()


def test_a_message_on_a_task_paused_uncertain_resumes_it_as_the_operators(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decider = ScriptedDecider([answer("needs_human", "0.60"), answer("groq_chat"), answer("done")])
    install(monkeypatch, decider, CannedProvider())
    task_id = _queue()
    paused = task_run.run_task(task_id)
    session_id = str(paused["session_id"])
    assert paused["pause_reason"] == "uncertain"

    app = control.create_app(home)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            f"{RUNS}/{session_id}/messages", json={"prompt": "use the blue folder"}
        )
        assert response.status_code == 202, response.text
        assert response.json()["session_id"] == session_id
        settle(app)

    row = _task_rows("SELECT state, human_messages, pauses FROM tasks WHERE task_id = ?", task_id)
    assert row == [("done", 1, 1)]
    names = names_of(home, session_id)
    assert names[-2:] == ["task.finished", "run.finished"]


def test_a_message_on_a_task_waiting_for_an_answer_is_refused_before_any_event(
    home: Path,
) -> None:
    task_id = _queue()
    conn = db.connect()
    try:
        task_store.begin_message(
            conn,
            task_id,
            run_id="run_00000000000000240WAITING0",
            message_seq=1,
            author="task",
            first=True,
        )
        task_store.pause_for_permission(conn, task_id, "perm_x")
    finally:
        conn.close()
    session_id = "run_00000000000000240WAITING0"
    before = events_of(home, session_id)
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        response = client.post(f"{RUNS}/{session_id}/messages", json={"prompt": "hurry up"})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "TaskAwaitingAnswer"
    assert body["message"].startswith("the task on session ")
    assert "waiting for your answer to the agent's request" in body["message"]
    assert events_of(home, session_id) == before, "nothing was published for the refused message"


def test_a_message_on_a_running_task_is_run_in_flight(home: Path) -> None:
    task_id = _queue()
    session_id = "run_00000000000000240RUNNING0"
    conn = db.connect()
    try:
        task_store.begin_message(
            conn, task_id, run_id=session_id, message_seq=1, author="task", first=True
        )
    finally:
        conn.close()
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        response = client.post(f"{RUNS}/{session_id}/messages", json={"prompt": "hello"})
    assert (response.status_code, response.json()["error"]) == (409, "RunInFlight")


def test_in_flight_409_says_a_task_is_running_and_what_to_do(home: Path) -> None:
    task_id = _queue()
    session_id = "run_00000000000000304RUNNING0"
    conn = db.connect()
    try:
        task_store.begin_message(
            conn, task_id, run_id=session_id, message_seq=1, author="task", first=True
        )
    finally:
        conn.close()
    with TestClient(control.create_app(home), raise_server_exceptions=False) as client:
        response = client.post(f"{RUNS}/{session_id}/messages", json={"prompt": "hello"})
    assert response.status_code == 409
    assert response.json()["message"] == (
        f"a task is running: {task_id} tidy the notes; "
        f"wait for it, or harnessed task cancel {task_id}"
    )


def test_a_message_on_a_finished_task_is_an_ordinary_follow_up_with_no_counters(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(
        monkeypatch,
        ScriptedDecider([answer("groq_chat"), answer("done"), answer("groq_chat")]),
        CannedProvider(),
    )
    task_id = _queue()
    done = task_run.run_task(task_id)
    session_id = str(done["session_id"])
    assert done["state"] == "done"
    rows_before = _task_rows("SELECT COUNT(*) FROM task_messages WHERE task_id = ?", task_id)

    app = control.create_app(home)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(f"{RUNS}/{session_id}/messages", json={"prompt": "thanks, one more"})
        assert response.status_code == 202, response.text
        settle(app)

    assert (
        _task_rows("SELECT COUNT(*) FROM task_messages WHERE task_id = ?", task_id) == rows_before
    )
    after = task_store.get(db.connect(), task_id)
    assert after is not None
    assert (after["state"], after["human_messages"], after["pauses"]) == ("done", "0", "0")
