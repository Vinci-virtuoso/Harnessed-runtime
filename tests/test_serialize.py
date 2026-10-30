"""``serialize``: the one canonical JSON spelling, for stdout and for the wire.

FR-37's byte-identity compares what ``--json`` prints against what the gateway serves. Before
ticket 062 the CLI spelled a colon with a trailing space and every other emitter did not, so a
parity test would have failed on whitespace before it got to a number. This file pins the one
spelling and pins ``render.json_object`` to it.
"""

from __future__ import annotations

import inspect
import json

import pytest

from harnessed import serialize
from harnessed.cli import render

PAYLOAD = {"score": 0.8, "passes": 4}
TIGHT = '{"passes":4,"score":0.8}'


def test_canonical_is_sorted_tight_and_unescaped() -> None:
    assert serialize.canonical(PAYLOAD) == TIGHT


def test_canonical_has_no_space_after_any_colon() -> None:
    text = serialize.canonical({"a": {"b": [1, {"c": "x: y"}]}, "d": None})
    # The only ``: `` allowed is the one inside a string value.
    assert text == '{"a":{"b":[1,{"c":"x: y"}]},"d":null}'


def test_canonical_preserves_non_ascii() -> None:
    assert serialize.canonical({"name": "café ✓"}) == '{"name":"café ✓"}'
    assert "\\u" not in serialize.canonical({"name": "café ✓"})


def test_canonical_round_trips() -> None:
    payload = {"z": [1, 2.5, "é"], "a": {"nested": True}}
    assert json.loads(serialize.canonical(payload)) == payload


def test_json_object_prints_canonical_and_one_newline(capsys: pytest.CaptureFixture[str]) -> None:
    render.json_object(PAYLOAD)
    out = capsys.readouterr().out
    assert out == serialize.canonical(PAYLOAD) + "\n"
    assert out == TIGHT + "\n"


def test_json_object_preserves_non_ascii(capsys: pytest.CaptureFixture[str]) -> None:
    render.json_object({"name": "café"})
    assert capsys.readouterr().out == '{"name":"café"}\n'


def test_json_object_owns_no_separators_literal() -> None:
    assert "separators=" not in inspect.getsource(render.json_object)


def test_serialize_imports_nothing_internal() -> None:
    source = inspect.getsource(serialize)
    assert "from harnessed" not in source
    assert "import harnessed" not in source


def test_canonical_presorted_is_canonical_for_a_payload_already_in_order() -> None:
    payload = {"tasks": [{"a": "é\n", "b": None, "c": {"x": 1, "y": [2]}}]}
    assert serialize.canonical_presorted(payload) == serialize.canonical(payload)
