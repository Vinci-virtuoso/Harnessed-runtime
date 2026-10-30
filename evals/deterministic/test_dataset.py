"""AC 9 - ``dataset.jsonl`` is validated before anything is parametrised over it.

The live tier reads this file at module scope, so a malformed line fails at **collection** rather
than halfway through a run that is already spending the operator's money. That is the whole reason
:func:`evals.helpers.load_dataset` raises instead of skipping the bad line.

There is no live tier in this file. The dataset is data; reading it costs nothing and proves
nothing about a model.
"""

from __future__ import annotations

import json

import pytest

from evals.helpers import DATASET_PATH, Case, DatasetError, load_dataset, parse_dataset

# ---- offline tier ------------------------------------------------------------------------------


def test_the_dataset_file_sits_beside_the_suite_it_serves() -> None:
    assert DATASET_PATH.is_file()
    assert DATASET_PATH.name == "dataset.jsonl"


def test_every_line_carries_an_id_and_an_input() -> None:
    for case in load_dataset():
        assert case.id
        assert case.input


def test_ids_are_unique() -> None:
    ids = [case.id for case in load_dataset()]
    assert len(ids) == len(set(ids))


def test_expect_tool_is_a_tool_name_or_none() -> None:
    for case in load_dataset():
        assert case.expect_tool is None or isinstance(case.expect_tool, str)
        assert case.expect_tool != ""


def test_the_dataset_covers_both_a_tool_case_and_a_no_tool_case() -> None:
    """A dataset where every case expects a tool cannot catch a model that always reaches."""
    expectations = {case.expect_tool for case in load_dataset()}
    assert None in expectations
    assert expectations - {None}


def test_each_line_is_one_json_object_and_nothing_else() -> None:
    for line in DATASET_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert isinstance(json.loads(line), dict)


def test_a_line_missing_its_id_is_refused_by_name() -> None:
    with pytest.raises(DatasetError) as caught:
        parse_dataset('{"input": "do something"}\n')
    assert "id" in str(caught.value)


def test_a_line_missing_its_input_is_refused_by_name() -> None:
    with pytest.raises(DatasetError) as caught:
        parse_dataset('{"id": "eval_x"}\n')
    assert "input" in str(caught.value)


def test_a_duplicate_id_is_refused_by_name() -> None:
    line = '{"id": "eval_x", "input": "a"}'
    with pytest.raises(DatasetError) as caught:
        parse_dataset(f"{line}\n{line}\n")
    assert "eval_x" in str(caught.value)


def test_an_expect_tool_that_is_neither_a_name_nor_null_is_refused() -> None:
    with pytest.raises(DatasetError):
        parse_dataset('{"id": "eval_x", "input": "a", "expect_tool": 7}\n')


def test_an_unparseable_line_names_its_line_number() -> None:
    with pytest.raises(DatasetError) as caught:
        parse_dataset('{"id": "eval_x", "input": "a"}\nnot json at all\n')
    assert "2" in str(caught.value)


def test_the_optional_fields_default_to_the_permissive_shape() -> None:
    (case,) = parse_dataset('{"id": "eval_x", "input": "a"}\n')
    assert isinstance(case, Case)
    assert case.expect_in_args == ()
    assert case.expect_min_tool_calls == 0
    assert case.setup_fact is None
