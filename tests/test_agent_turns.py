"""A subscription agent is never proxied; its transcript turns are journaled (ticket 171, FR-112).

Decision 63: ``agent_turns`` is a sibling of ``turns``, owned by ``acp/turns.py``. The two recorded
transcripts under ``tests/fixtures/acp/`` are the operator's own delegations (see the README there),
scrubbed and hand-reviewed, never generated (MUST rule 8):

* ``reports-cost.json`` - an ``oauth`` agent that sends four ``usage_update`` frames, the last one
  carrying ``cost {amount, currency}``. That figure is an **API-equivalent** cost, stored as
  reported and never recomputed.
* ``not-metered.json`` - Grok, which sends no ``usage_update`` at all. Its turn is not metered:
  a null cost and a null ``cost_kind``, never ``0``.

The fixtures are named by what they show rather than by agent, so this file never names a specific
agent CLI (``test_acp.py``'s scan).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from harnessed.acp import delegate as acp_delegate
from harnessed.acp import ledger
from harnessed.acp import turns as agent_turns
from harnessed.cli import routed
from harnessed.events.bus import EventBus
from harnessed.loop.journal import Journal, last_iteration
from harnessed.memory import db
from harnessed.providers.translate import tool_call_payload
from harnessed.providers.types import ChatRequest, ChatResponse, Message
from support import stub_acp_agent as stub
from test_acp import probe_spec, talking_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "acp"
REPORTS_COST = FIXTURES / "reports-cost.json"
NOT_METERED = FIXTURES / "not-metered.json"
STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_acp_agent.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "171"

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


def recorded(path: Path) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return body


def rows(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT * FROM agent_turns WHERE run_id = ? ORDER BY idx", (run_id,)
        ).fetchall()
    finally:
        conn.row_factory = None
    return [dict(row) for row in found]


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "harness.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / ".harnessed"
    folder.mkdir()
    return folder


def replay(
    conn: sqlite3.Connection, path: Path, *, billing: str | None = "oauth"
) -> list[dict[str, Any]]:
    """Journal a recorded transcript exactly as a finished delegation would."""
    body = recorded(path)
    parsed = agent_turns.from_transcript(
        body["transcript"],
        billing=billing,
        request="confirm you received this prompt",
        complete=body["status"] == "succeeded",
    )
    run_id = f"run_{path.stem.replace('-', '_')}"
    agent_turns.record(
        conn,
        run_id,
        parsed,
        entry=str(body["agent"]),
        billing=billing,
        source="acp",
        message_seq=1,
        hop=0,
        task_hash="dlg_recorded",
    )
    return rows(conn, run_id)


def usage_updates(path: Path) -> list[dict[str, Any]]:
    return [
        frame
        for frame in recorded(path)["transcript"]
        if frame.get("sessionUpdate") == "usage_update"
    ]


# ---- AC3: the recorded transcript that reports a cost ------------------------------------------


def test_the_reporting_transcript_is_one_row_holding_its_chunks_and_its_reported_cost(
    store: sqlite3.Connection,
) -> None:
    assert len(usage_updates(REPORTS_COST)) == 4, "the recording the ticket was planned on"
    (row,) = replay(store, REPORTS_COST)
    assert row["source"] == "acp"
    assert row["billing"] == "oauth"
    assert row["response"] == "I received the prompt and I'm ready to help; no files changed."
    assert row["tool_calls"] is None
    assert row["cost"] == "0.063306"
    assert row["cost_nanos"] == 63_306_000
    assert row["currency"] == "USD"
    assert row["cost_kind"] == "api_equivalent"
    assert row["complete"] == 1
    # The recording's `usage_update._meta` carries a rate limit and no model, so none is claimed.
    assert row["model"] is None
    assert (row["message_seq"], row["hop"], row["task_hash"]) == (1, 0, "dlg_recorded")
    capture("replayed-reports-cost.json", json.dumps(row, indent=2, sort_keys=True) + "\n")


def test_the_model_is_read_from_usage_update_meta_where_one_is_sent() -> None:
    """FR-112: "the agent's model from ``usage_update._meta`` where sent"."""
    frames = [
        {"kind": "session_update", "sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "done"}},
        {"kind": "session_update", "sessionUpdate": "usage_update", "used": 1, "size": 2,
         "_meta": {"model": "some-model-1"}, "cost": {"amount": 0.5, "currency": "USD"}},
    ]  # fmt: skip
    (turn,) = agent_turns.from_transcript(frames, billing="oauth", request="x", complete=True)
    assert turn.model == "some-model-1"
    assert turn.cost == Decimal("0.5")


def test_a_tool_call_lands_on_the_turn_with_its_last_status() -> None:
    frames = [
        # A tool call's own ACP kind overwrites the frame's (acp/client.py spreads it last).
        {"sessionUpdate": "tool_call", "toolCallId": "t1", "kind": "read",
         "title": "read a file", "status": "pending"},
        {"kind": "session_update", "sessionUpdate": "tool_call_update", "toolCallId": "t1",
         "status": "completed"},
        {"kind": "session_update", "sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "read it"}},
    ]  # fmt: skip
    (turn,) = agent_turns.from_transcript(frames, billing="oauth", request="x", complete=True)
    assert turn.tool_calls == (
        {"tool_call_id": "t1", "kind": "read", "title": "read a file", "status": "completed"},
    )
    assert turn.response == "read it"


# ---- AC4: the recorded transcript with no usage_update -----------------------------------------


def test_the_unmetered_transcript_carries_a_null_cost_never_a_zero(
    store: sqlite3.Connection,
) -> None:
    assert usage_updates(NOT_METERED) == [], "the recording the ticket was planned on"
    (row,) = replay(store, NOT_METERED)
    assert row["entry"] == "grok"
    assert row["response"] == "I received this prompt and will change no files."
    assert (row["cost"], row["cost_nanos"], row["currency"], row["cost_kind"]) == (
        None,
        None,
        None,
        None,
    )
    assert row["model"] is None
    capture("replayed-not-metered.json", json.dumps(row, indent=2, sort_keys=True) + "\n")


def test_no_subscription_turn_carries_a_computed_figure(store: sqlite3.Connection) -> None:
    """Every stored figure is one an agent reported in a ``usage_update``; 0 are computed."""
    replayed = replay(store, REPORTS_COST) + replay(store, NOT_METERED)
    reported = {
        Decimal(str(frame["cost"]["amount"]))
        for path in (REPORTS_COST, NOT_METERED)
        for frame in usage_updates(path)
        if "cost" in frame
    }
    for row in replayed:
        if row["cost"] is None:
            assert row["cost_nanos"] is None and row["cost_kind"] is None
            continue
        assert row["cost_kind"] == "api_equivalent"
        assert Decimal(row["cost"]) in reported
    assert [row["cost"] is None for row in replayed] == [False, True]


def test_an_entry_with_no_billing_is_not_metered_even_when_a_cost_is_sent(
    store: sqlite3.Connection,
) -> None:
    """``billing`` absent means not metered (``agents.toml``): the reported figure is not kept."""
    (row,) = replay(store, REPORTS_COST, billing=None)
    assert (row["cost"], row["cost_nanos"], row["currency"], row["cost_kind"]) == (
        None,
        None,
        None,
        None,
    )


# ---- AC2: an oauth entry is never proxied ------------------------------------------------------


def _is_routing_variable(name: str) -> bool:
    """A variable that would move an agent's traffic: a base URL override or a proxy."""
    upper = name.upper()
    return upper.endswith("BASE_URL") or "PROXY" in upper


def routing_additions(parent: dict[str, str], child: dict[str, str]) -> dict[str, str]:
    """Every base-URL or proxy variable the child carries that the parent did not hand it."""
    return {
        name: value
        for name, value in child.items()
        if _is_routing_variable(name) and parent.get(name) != value
    }


def test_the_census_catches_a_synthetic_base_url_and_proxy() -> None:
    """Without this the census below is green against a child that was never looked at."""
    parent = {"PATH": "p", "HTTPS_PROXY": "http://corp"}
    child = {**parent, "SOME_BASE_URL": "http://127.0.0.1:1", "ALL_PROXY": "socks5://x"}
    assert routing_additions(parent, parent) == {}
    assert routing_additions(parent, child) == {
        "SOME_BASE_URL": "http://127.0.0.1:1",
        "ALL_PROXY": "socks5://x",
    }


#: Terminal-size variables a C library (readline, curses) exports into the process environment
#: behind ``os.environ``'s back, so a snapshot of ``os.environ`` never holds them but every child
#: inherits them. They carry no routing, so they are not what this census is about.
TERMINAL_SIZE_VARS = frozenset({"COLUMNS", "LINES"})


def test_an_oauth_entry_is_launched_with_no_base_url_override_and_no_proxy(
    tmp_path: Path, home: Path
) -> None:
    dumped = tmp_path / "child-env.json"
    script = (
        "import json, os, runpy, sys\n"
        f"open({str(dumped)!r}, 'w', encoding='utf-8').write(json.dumps(dict(os.environ)))\n"
        f"sys.argv = [{str(STUB_PATH)!r}]\n"
        f"runpy.run_path({str(STUB_PATH)!r}, run_name='__main__')\n"
    )
    oauth = dataclasses.replace(talking_spec(tmp_path, script), billing="oauth")
    parent = dict(os.environ)
    result = acp_delegate.delegate(oauth, task="do it", acceptance="done", home=home)
    assert result.status == "succeeded"
    child: dict[str, str] = json.loads(dumped.read_text(encoding="utf-8"))
    assert routing_additions(parent, child) == {}
    added = set(child) - set(parent) - TERMINAL_SIZE_VARS
    assert added == set(), "the child's environment is the parent's"
    capture(
        "child-environment-census.txt",
        "ticket 171 - an oauth entry's child environment against its parent's\n"
        f"base-URL or proxy variables added: {len(routing_additions(parent, child))}\n"
        f"variables only the child carries: {sorted(set(child) - set(parent))}\n",
    )


# ---- AC5 and the wiring: a delegation journals agent_turns and never turns ----------------------


def record_loop_turn(conn: sqlite3.Connection, run_key: str) -> Journal:
    journal = Journal(conn, run_key, serialize_call=tool_call_payload, provider="fake")
    request = ChatRequest(model="m", system="", messages=(Message(role="user", content="hi"),))
    response = ChatResponse(response_id="r1", model="m", content="hello", stop_reason="end_turn")
    journal.record(1, request, response, ())
    return journal


def test_a_delegation_journals_one_agent_turn_and_no_loop_turn(
    tmp_path: Path, home: Path, store: sqlite3.Connection
) -> None:
    bus = EventBus(conn=store)
    journal = record_loop_turn(store, bus.run_id)
    replayed_before = journal.recorded(1)
    turns_before = count(store, "turns")
    spec = probe_spec(tmp_path, billing='"oauth"')

    routed.run_agent(spec, "do it", "done", home=home, bus=bus, store=store, message_seq=7, hop=0)

    assert count(store, "turns") == turns_before, "turns gains 0 rows (AC)"
    assert journal.recorded(1) == replayed_before, "journal.recorded() replays as before"
    assert last_iteration(store, bus.run_id) == 1
    (row,) = rows(store, bus.run_id)
    cwd = spec.cwd or str(Path.cwd())
    assert row["task_hash"] == ledger.task_hash(
        agent="probe", cwd=cwd, task="do it", acceptance="done"
    )
    assert (row["idx"], row["message_seq"], row["hop"]) == (0, 7, 0)
    assert (row["entry"], row["billing"], row["source"]) == ("probe", "oauth", "acp")
    assert row["request"] == "do it"
    assert row["response"] == stub.STUB_MESSAGE
    assert stub.TOOL_TITLE in row["tool_calls"]
    assert row["complete"] == 1
    assert row["cost"] is None and row["cost_kind"] is None, "the stub sends no usage_update"


def test_a_delegation_the_ledger_answers_journals_nothing_twice(
    tmp_path: Path, home: Path, store: sqlite3.Connection
) -> None:
    bus = EventBus(conn=store)
    spec = probe_spec(tmp_path, billing='"oauth"')
    acp_delegate.delegate(spec, "do it", "done", home=home, bus=bus, store=store)
    acp_delegate.delegate(spec, "do it", "done", home=home, bus=bus, store=store)
    assert len(rows(store, bus.run_id)) == 1


def test_an_openrouter_entrys_turns_are_left_to_its_metering_proxy(
    tmp_path: Path, home: Path, store: sqlite3.Connection
) -> None:
    """Decision 63: an ``openrouter`` entry is journaled by the proxy (FR-113), so its transcript
    is not journaled a second time here."""
    bus = EventBus(conn=store)
    spec = probe_spec(tmp_path, billing='"openrouter"')
    acp_delegate.delegate(spec, "do it", "done", home=home, bus=bus, store=store)
    assert rows(store, bus.run_id) == []
