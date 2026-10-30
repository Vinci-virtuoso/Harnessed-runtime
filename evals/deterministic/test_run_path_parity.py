"""An eval must exercise the run the product actually does (ticket 044).

The whole value of this suite rests on one unstated assumption: that `ask` sends the model what
`harnessed run` would send it. That assumption was true when the suite was written and **false
from the moment ticket 041 landed** - 041 put the retrieval gate, the token ceiling and the
episode write-back on the CLI's run path, and `ask` kept composing its own request with a bare
system prompt. Nothing noticed, because the difference was a default argument.

So these are the parity assertions. They are deliberately about the *composition* rather than
about any model's behaviour: a scripted client records what it was handed, and the test compares
that against what the composition root produces for the same prompt against the same stores.

Both cases here are offline. The divergence they catch is a wiring fact, and a wiring fact that
needs a live model to observe is a wiring fact nobody will observe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.helpers import ask, make_harness, text_response
from harnessed.cli import run_cmd
from harnessed.memory import semantic

#: A fact whose words overlap the task, so FTS5 actually returns it. Retrieval rebuilds the query
#: out of the task's own words; a fact sharing none of them would be absent for a reason that has
#: nothing to do with the wiring under test.
RELEVANT_FACT = "the ping case is answered with pong by the committed corpus"

TASK = "ping"


def _serve(reply: str = "pong") -> list:
    """One loop answer. The gate is answered by ``ScriptedClient`` and never touches the script."""
    return [text_response(reply)]


def test_ask_sends_the_retrieval_the_cli_would_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion that would have caught 041's divergence on the day it happened."""
    monkeypatch.chdir(tmp_path)
    from evals.helpers import ScriptedClient

    client = ScriptedClient(_serve())
    session = make_harness(tmp_path / "home", monkeypatch, client=client)
    semantic.put(session.conn, RELEVANT_FACT, source="test")

    ask(session, TASK)

    assert client.loop_requests, "the loop was never called"
    assert RELEVANT_FACT in client.loop_requests[-1].messages[0].content, (
        "the eval path sent a user turn with no retrieved context in it. `ask` is composing "
        "its own request instead of using the composition root's, so every case in this suite is "
        "a fact about a run the operator cannot trigger."
    )


def test_the_eval_path_and_the_cli_path_build_one_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Byte-for-byte, one prompt, two callers. One composition or this cannot hold."""
    monkeypatch.chdir(tmp_path)
    from evals.helpers import ScriptedClient

    through_ask = ScriptedClient(_serve())
    eval_session = make_harness(tmp_path / "ask-home", monkeypatch, client=through_ask)
    semantic.put(eval_session.conn, RELEVANT_FACT, source="test")
    ask(eval_session, TASK)

    through_cli = ScriptedClient(_serve())
    cli_session = make_harness(tmp_path / "cli-home", monkeypatch, client=through_cli)
    semantic.put(cli_session.conn, RELEVANT_FACT, source="test")
    run_cmd.solve_with_memory(cli_session, TASK)

    def loop_prompt(client: ScriptedClient) -> tuple[str, str]:
        assert client.loop_requests, "the loop was never called"
        request = client.loop_requests[-1]
        return request.system, request.messages[0].content

    assert loop_prompt(through_ask) == loop_prompt(through_cli)
