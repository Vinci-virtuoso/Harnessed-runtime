"""Ticket 234 (FR-130, decisions 74, 75, 78): a permission request reaches the operator, and an
unanswered one is denied at its ceiling.

The stub agent asks (``HARNESSED_STUB_ASK_PERMISSION=1``) and names its tool kind
(``HARNESSED_STUB_PERMISSION_TOOL``); the delegation runs on a thread of its own, as it does in the
control app, and the test is the operator: it answers through ``acp/pending.py`` on a connection of
its own. Time is the frozen clock, moved by hand, so no test sleeps for a ceiling.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from harnessed import clock, config, proc
from harnessed.acp import delegate as acp_delegate
from harnessed.acp import pending
from harnessed.acp import permissions as perms
from harnessed.acp.pending import Door
from harnessed.cli import run_cmd
from harnessed.errors import (
    ParameterError,
    PermissionAnswered,
    RunInFlight,
    SurfaceNotFound,
)
from harnessed.events.bus import EventBus
from harnessed.memory import db
from harnessed.tasks import lease
from test_acp import probe_spec

TOOL = "execute"
SESSION = "run_00000000000000234PERMIT00"
WAIT_S = 15.0
CEILING_S = 6  # the delegation's own ceiling in the pause test; see that test


# ---- the harness ----


class Delegation:
    """One delegation on its own thread, with the connection and bus that thread owns."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        door_for: Callable[[sqlite3.Connection], Door] | None = None,
        **fields: str,
    ) -> None:
        monkeypatch.setenv("HARNESSED_STUB_ASK_PERMISSION", "1")
        monkeypatch.setenv("HARNESSED_STUB_PERMISSION_TOOL", TOOL)
        self.home = tmp_path / ".harnessed"
        self.home.mkdir(exist_ok=True)
        self.db_path = tmp_path / "permissions.db"
        db.connect(self.db_path).close()  # migrated here, so no two threads race to do it
        self.spec = probe_spec(tmp_path, **fields)
        self.door_for = door_for
        self.result: Any = None
        self.message_seq: int | None = None
        self.hop = 0
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        conn = db.connect(self.db_path)
        try:
            bus = EventBus(run_id=SESSION, conn=conn)
            door = None if self.door_for is None else self.door_for(conn)
            self.result = acp_delegate.delegate(
                self.spec,
                task="do it",
                acceptance="done",
                home=self.home,
                bus=bus,
                store=conn,
                door=door,
                message_seq=self.message_seq,
                hop=self.hop,
            )
        except BaseException as exc:  # reported by join(), never lost on the thread
            self.error = exc
        finally:
            conn.close()

    def start(self) -> Delegation:
        self.thread.start()
        return self

    def join(self) -> Any:
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive(), "the delegation hung"
        if self.error is not None:
            raise self.error
        return self.result

    def operator(self) -> sqlite3.Connection:
        """The operator's own connection, never the delegation's."""
        return db.connect(self.db_path)

    def pending_row(self) -> pending.Row:
        """Wait for the one open request to be recorded."""
        deadline = time.monotonic() + WAIT_S
        with closing(self.operator()) as conn:
            while time.monotonic() < deadline:
                rows = pending.listing(conn, state="pending")
                if rows:
                    return rows[0]
                assert self.thread.is_alive() or self.error is None, self.error
                time.sleep(0.02)
        raise AssertionError("no request ever became pending")

    def events(self) -> list[tuple[str, dict[str, Any], str]]:
        with closing(self.operator()) as conn:
            return [
                (str(name), json.loads(payload), str(ts))
                for name, payload, ts in conn.execute(
                    "SELECT name, payload, created_at FROM events WHERE run_id = ? ORDER BY seq",
                    (SESSION,),
                )
            ]

    def rows(self) -> tuple[pending.Row, ...]:
        with closing(self.operator()) as conn:
            return pending.listing(conn, session=SESSION)

    def chosen(self) -> str:
        """The option the agent was answered with, as the transcript recorded it."""
        (written,) = sorted((self.home / "acp").glob("*.json"))
        frames = json.loads(written.read_text(encoding="utf-8"))["transcript"]
        (decision,) = (f for f in frames if f["kind"] == "permission_decision")
        return str(decision["option_id"])


def ask_door(**hooks: Any) -> Callable[[sqlite3.Connection], Door]:
    return lambda _conn: Door(answer_door=True, **hooks)


@pytest.fixture
def delegation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen_clock: Any) -> Any:
    def build(
        door_for: Callable[[sqlite3.Connection], Door] | None = None, **fields: str
    ) -> Delegation:
        return Delegation(tmp_path, monkeypatch, door_for, **fields)

    return build


# ---- AC 1: allow, deny and allowlist answer at once ----


@pytest.mark.parametrize(
    ("fields", "answer", "rule", "allowed"),
    [
        ({"mode": '"allow"'}, "approve", "allowlist", True),
        ({"mode": '"deny"'}, "deny", "deny_list", False),
        ({"mode": '"allowlist"', "allow": f'["{TOOL}"]'}, "approve", "allowlist", True),
        ({"mode": '"allowlist"', "allow": '["other"]'}, "deny", "deny_list", False),
        ({"mode": '"allow"', "deny": f'["{TOOL}"]'}, "deny", "deny_list", False),
    ],
)
def test_a_rule_decided_request_is_answered_at_once_recorded_and_published(
    delegation: Any, fields: dict[str, str], answer: str, rule: str, allowed: bool
) -> None:
    run = delegation(**fields).start()
    assert run.join().status == "succeeded"
    (row,) = run.rows()
    assert (row.tool_kind, row.answer, row.rule, row.answered_via) == (TOOL, answer, rule, None)
    assert row.ceiling_at is None, "nobody was asked, so there is no ceiling"
    names = [name for name, _payload, _ts in run.events()]
    assert names.index("acp.permission.request") < names.index("acp.permission.decision")
    decision = next(p for n, p, _ in run.events() if n == "acp.permission.decision")
    assert decision["allowed"] is allowed and decision["permission_id"] == row.permission_id
    assert run.chosen() == ("allow_once" if allowed else "reject_once")


# ---- AC 2, 4, 10: ask with an answer door waits for a person ----


def test_an_ask_with_an_answer_door_waits_and_the_answer_reaches_the_agent(
    delegation: Any,
) -> None:
    run = delegation(ask_door(), mode='"ask"').start()
    opened = run.pending_row()
    assert opened.ceiling_at == clock.isoformat(
        clock.now() + timedelta(seconds=config.DEFAULT_PERMISSION_CEILING_S)
    )
    request = next(p for n, p, _ in run.events() if n == "acp.permission.request")
    # Live: the request is already on the ledger while the delegation is still waiting.
    assert run.thread.is_alive()
    assert request["permission_id"] == opened.permission_id
    assert request["tool_kind"] == TOOL and request["ceiling_at"] == opened.ceiling_at
    assert "description" in request

    clock.freeze(clock.now() + timedelta(seconds=2))
    with closing(run.operator()) as operator:
        pending.answer(operator, opened.permission_id, "approve", rule="operator", via="console")
    assert run.join().status == "succeeded"

    events = run.events()
    asked = next(ts for n, _p, ts in events if n == "acp.permission.request")
    decided = next(ts for n, _p, ts in events if n == "acp.permission.decision")
    assert clock.parse(decided) - clock.parse(asked) == timedelta(seconds=2), "not by zero"
    decision = next(p for n, p, _ in events if n == "acp.permission.decision")
    assert (decision["answer"], decision["answered_via"]) == ("approve", "console")
    (row,) = run.rows()
    assert row.state == "approved" and run.chosen() == "allow_once"


def test_a_denied_request_is_an_answer_and_the_agent_is_told_no(delegation: Any) -> None:
    run = delegation(ask_door(), mode='"ask"').start()
    opened = run.pending_row()
    with closing(run.operator()) as operator:
        pending.answer(operator, opened.permission_id, "deny", rule="operator", via="cli")
    assert run.join().status == "succeeded"
    (row,) = run.rows()
    assert (row.state, row.answered_via) == ("denied", "cli")
    assert run.chosen() == "reject_once"


# ---- AC 3: ask with no answer door ----


def test_an_ask_with_no_answer_door_asks_an_interactive_terminal(
    delegation: Any,
) -> None:
    asked: list[str] = []

    def terminal(question: str, kinds: tuple[str, ...], timeout_s: float) -> str | None:
        asked.append(question)
        return "allow_once"

    interactive = delegation(
        lambda _conn: Door(interactive=True, prompter=terminal), mode='"ask"'
    ).start()
    interactive.join()
    (row,) = interactive.rows()
    assert (row.rule, row.answered_via, row.answer) == ("operator", "cli", "approve")
    assert len(asked) == 1 and TOOL in asked[0]


def test_an_unattended_ask_degrades_to_the_allowlist_with_its_warning(delegation: Any) -> None:
    unattended = delegation(None, mode='"ask"', allow='["other"]')
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        unattended.start().join()
    assert any("allowlist" in str(w.message) for w in caught)
    (degraded,) = unattended.rows()
    assert (degraded.rule, degraded.answer, degraded.answered_via) == ("deny_list", "deny", None)
    decision = next(p for n, p, _ in unattended.events() if n == "acp.permission.decision")
    assert decision["rule"] == "ask_degraded_allowlist" and decision["warning"]


def test_an_ask_with_an_answer_door_is_pending_not_decided() -> None:
    door = perms.decide(perms.Policy(mode="ask", answer_door=True), TOOL)
    assert door == perms.Pending(tool=TOOL), "the third path: an answer door returns Pending"


# ---- AC 6: no answer by the ceiling ----


def test_no_answer_by_the_ceiling_denies_and_nothing_hangs(delegation: Any) -> None:
    run = delegation(ask_door(ceiling_s=600.0), mode='"ask"').start()
    opened = run.pending_row()
    assert opened.ceiling_at is not None
    clock.freeze(clock.parse(opened.ceiling_at) + timedelta(seconds=1))
    assert run.join().status == "succeeded"
    (row,) = run.rows()
    assert (row.state, row.answer, row.rule, row.answered_via) == (
        "expired",
        "deny",
        "ceiling",
        None,
    )
    decision = next(p for n, p, _ in run.events() if n == "acp.permission.decision")
    assert (decision["answer"], decision["allowed"]) == ("deny", False)
    assert run.chosen() == "reject_once"


def test_a_ceiling_that_loses_the_race_reads_the_operators_answer(delegation: Any) -> None:
    """The compare-and-set picks one winner; the loser reads it rather than overwriting it."""
    run = delegation(ask_door(), mode='"ask"').start()
    opened = run.pending_row()
    with closing(run.operator()) as operator:
        pending.answer(operator, opened.permission_id, "approve", rule="operator", via="console")
    assert opened.ceiling_at is not None
    clock.freeze(clock.parse(opened.ceiling_at) + timedelta(seconds=1))
    run.join()
    (row,) = run.rows()
    assert (row.rule, row.answer) == ("operator", "approve")


# ---- AC 7: the lease, and the delegation's own timeout, pause while a request is open ----


def test_a_pending_request_gives_up_the_lease_and_the_answer_waits_for_it_back(
    delegation: Any, tmp_path: Path
) -> None:
    parked: list[tuple[str, bool]] = []
    settings = config.load(provider="fake")

    def door_for(conn: sqlite3.Connection) -> Door:
        lease.acquire(conn, SESSION)
        return run_cmd._door(
            settings, conn, SESSION, True, None, lambda s, p: parked.append((s, p))
        )

    run = delegation(door_for, mode='"ask"').start()
    opened = run.pending_row()
    with closing(run.operator()) as operator:
        assert lease.holder(operator) is None, "the lease was released while waiting"
        lease.acquire(operator, "run_other")  # a second run (`harnessed run`, the console's Send)
        pending.answer(operator, opened.permission_id, "approve", rule="operator", via="cli")
        time.sleep(1.0)
        assert run.thread.is_alive(), "another run holds the lease, so the answer waits"
        lease.release(operator, "run_other")
    assert run.join().status == "succeeded"
    assert parked == [(SESSION, True), (SESSION, False)]


def test_the_delegations_own_timeout_pauses_while_a_request_is_open(delegation: Any) -> None:
    # The ceiling runs from the spawn until the request opens, so it has to outlast the stub's own
    # start-up: on the operator's PC, under coverage, the stub had not asked within 2.0 s and the
    # delegation timed out before there was a request to pause for (ticket 263). The wait below
    # is measured from the spawn, so the claim - a request open past the ceiling - holds wherever
    # the stub is slow to start.
    started = time.monotonic()
    run = delegation(ask_door(), mode='"ask"', timeout_s=str(CEILING_S)).start()
    opened = run.pending_row()
    time.sleep(max(0.0, CEILING_S + 1.0 - (time.monotonic() - started)))  # past the ceiling
    assert run.thread.is_alive(), "a pending request is not an agent that produced nothing"
    with closing(run.operator()) as operator:
        pending.answer(operator, opened.permission_id, "approve", rule="operator", via="console")
    assert run.join().status == "succeeded"


def test_a_shutdown_wakes_a_parked_run_with_a_deny(delegation: Any) -> None:
    stopping = threading.Event()
    run = delegation(ask_door(stopping=stopping.is_set), mode='"ask"').start()
    run.pending_row()
    stopping.set()
    assert run.join().status == "succeeded"
    (row,) = run.rows()
    assert (row.answer, row.rule, row.answered_via) == ("deny", "operator", None)
    assert run.chosen() == "reject_once"


# ---- the store: answer once, read every state ----


@pytest.fixture
def store(tmp_path: Path, frozen_clock: Any) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "store.db")
    EventBus(run_id=SESSION, conn=conn).publish("run.started", {"provider": "fake"})
    try:
        yield conn
    finally:
        conn.close()


def seed(conn: sqlite3.Connection, **fields: Any) -> pending.Row:
    base: dict[str, Any] = {"session_id": SESSION, "agent": "probe", "tool_kind": TOOL}
    return pending.record(conn, **{**base, "description": "run ls", **fields})


def test_a_second_answer_names_who_answered_first(store: sqlite3.Connection) -> None:
    row = seed(store, ceiling_at="2026-01-01T12:10:00Z")
    pending.answer(store, row.permission_id, "approve", rule="operator", via="cli")
    with pytest.raises(PermissionAnswered) as raised:
        pending.answer(store, row.permission_id, "deny", rule="operator", via="console")
    assert raised.value.answered_via == "cli" and raised.value.answer == "approve"
    assert str(raised.value) == "already answered: approved from the cli at 12:00"
    with pytest.raises(SurfaceNotFound):
        pending.answer(store, "perm_unknown", "deny", rule="operator", via="cli")


def test_the_listing_serves_one_decision_per_state_in_request_order(
    store: sqlite3.Connection,
) -> None:
    ceiling = "2026-01-01T12:10:00Z"
    wait = seed(store, ceiling_at=ceiling)
    yes = seed(store, ceiling_at=ceiling)
    no = seed(store, ceiling_at=ceiling)
    late = seed(store, ceiling_at=ceiling)
    allowed = seed(store, answer="approve", rule="allowlist")
    refused = seed(store, answer="deny", rule="deny_list")
    pending.answer(store, yes.permission_id, "approve", rule="operator", via="console")
    pending.answer(store, no.permission_id, "deny", rule="operator", via="cli")
    pending.answer(store, late.permission_id, "deny", rule="ceiling", via=None)

    rows = pending.listing(store, session=SESSION)
    assert [row.state for row in rows] == [
        "pending", "approved", "denied", "expired", "allowed", "refused"
    ]  # fmt: skip
    assert [row.permission_id for row in rows] == [
        r.permission_id for r in (wait, yes, no, late, allowed, refused)
    ]
    assert [row.permission_id for row in pending.listing(store, state="pending")] == [
        wait.permission_id
    ]
    served = pending.listing_payload(rows)["permissions"]
    assert isinstance(served, list)
    for item in served:
        for column, value in item.items():
            assert value is None or isinstance(value, str), column
            if value is None and not column.endswith("_reason"):
                assert isinstance(item[f"{column}_reason"], str), f"{column} says why it is null"
    first = served[0]
    assert first["answer"] is None and first["answer_reason"] == "nobody has answered yet"
    assert pending.listing_payload(()) == {"permissions": []}


def test_an_unknown_session_is_not_found_and_an_empty_one_is_not(store: sqlite3.Connection) -> None:
    assert pending.listing(store, session=SESSION) == (), "held, and asked for nothing"
    with pytest.raises(SurfaceNotFound):
        pending.listing(store, session="run_nobody_ever_opened")
    with pytest.raises(ParameterError):
        pending.listing(store)
    with pytest.raises(ParameterError):
        pending.listing(store, state="maybe")


def test_the_migration_is_019_and_a_row_cannot_be_half_answered(store: sqlite3.Connection) -> None:
    shipped = db.load_migrations()
    assert (shipped[18].version, shipped[18].filename) == (19, "019_permissions.sql")
    row = seed(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "UPDATE permission_decisions SET answer = 'approve' WHERE permission_id = ?",
            (row.permission_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO run_lease (slot, session_id, pid, acquired_at) VALUES (2,'a',1,'t')"
        )


# ---- the lease and the process identity ----


def test_the_lease_refuses_a_live_holder_and_replaces_a_dead_one(store: sqlite3.Connection) -> None:
    lease.acquire(store, "run_a")
    with pytest.raises(RunInFlight) as refused:
        lease.acquire(store, "run_b")
    assert refused.value.session_id == "run_a"
    lease.acquire(store, "run_a")  # the same session re-takes its own
    lease.release(store, "run_b")  # not the holder: a no-op
    assert lease.holder(store) is not None
    lease.release(store, "run_a")
    lease.acquire(store, "run_b")

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    store.execute("UPDATE run_lease SET pid = ?, session_id = 'run_dead'", (dead.pid,))
    assert lease.holder(store) is None, "a holder that died is not a live holder"
    lease.acquire(store, "run_c")


def test_the_process_identity_pairs_a_pid_with_its_start_time_or_says_why_not() -> None:
    import os

    me = proc.identity(os.getpid())
    assert me is not None and me.pid == os.getpid()
    if sys.platform.startswith("linux") or sys.platform == "win32":
        assert me.started_at is not None and me.reason is None
        assert proc.identity(os.getpid()) == me, "stable across calls"
        assert proc.holds(me.pid, me.started_at)
        assert not proc.holds(me.pid, "1"), "a reused pid is not the recorded process"
    else:
        assert me.started_at is None and me.reason
    assert proc.identity(2**22 + 12345) is None
    assert acp_delegate.alive(os.getpid()) and not acp_delegate.alive(None)


# ---- ticket 240 (FR-141, decision 75): the task half of the pending-decision hooks ----

from tests.support.task_world import capture  # noqa: E402

from harnessed.cli import routed  # noqa: E402
from harnessed.tasks import store as task_store  # noqa: E402


def task_world(delegation: Any) -> tuple[Any, str, list[tuple[str, dict[str, Any]]]]:
    """A delegation whose door is the task's: the task is ``running`` its first message."""
    published: list[tuple[str, dict[str, Any]]] = []
    settings = config.load(provider="fake")
    holder: dict[str, str] = {}

    def door_for(conn: sqlite3.Connection) -> Door:
        lease.acquire(conn, SESSION)
        base = run_cmd._door(settings, conn, SESSION, True, None, None)
        return routed.task_door(
            base, conn, holder["task_id"], publish=lambda n, p: published.append((n, dict(p)))
        )

    run = delegation(door_for, mode='"ask"')
    with closing(run.operator()) as operator:
        task = task_store.queue(operator, "tidy the notes", via="cli")
        holder["task_id"] = str(task["task_id"])
        task_store.begin_message(
            operator, holder["task_id"], run_id=SESSION, message_seq=1, author="task", first=True
        )
    return run, holder["task_id"], published


def task_row(run: Any, task_id: str) -> dict[str, str | None]:
    with closing(run.operator()) as operator:
        row = task_store.get(operator, task_id)
    assert row is not None
    return row


def test_a_pending_permission_pauses_the_task_and_the_answer_resumes_it(delegation: Any) -> None:
    run, task_id, published = task_world(delegation)
    run.start()
    opened = run.pending_row()

    waiting = task_row(run, task_id)
    assert (waiting["state"], waiting["pause_reason"]) == ("needs_human", "permission")
    assert waiting["pending_permission_id"] == opened.permission_id
    assert (waiting["pauses"], waiting["paused_at"] is not None) == ("1", True)
    assert published == [
        (
            "task.paused",
            {
                "task_id": task_id,
                "pause_reason": "permission",
                "permission_id": opened.permission_id,
                "route": None,
                "confidence": None,
                "confidence_percent": None,
                "floor": None,
                "floor_percent": None,
            },
        )
    ]
    with closing(run.operator()) as operator:
        assert lease.holder(operator) is None, "the lease is released while it waits"
        pending.answer(operator, opened.permission_id, "approve", rule="operator", via="cli")
    run.join()

    resumed = task_row(run, task_id)
    assert (resumed["state"], resumed["pause_reason"], resumed["pending_permission_id"]) == (
        "running",
        None,
        None,
    )
    assert resumed["pauses"] == "1"
    with closing(run.operator()) as operator:
        assert lease.holder(operator) is not None, "the answer took the lease back"


def test_an_operators_deny_lets_the_task_continue_to_its_next_checkpoint(
    delegation: Any,
) -> None:
    run, task_id, _ = task_world(delegation)
    run.start()
    opened = run.pending_row()
    with closing(run.operator()) as operator:
        pending.answer(operator, opened.permission_id, "deny", rule="operator", via="console")
    run.join()

    with closing(run.operator()) as operator:
        row = task_store.checkpoint(
            operator,
            task_id,
            run_id=SESSION,
            message_seq=1,
            started_at=clock.isoformat(clock.now()),
            nanos=0,
            unpriced=0,
            ended="ok",
        )
    assert (row["state"], row["stop_reason"]) == ("running", None), "not unattended: it goes on"
    capture("deny-continues.json", row)


def test_a_ceiling_denial_fails_the_task_closed_and_it_is_never_resumed_by_itself(
    delegation: Any,
) -> None:
    run, task_id, _ = task_world(delegation)
    started = clock.isoformat(clock.now())
    run.start()
    opened = run.pending_row()
    assert opened.ceiling_at is not None
    clock.freeze(clock.parse(opened.ceiling_at) + timedelta(seconds=1))  # the fake clock
    run.join()

    after = task_row(run, task_id)
    assert after["state"] == "running", "the denial is answered; the checkpoint decides"
    with closing(run.operator()) as operator:
        row = task_store.checkpoint(
            operator,
            task_id,
            run_id=SESSION,
            message_seq=1,
            started_at=started,
            nanos=0,
            unpriced=0,
            ended="ok",
        )
    assert (row["state"], row["stop_reason"]) == ("failed", "ceiling")
    assert row["finished_at"] is not None
    assert float(str(row["used_seconds"])) == 0.0, "the 601 s spent waiting on a person is not work"
    capture("ceiling-fails-closed.json", row)


def test_a_cancel_while_a_permission_is_pending_stays_cancelled(delegation: Any) -> None:
    run, task_id, _ = task_world(delegation)
    run.start()
    run.pending_row()
    with closing(run.operator()) as operator:

        def deny(permission_id: str) -> None:
            pending.answer(operator, permission_id, "deny", rule="operator", via="cli")

        task_store.cancel(operator, task_id, deny=deny)
    run.join()
    assert task_row(run, task_id)["state"] == "cancelled"


def test_a_request_names_the_exchange_it_belongs_to(store: sqlite3.Connection) -> None:
    asked = seed(store, message_seq=3, hop=1)
    bare = seed(store)
    assert (asked.message_seq, asked.hop) == ("3", "1")
    assert (bare.message_seq, bare.hop) == (None, None)
    assert pending.get(store, asked.permission_id) == asked
    items = pending.listing_payload(pending.listing(store, session=SESSION))["permissions"]
    assert isinstance(items, list)
    keys = list(items[0])
    after = keys[keys.index("session_id") + 1 :]
    assert after[:6] == [
        "message_seq", "message_seq_reason", "hop", "hop_reason", "task_id", "task_id_reason"
    ]  # fmt: skip
    first, second = items
    assert (first["message_seq"], first["hop"], first["message_seq_reason"]) == ("3", "1", None)
    assert second["message_seq"] is None and second["hop"] is None
    why = "asked outside a routed message, or recorded before the exchange was kept"
    assert second["message_seq_reason"] == second["hop_reason"] == why
    assert first["task_id"] is None and first["task_id_reason"] == "the session is not a task's"


# ---- ticket 288 (FR-167): a request stores what the agent sent ----


def test_a_request_stores_what_the_agent_sent_and_who_waits(store: sqlite3.Connection) -> None:
    row = seed(
        store,
        task_hash="h" * 8,
        raw_input={"command": "ls", "key": "sk-or-v1-" + "a" * 40},
        warning="ask degraded to allowlist",
    )
    stored = pending.get(store, row.permission_id)
    assert stored == row and stored is not None
    assert stored.task_hash == "h" * 8 and stored.warning == "ask degraded to allowlist"
    assert stored.detail is not None and stored.detail.startswith('{"command":"ls"')
    assert "sk-or-v1-" + "a" * 40 not in stored.detail
    me = proc.identity(__import__("os").getpid())
    assert me is not None
    assert stored.waiter_pid == str(me.pid) and stored.waiter_started_at == me.started_at
    long = seed(store, raw_input={"text": "x" * 5000})
    assert long.detail is not None and len(long.detail) == 2000


def test_the_listing_serves_the_five_fields_with_a_reason_beside_each_null(
    store: sqlite3.Connection,
) -> None:
    full = seed(store, task_hash="abc", raw_input={"a": 1}, warning="careful")
    bare = seed(store)
    store.execute(
        "UPDATE permission_decisions SET waiter_pid = NULL, waiter_started_at = NULL "
        "WHERE permission_id = ?",
        (bare.permission_id,),
    )
    first, second = pending.listing_payload(pending.listing(store, session=SESSION))["permissions"]  # type: ignore[misc]
    assert (first["task_hash"], first["detail"], first["warning"]) == ("abc", '{"a":1}', "careful")
    for name in ("task_hash", "detail", "warning", "waiter_pid", "waiter_started_at"):
        assert first[f"{name}_reason"] is None
        assert second[name] is None
    old = "recorded before Harnessed kept this"
    assert second["task_hash_reason"] == old and second["waiter_pid_reason"] == old
    assert second["detail_reason"] == "the agent sent no details"
    assert second["warning_reason"] == "no warning applied"
    assert full.permission_id == first["permission_id"]


def test_the_request_event_carries_the_exchange_the_row_stores(delegation: Any) -> None:
    run = delegation(mode='"allow"')
    run.message_seq, run.hop = 7, 2
    assert run.start().join().status == "succeeded"
    (row,) = run.rows()
    request = next(p for n, p, _ in run.events() if n == "acp.permission.request")
    assert (request["message_seq"], request["hop"]) == (7, 2)
    assert (str(request["message_seq"]), str(request["hop"])) == (row.message_seq, row.hop)
    assert row.detail is not None and "command" in row.detail, "the stub sent raw input"
    assert row.task_hash is not None and row.waiter_pid is not None
