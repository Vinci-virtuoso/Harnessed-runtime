"""FR-147 (ticket 246): one denied-by-default ``execute`` request, approved in the console.

The live case is operator-run (``-m live``, outside ``verify:``). The offline cases parse the
operator's recording, copied verbatim into ``tests/fixtures/acp/``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "acp"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_the_recorded_request_is_an_execute_that_the_agent_then_ran() -> None:
    frames = load("approved-execute-transcript.json")["transcript"]
    kinds = [f.get("kind") for f in frames]
    request = kinds.index("permission_request")
    decision = kinds.index("permission_decision")
    assert frames[request]["tool"] == "execute"
    assert request < decision
    assert frames[decision]["allowed"] is True
    ran = [
        f for f in frames[decision:] if f.get("_meta", {}).get("claudeCode", {}).get("toolResponse")
    ]
    assert ran, "the approved command never ran"


def test_the_recorded_decision_is_the_operators_console_approval() -> None:
    (permission,) = load("approved-execute-permission.json")["permissions"]
    assert permission["tool_kind"] == "execute"
    assert permission["answer"] == "approve"
    assert permission["rule"] == "operator"
    assert permission["answered_via"] == "console"
    assert permission["state"] == "approved"


def test_the_request_precedes_the_decision_by_the_operators_answer_time() -> None:
    (permission,) = load("approved-execute-permission.json")["permissions"]
    waited = stamp(permission["answered_at"]) - stamp(permission["requested_at"])
    ceiling = stamp(permission["ceiling_at"]) - stamp(permission["requested_at"])
    assert 0 < waited.total_seconds() < ceiling.total_seconds()


@pytest.mark.live
def test_a_real_agent_request_waits_for_the_console_and_runs_once_approved() -> None:
    pytest.skip(
        "operator-run: needs the console and a real agent; see the operator record of ticket 246"
    )
