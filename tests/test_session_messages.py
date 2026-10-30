"""Ticket 182 (FR-128, decision 66): a session's messages, the activity list, and titled sessions.

``loop/messages.py::for_session`` is the events' ``run.started``/``run.finished`` pairs (decision
51) joined on ``message_seq`` against ``decisions``, ``turns``, ``agent_turns`` and ``outputs``;
``recent`` is the newest-first activity list over hop-0 decisions alone; ``events/read.py::
sessions`` gains ``title``, the first message's task. Sessions are built directly against
``Session`` and a ``_StubDecider``, the same seam tickets 160 to 176 already proved routing with -
no CLI process and no dashboard fixture recording needed to prove the store read.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harnessed import config
from harnessed.cli.run_cmd import Session, continue_session, solve_with_memory
from harnessed.errors import SurfaceNotFound
from harnessed.events import read as events_read
from harnessed.events.bus import EventBus
from harnessed.gateway import create_app
from harnessed.loop import messages
from harnessed.memory import db
from harnessed.providers.fake import FakeProvider
from harnessed.tools import default_registry
from test_router import (
    CHAIN_TASK,
    EMAIL_OUTPUT,
    _chain_session,
    _continuation_decision,
    _decisions,
    _StubDecider,
)

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence" / "182"


def capture(name: str, text: str, *, append: bool = False) -> Path:
    """Write one evidence artefact under ``evidence/182/``. Regenerated on every verify run;
    never committed (``evidence/`` is gitignored)."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    if append and path.exists():
        path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")
    else:
        path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


def _groq_session(conn: sqlite3.Connection, confidence: str = "0.93") -> Session:
    """A session that routes every message to ``groq_chat`` above its floor."""
    settings = config.load(provider="fake")
    return Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=default_registry(settings.home),
        provider=FakeProvider(settings),
        decider=_StubDecider(decisions=_decisions("groq_chat", confidence)),
    )


def _unrouted_session(conn: sqlite3.Connection) -> Session:
    """A session with no decider at all: the pre-router shape (``decider=None``)."""
    settings = config.load(provider="fake")
    return Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=default_registry(settings.home),
        provider=FakeProvider(settings),
    )


# ---- AC1: the wireframe-then-email chain is one message, two hops of outputs, email the reply ---


def test_the_chain_is_one_message_with_two_hops_and_the_synthesis_as_the_reply(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd

    session, decider, provider = _chain_session(conn, tmp_path, "chain-wireframe-email")
    memory_run = run_cmd.solve_with_memory(session, CHAIN_TASK)
    assert memory_run.result is not None
    assert memory_run.result.reply.startswith("The wireframe and the launch email")

    (decision_rows,) = (
        conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE run_id = ?", (memory_run.run_id,)
        ).fetchone(),
    )
    assert decision_rows == (3,), "the chain writes three decisions: wireframe, email, done"

    (found,) = messages.for_session(conn, memory_run.run_id)
    assert found.text == CHAIN_TASK
    assert found.text_reason is None
    assert found.outcome == "routed"
    assert found.handler is None, "the last decision is the 'done' hop, which carries no handler"
    assert len(found.hops) == 2, "two delegations, wireframe and email - 'done' delegates nothing"
    assert [hop.hop for hop in found.hops] == [0, 1]
    for hop in found.hops:
        assert len(hop.outputs) == 1, "each hop's own text result, position 0, no files declared"
    assert found.reply is not None
    assert found.reply.text == memory_run.result.reply
    assert found.reply.output_kind == "text"
    assert found.reply.output_id is None, "the reply is the synthesis turn, not a hop output"
    assert found.steps == 1
    (email,) = found.hops[1].outputs
    assert email.output_kind == "text", "the email stays a hop output, one click away"
    assert found.reply_reason is None
    assert found.last_seq is not None, "the chain's run.finished paired with its own start"

    capture(
        "chain-message.txt",
        "ticket 182 AC1 - the wireframe-then-email chain is one message\n\n"
        f"text={found.text!r}\n"
        f"decisions={decision_rows}\n"
        f"hops={[hop.hop for hop in found.hops]}\n"
        f"reply={found.reply}\n"
        f"first_seq={found.first_seq} last_seq={found.last_seq}\n",
    )


def test_a_turn_is_the_reply_even_when_a_hop_output_exists(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd

    session, _decider, _provider = _chain_session(conn, tmp_path, "chain-wireframe-email")
    memory_run = run_cmd.solve_with_memory(session, CHAIN_TASK)

    (found,) = messages.for_session(conn, memory_run.run_id)
    assert found.hops, "the hop outputs exist"
    assert found.reply is not None
    assert (found.reply.output_kind, found.reply.output_id) == ("text", None)
    assert found.reply.text == memory_run.result.reply  # type: ignore[union-attr]
    assert found.reply_reason is None
    assert [len(hop.outputs) for hop in found.hops] == [1, 1], "hops still lists every output"


def test_a_message_with_outputs_and_no_turn_still_serves_its_last_hop(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd

    session, _decider, _provider = _chain_session(conn, tmp_path, "chain-wireframe-email")
    memory_run = run_cmd.solve_with_memory(session, CHAIN_TASK)
    conn.execute("DELETE FROM turns WHERE run_id = ?", (memory_run.run_id,))  # pre-198 shape

    (found,) = messages.for_session(conn, memory_run.run_id)
    assert found.reply is not None
    assert found.reply.text == EMAIL_OUTPUT
    assert found.reply.output_id == found.hops[1].outputs[0].output_id
    assert found.reply_reason is None


# ---- AC2: a three-message session serves in order, first_seq/last_seq per message ---------------


def test_a_three_message_session_serves_in_order_with_first_and_last_seq(
    conn: sqlite3.Connection,
) -> None:
    session = _groq_session(conn)
    first = solve_with_memory(session, "three message session")
    second = continue_session(session, first.run_id, "second turn")
    third = continue_session(session, first.run_id, "third turn")
    assert first.run_id == second.run_id == third.run_id

    found = messages.for_session(conn, first.run_id)
    assert len(found) == 3
    assert [row.text for row in found] == [
        "three message session",
        "second turn",
        "third turn",
    ]
    assert [row.first_seq for row in found] == sorted(row.first_seq for row in found)
    for row in found:
        assert isinstance(row.first_seq, int)
        assert isinstance(row.last_seq, int), "every message here completed"
        assert row.first_seq < row.last_seq
    # Consecutive messages never overlap: one pair's finish precedes the next pair's start.
    for earlier, later in itertools.pairwise(found):
        assert earlier.last_seq < later.first_seq

    capture(
        "three-message-session.txt",
        "ticket 182 AC2 - a three-message session, in order\n\n"
        + "\n".join(
            f"message_seq={row.message_seq} first_seq={row.first_seq} "
            f"last_seq={row.last_seq} text={row.text!r}"
            for row in found
        )
        + "\n",
    )


def test_a_live_message_has_a_null_last_seq(conn: sqlite3.Connection) -> None:
    """A ``run.started`` with no paired finish yet: ``last_seq`` is null, the rest still reads."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})

    (found,) = messages.for_session(conn, bus.run_id)
    assert found.first_seq == 1
    assert found.last_seq is None
    assert found.seconds is None
    assert found.text is None
    assert found.text_reason == messages.TEXT_BEFORE_ROUTER
    assert found.reply is None
    assert found.reply_reason == messages.REPLY_NOTHING_RAN


_SEED_DECISION = (
    "INSERT INTO decisions (run_id, idx, message_seq, hop, task, outcome, route, handler, "
    "state_truncated, latency_seconds, model, created_at, result, result_reason) "
    "VALUES (?, ?, ?, ?, ?, 'routed', ?, ?, 0, '0.1', 'jev', '2026-01-01T00:00:00Z', ?, ?)"
)


def _seed_hop(
    conn: sqlite3.Connection,
    run_id: str,
    hop: int,
    *,
    route: str,
    handler: str,
    result: str | None,
    reason: str | None,
) -> None:
    """One ``decisions`` row for the message that started at ``seq`` 1, as 018 settles it."""
    conn.execute(
        _SEED_DECISION,
        (run_id, hop, 1, hop, "do it" if hop == 0 else None, route, handler, result, reason),
    )
    conn.commit()


def _failed_message(
    conn: sqlite3.Connection, route: str, result: str | None, reason: str | None
) -> messages.MessageRow:
    """A started/finished pair whose one agent hop settled as given; no turn, no output."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    _seed_hop(conn, bus.run_id, 0, route=route, handler="agent", result=result, reason=reason)
    bus.publish("run.finished", {})
    (found,) = messages.for_session(conn, bus.run_id)
    return found


def test_a_wrote_files_hop_reads_failed_and_is_present_in_hops(conn: sqlite3.Connection) -> None:
    found = _failed_message(conn, "agent_y", "failed", "wrote_files")
    assert found.outcome == "failed", "never `routed` with the failure hidden"
    assert found.reply is None
    assert found.reply_reason == "the delegation to agent_y failed: wrote_files"
    (hop,) = found.hops
    assert (hop.hop, hop.result, hop.reason) == (0, "failed", "wrote_files")
    assert hop.outputs == ()
    (summary,) = messages.recent(conn)
    assert summary.outcome == "failed"


def test_a_pre_018_hop_reads_result_null_with_its_reason(conn: sqlite3.Connection) -> None:
    found = _failed_message(conn, "agent_y", None, None)
    assert found.outcome == "routed"
    (hop,) = found.hops
    assert hop.result is None
    assert hop.reason == "recorded before Harnessed recorded a hop's result"


def test_a_succeeded_hop_carries_no_reason(conn: sqlite3.Connection) -> None:
    found = _failed_message(conn, "agent_y", "succeeded", None)
    assert found.outcome == "routed"
    assert (found.hops[0].result, found.hops[0].reason) == ("succeeded", None)


def test_the_last_failed_hop_names_the_reason(conn: sqlite3.Connection) -> None:
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    _seed_hop(conn, bus.run_id, 0, route="a", handler="agent", result="failed", reason="error")
    _seed_hop(conn, bus.run_id, 1, route="b", handler="agent", result="failed", reason="timeout")
    bus.publish("run.finished", {})
    (found,) = messages.for_session(conn, bus.run_id)
    assert found.reply_reason == "the delegation to b failed: timeout"


def test_a_groq_hop_that_raised_is_a_failed_hop(conn: sqlite3.Connection) -> None:
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    _seed_hop(
        conn, bus.run_id, 0, route="groq_chat", handler="groq", result="failed", reason="raised"
    )
    (found,) = messages.for_session(conn, bus.run_id)
    assert found.outcome == "failed"
    assert [(hop.hop, hop.reason) for hop in found.hops] == [(0, "raised")]


def test_a_message_that_ran_nothing_says_so(conn: sqlite3.Connection) -> None:
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    bus.publish("run.finished", {})
    (found,) = messages.for_session(conn, bus.run_id)
    assert found.reply_reason == messages.REPLY_NOTHING_RAN


def _routed_message(conn: sqlite3.Connection, route: str, outcome: str) -> messages.MessageRow:
    """A message whose only decision settled as given and which ran nothing."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    conn.execute(
        "INSERT INTO decisions (run_id, idx, message_seq, hop, task, outcome, route, handler, "
        "state_truncated, latency_seconds, model, created_at) "
        "VALUES (?, 0, 1, 0, 'do it', ?, ?, NULL, 0, '0.1', 'jev', '2026-01-01T00:00:00Z')",
        (bus.run_id, outcome, route),
    )
    conn.commit()
    bus.publish("run.finished", {})
    (found,) = messages.for_session(conn, bus.run_id)
    return found


def test_a_message_routed_done_reads_as_finished_not_as_an_uncertain_stop(
    conn: sqlite3.Connection,
) -> None:
    done = _routed_message(conn, "done", "routed")
    assert done.outcome == "routed"
    assert done.reply is None
    assert done.reply_reason == messages.REPLY_TASK_DONE
    assert done.reply_reason != messages.REPLY_NOTHING_RAN


def test_an_uncertain_stop_and_needs_human_still_read_as_nothing_ran(
    conn: sqlite3.Connection,
) -> None:
    assert _routed_message(conn, "none", "uncertain").reply_reason == messages.REPLY_NOTHING_RAN
    assert _routed_message(conn, "needs_human", "uncertain").reply_reason == (
        messages.REPLY_NOTHING_RAN
    )


def test_the_message_reads_no_event_payloads() -> None:
    source = (REPO_ROOT / "src" / "harnessed" / "loop" / "messages.py").read_text(encoding="utf-8")
    assert "json_extract" not in source


# ---- AC3: an eval session is 200 [], an unknown id is 404, a pre-router message is null text ----


def test_an_eval_session_reads_as_an_empty_list(conn: sqlite3.Connection) -> None:
    bus = EventBus(conn=conn)
    bus.publish("eval.run.started", {"suite": "smoke"})
    bus.publish("eval.run.finished", {"suite": "smoke"})
    found = messages.for_session(conn, bus.run_id)
    assert found == ()
    capture(
        "empty-and-404.txt",
        "ticket 182 AC3 - an eval session and an unknown id\n\n"
        f"eval session {bus.run_id} -> 200 {list(found)!r}\n",
    )


def test_an_unknown_session_id_is_404(conn: sqlite3.Connection) -> None:
    with pytest.raises(SurfaceNotFound) as excinfo:
        messages.for_session(conn, "run_00000000000000000000000000")
    capture(
        "empty-and-404.txt",
        f"unknown session -> 404 {excinfo.value}\n",
        append=True,
    )


def test_a_message_recorded_before_the_router_has_a_null_text_with_its_reason(
    conn: sqlite3.Connection,
) -> None:
    session = _unrouted_session(conn)
    memory_run = solve_with_memory(session, "ping")
    assert memory_run.result is not None

    (found,) = messages.for_session(conn, memory_run.run_id)
    assert found.text is None
    assert found.text_reason == messages.TEXT_BEFORE_ROUTER
    assert found.handler is None
    assert found.outcome is None
    # Nothing is guessed from run_messages: the reply still comes from the turns ledger.
    assert found.reply is not None
    assert found.reply.text == memory_run.result.reply
    assert found.reply.output_id is None
    assert found.reply_reason is None


# ---- AC4: /v1/messages?limit= is newest first over hop-0 decisions, a bad limit is 400 ----------


def test_recent_serves_newest_first_over_hop_zero_decisions(conn: sqlite3.Connection) -> None:
    settings = config.load(provider="fake")
    decider = _StubDecider(decisions=_decisions("groq_chat", "0.93"))
    older = Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=default_registry(settings.home),
        provider=FakeProvider(settings),
        decider=decider,
    )
    older_run = solve_with_memory(older, "ping")

    newer = Session(
        settings=settings,
        conn=conn,
        bus=EventBus(conn=conn),
        tools=default_registry(settings.home),
        provider=FakeProvider(settings),
        decider=_StubDecider(decisions=_decisions("groq_chat", "0.93")),
    )
    newer_run = solve_with_memory(newer, "three message session")

    found = messages.recent(conn, limit=20)
    assert [row.session_id for row in found][:2] == [newer_run.run_id, older_run.run_id]
    assert found[0].text == "three message session"
    assert found[0].outcome == "routed"
    assert found[0].handler == "groq"


def test_a_bad_limit_on_the_messages_route_is_400(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    app_over = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    with TestClient(app_over, raise_server_exceptions=False) as client:
        response = client.get("/v1/messages?limit=0")
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "LimitError"


# ---- AC5: /v1/sessions carries title, null before the router ------------------------------------


def test_a_routed_sessions_title_is_its_first_messages_task(conn: sqlite3.Connection) -> None:
    session = _groq_session(conn)
    routed_run = solve_with_memory(session, "ping")
    unrouted_run = solve_with_memory(_unrouted_session(conn), "ping")

    rows = {row.session_id: row for row in events_read.sessions(conn, limit=20)}
    assert rows[routed_run.run_id].title == "ping"
    assert rows[unrouted_run.run_id].title is None

    capture(
        "titled-listing.txt",
        "ticket 182 AC5 - /v1/sessions carries title, null before the router\n\n"
        f"routed session {routed_run.run_id}: title={rows[routed_run.run_id].title!r}\n"
        f"unrouted session {unrouted_run.run_id}: title={rows[unrouted_run.run_id].title!r}\n",
    )


# ---- AC6: the gateway and CLI answer the same bytes for both reads -------------------------------


def test_the_gateway_and_cli_serve_the_same_bytes(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    from typer.testing import CliRunner

    from harnessed.cli.app import app

    session = _groq_session(conn)
    routed_run = solve_with_memory(session, "ping")
    conn.commit()

    cli = CliRunner(mix_stderr=False)
    app_over = create_app(tmp_harnessed_home / db.DATABASE_FILENAME)
    with TestClient(app_over, raise_server_exceptions=False) as client:
        session_response = client.get(f"/v1/sessions/{routed_run.run_id}/messages")
        recent_response = client.get("/v1/messages")

    session_stdout = cli.invoke(
        app, ["sessions", "messages", routed_run.run_id, "--json"]
    ).stdout.encode("utf-8")
    recent_stdout = cli.invoke(app, ["sessions", "recent", "--json"]).stdout.encode("utf-8")

    assert session_response.status_code == 200, session_response.text
    assert session_stdout[:-1] == session_response.content
    assert recent_stdout[:-1] == recent_response.content

    capture(
        "parity-rows.txt",
        "ticket 182 AC6 - the gateway and CLI serve the same bytes\n\n"
        f"GET /v1/sessions/{routed_run.run_id}/messages -> {session_response.status_code}, "
        f"{len(session_response.content)} bytes, CLI byte-identical\n"
        f"GET /v1/messages -> {recent_response.status_code}, "
        f"{len(recent_response.content)} bytes, CLI byte-identical\n",
    )


def test_a_quota_refused_hop_is_the_reply_reason(conn: sqlite3.Connection) -> None:
    found = _failed_message(conn, "x", "failed", "quota_refused")
    assert found.reply_reason == "the delegation to x failed: quota_refused"


def test_the_gateway_serves_a_failed_hop_as_failed(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    found = _failed_message(conn, "agent_y", "failed", "wrote_files")
    run_id = conn.execute("SELECT run_id FROM decisions").fetchone()[0]
    with TestClient(create_app(tmp_harnessed_home / db.DATABASE_FILENAME)) as client:
        body = client.get(f"/v1/sessions/{run_id}/messages").json()
        recent = client.get("/v1/messages").json()
    (message,) = body["messages"]
    assert found.outcome == message["outcome"] == "failed"
    assert message["hops"] == [
        {
            "hop": 0,
            "outputs": [],
            "result": "failed",
            "reason": "wrote_files",
            "repository": None,
            "repository_reason": messages.REPOSITORY_NO_DELEGATION,
            "compared": None,
            "compared_reason": "the router acted",
        }
    ]
    assert recent["messages"][0]["outcome"] == "failed"
    capture("failed-hop-body.json", json.dumps(message, indent=2, sort_keys=True) + "\n")


def test_a_chain_settles_every_agent_hop_and_the_groq_hop_succeeded(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harnessed.cli import run_cmd

    session, _decider, _provider = _chain_session(conn, tmp_path, "chain-wireframe-email")
    memory_run = run_cmd.solve_with_memory(session, CHAIN_TASK)
    rows = conn.execute(
        "SELECT hop, handler, result, result_reason FROM decisions WHERE run_id = ? ORDER BY hop",
        (memory_run.run_id,),
    ).fetchall()
    agent_rows = [row for row in rows if row[1] == "agent"]
    assert agent_rows and all(row[2:] == ("succeeded", None) for row in agent_rows)


def test_a_failure_settles_under_the_reason_its_class_names() -> None:
    from harnessed.cli import routed
    from harnessed.errors import (
        AcpVersionMismatch,
        DelegationError,
        DelegationTimeout,
        QuotaRefused,
    )

    assert routed.failure_reason(DelegationTimeout("slow")) == "timeout"
    assert routed.failure_reason(AcpVersionMismatch("old")) == "version_mismatch"
    assert routed.failure_reason(QuotaRefused("out")) == "quota_refused"
    assert routed.failure_reason(DelegationError("x (reason: wrote_files)")) == "wrote_files"
    assert routed.failure_reason(DelegationError("x")) == "error"
    assert routed.failure_reason(RuntimeError("x")) == "raised"


def test_a_run_publishes_memory_write_committed_for_its_episode_before_run_finished(
    conn: sqlite3.Connection,
) -> None:
    """Ticket 232: the episode's own id, on the session's bus, ahead of ``run.finished``."""
    memory_run = solve_with_memory(_groq_session(conn), "ping")
    names = [event.name for event in events_read.replay(conn, memory_run.run_id)]
    assert names.count("memory.write.committed") == 1
    assert names.index("memory.write.committed") < names.index("run.finished")
    committed = next(
        event
        for event in events_read.replay(conn, memory_run.run_id)
        if event.name == "memory.write.committed"
    )
    (episode_id,) = conn.execute(
        "SELECT episode_id FROM mem_episodic WHERE run_id = ?", (memory_run.run_id,)
    ).fetchone()
    assert dict(committed.payload) == {"kind": "episode", "episode_id": episode_id}


def test_memory_add_publishes_nothing_and_says_so() -> None:
    from harnessed.cli import memory_cmd

    assert "Publishes nothing" in (memory_cmd.add.__doc__ or "")


def test_the_sessions_listing_carries_the_newest_hop_zero_decision(
    conn: sqlite3.Connection,
) -> None:
    """Ticket 232: ``last_handler``, ``last_outcome``, ``last_cost`` and ``last_at`` as strings,
    the entry's own name for an agent route and ``groq`` for a chat route."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    _seed_hop(
        conn, bus.run_id, 0, route="agent_y", handler="agent", result="failed", reason="error"
    )
    conn.execute("UPDATE decisions SET cost = '0.0010'")
    conn.commit()
    (row,) = events_read.sessions(conn)
    assert (row.last_handler, row.last_outcome, row.last_cost, row.last_at) == (
        "agent_y",
        "failed",
        "0.0010",
        "2026-01-01T00:00:00Z",
    )
    conn.execute("UPDATE decisions SET handler = 'groq', route = 'groq_chat', result = NULL")
    conn.commit()
    (row,) = events_read.sessions(conn)
    assert (row.last_handler, row.last_outcome) == ("groq", "routed")


def test_a_session_that_never_routed_has_null_last_fields(conn: sqlite3.Connection) -> None:
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    (row,) = events_read.sessions(conn)
    assert (row.last_handler, row.last_outcome, row.last_cost, row.last_at) == (None,) * 4


# ---- ticket 241 (FR-143, G-37, G-62, G-63): each message's author and the session's task --------


def _pair(conn: sqlite3.Connection, bus: EventBus) -> None:
    bus.publish("run.started", {"provider": "fake"})
    bus.publish("run.finished", {})


def _task_message(conn: sqlite3.Connection, run_id: str, seq: int, kind: str) -> None:
    conn.execute(
        "INSERT INTO task_messages (run_id, message_seq, task_id, kind) VALUES (?, ?, 'task_x', ?)",
        (run_id, seq, kind),
    )
    conn.commit()


def test_a_message_with_no_task_row_reads_operator_and_a_task_row_reads_task(
    conn: sqlite3.Connection,
) -> None:
    bus = EventBus(conn=conn)
    _pair(conn, bus)
    _pair(conn, bus)
    _pair(conn, bus)
    first, second, third = messages.for_session(conn, bus.run_id)
    _task_message(conn, bus.run_id, first.message_seq, "first")
    _task_message(conn, bus.run_id, second.message_seq, "continuation")
    served = messages.messages_payload(messages.for_session(conn, bus.run_id))["messages"]
    assert [(m["author"], m["task_message"]) for m in served] == [  # type: ignore[index]
        ("task", "first"),
        ("task", "continuation"),
        ("operator", None),
    ]
    assert third.author == "operator" and third.task_message is None


def test_the_payload_carries_the_sessions_task_once_or_null(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    from harnessed.tasks import store

    bus = EventBus(conn=conn)
    _pair(conn, bus)
    assert messages.session_payload(conn, bus.run_id)["task"] is None
    task_id = str(store.queue(conn, "tidy the notes", via="cli")["task_id"])
    conn.execute(
        "UPDATE tasks SET session_id = ?, state = 'done', stop_reason = 'finished' "
        "WHERE task_id = ?",
        (bus.run_id, task_id),
    )
    conn.commit()
    assert messages.session_payload(conn, bus.run_id)["task"] == {
        "task_id": task_id,
        "text": "tidy the notes",
        "state": "done",
        "unattended": "true",
    }
    conn.execute("UPDATE tasks SET pauses = 1 WHERE task_id = ?", (task_id,))
    conn.commit()
    task = messages.session_payload(conn, bus.run_id)["task"]
    assert task["unattended"] == "false"  # type: ignore[index]
    client = TestClient(create_app(tmp_harnessed_home / db.DATABASE_FILENAME))
    assert client.get(f"/v1/sessions/{bus.run_id}/messages").json()["task"] == task


def test_recent_rows_carry_author_and_task_id(conn: sqlite3.Connection) -> None:
    bus = EventBus(conn=conn)
    _pair(conn, bus)
    _pair(conn, bus)
    first, second = messages.for_session(conn, bus.run_id)
    for row in (first, second):
        conn.execute(
            _SEED_DECISION,
            (
                bus.run_id,
                row.message_seq,
                row.message_seq,
                0,
                "do it",
                "groq_chat",
                "groq",
                None,
                None,
            ),
        )
    conn.commit()
    _task_message(conn, bus.run_id, first.message_seq, "first")
    by_seq = {row.message_seq: row for row in messages.recent(conn)}
    assert (by_seq[first.message_seq].author, by_seq[first.message_seq].task_id) == (
        "task",
        "task_x",
    )
    assert (by_seq[second.message_seq].author, by_seq[second.message_seq].task_id) == (
        "operator",
        None,
    )
    served = messages.recent_payload(messages.recent(conn))["messages"]
    assert {"author", "task_id"} <= set(served[0])  # type: ignore[index]


def test_task_done_continuation_reply_reads_the_task_is_finished(
    conn: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """A finished task's last continuation reads "the task is finished", never an uncertain
    stop's words (FR-168, decision 102)."""
    from harnessed.tasks import store

    bus = EventBus(conn=conn)
    _pair(conn, bus)
    _pair(conn, bus)
    first, second = messages.for_session(conn, bus.run_id)
    task_id = str(store.queue(conn, "tidy the notes", via="cli")["task_id"])
    conn.execute(
        "UPDATE tasks SET session_id = ?, state = 'done', stop_reason = 'finished' "
        "WHERE task_id = ?",
        (bus.run_id, task_id),
    )
    for row, kind in ((first, "first"), (second, "continuation")):
        conn.execute(
            "INSERT INTO task_messages (run_id, message_seq, task_id, kind) VALUES (?, ?, ?, ?)",
            (bus.run_id, row.message_seq, task_id, kind),
        )
    conn.commit()
    found = messages.for_session(conn, bus.run_id)
    assert found[1].reply is None
    assert found[1].reply_reason == messages.REPLY_TASK_FINISHED == "the task is finished"
    assert found[0].reply_reason != messages.REPLY_TASK_FINISHED
    conn.execute("UPDATE tasks SET state = 'needs_human', stop_reason = NULL")
    conn.commit()
    assert messages.for_session(conn, bus.run_id)[1].reply_reason == messages.REPLY_NOTHING_RAN


# ---- ticket 291 (FR-171, decision 101): a hop carries the figure its stop compared ----


def test_the_messages_compared_expression_is_the_stores_word_for_word() -> None:
    from harnessed.router import decisions as decisions_store

    assert messages._COMPARED_SQL == decisions_store.COMPARED_SQL


def test_an_uncertain_hop_serves_the_figure_it_compared_and_an_acted_one_says_so(
    conn: sqlite3.Connection,
) -> None:
    _continuation_decision(conn, session="run_h1", stored="0.78", done_probability="0.78")
    _continuation_decision(conn, session="run_h2", stored="0.78", done_probability="0.82")
    _continuation_decision(
        conn, session="run_h3", stored="0.9", done_probability="0.9", outcome="routed"
    )
    # a routed decision with an agent handler is a hop of its own; an uncertain one has none
    conn.execute("UPDATE decisions SET handler = 'agent' WHERE run_id = 'run_h3'")
    conn.commit()
    served = {}
    for run in ("run_h1", "run_h2", "run_h3"):
        (found,) = messages.for_session(conn, run)
        (hop,) = messages.messages_payload((found,))["messages"][0]["hops"]  # type: ignore[index]
        served[run] = (hop["compared"], hop["compared_reason"])
    assert served == {
        "run_h1": ("done_probability", None),
        "run_h2": ("confidence", None),
        "run_h3": (None, "the router acted"),
    }


# ---- routing off (ticket 316, FR-175, decision 105) -------------------------------------------


def test_a_message_with_no_decision_and_routing_off_serves_a_null_route_and_why(
    conn: sqlite3.Connection,
) -> None:
    from harnessed.cli import run_cmd

    session = _unrouted_session(conn)
    memory_run = run_cmd.solve_with_memory(session, "ping")

    (found,) = messages.for_session(conn, memory_run.run_id)
    assert found.route is None
    assert found.route_reason == messages.ROUTING_OFF == "routing is off"
    assert found.reply is not None, "the reply comes from Groq chat all the same"
    wire = messages.session_payload(conn, memory_run.run_id)["messages"]
    assert isinstance(wire, list)
    assert wire[0]["route"] is None and wire[0]["route_reason"] == "routing is off"


def test_a_routed_message_serves_its_route_and_no_reason(conn: sqlite3.Connection) -> None:
    from harnessed.cli import run_cmd

    memory_run = run_cmd.solve_with_memory(_groq_session(conn), "ping")

    (found,) = messages.for_session(conn, memory_run.run_id)
    assert found.route == "groq_chat"
    assert found.route_reason is None


def test_a_message_recorded_before_routing_was_marked_says_so(conn: sqlite3.Connection) -> None:
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})

    (found,) = messages.for_session(conn, bus.run_id)
    assert found.route is None
    assert found.route_reason == messages.TEXT_BEFORE_ROUTER
