"""AC 6 - the playback client the whole offline tier stands on.

``ScriptedClient`` is waku's structure with Harnessed's types: it satisfies
``providers.base.Provider``, hands back a fixed list of responses in order, and when the list runs
out it says so by name rather than raising an ``IndexError`` from inside a list subscript.

There is no live tier in this file. A scripted client has no live counterpart - it exists
precisely so that a tier can run without one.
"""

from __future__ import annotations

import pytest

from evals.helpers import ScriptedClient, ScriptExhaustedError, text_response, tool_response
from harnessed.providers.base import Provider
from harnessed.providers.types import ChatRequest, Message

# ---- offline tier ------------------------------------------------------------------------------


def request(prompt: str = "anything") -> ChatRequest:
    return ChatRequest(model="scripted", messages=(Message(role="user", content=prompt),))


def test_scripted_client_satisfies_the_provider_protocol() -> None:
    assert isinstance(ScriptedClient([text_response("one")]), Provider)


def test_responses_come_back_in_the_order_they_were_scripted() -> None:
    client = ScriptedClient(
        [text_response("first"), tool_response("fs_read", {"path": "a.md"}), text_response("third")]
    )
    assert client.complete(request()).content == "first"
    assert client.complete(request()).tool_calls[0].name == "fs_read"
    assert client.complete(request()).content == "third"


def test_every_request_is_kept_so_a_test_can_assert_on_what_the_loop_sent() -> None:
    client = ScriptedClient([text_response("one"), text_response("two")])
    client.complete(request("alpha"))
    client.complete(request("beta"))
    assert [r.messages[0].content for r in client.requests] == ["alpha", "beta"]


def test_an_exhausted_script_raises_a_named_error_naming_the_script() -> None:
    client = ScriptedClient([text_response("only one")])
    client.complete(request())
    with pytest.raises(ScriptExhaustedError) as caught:
        client.complete(request("one turn too many"))
    message = str(caught.value)
    assert "1" in message
    assert "exhausted" in message


def test_the_exhaustion_error_is_not_an_index_error() -> None:
    """An ``IndexError`` would be indistinguishable from a bug inside the loop."""
    client = ScriptedClient([])
    with pytest.raises(ScriptExhaustedError):
        client.complete(request())
    assert not issubclass(ScriptExhaustedError, IndexError)
