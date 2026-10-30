"""FR-62: the eval comparison is served by ``evals/scoring.py`` and never computed by a client.

Decision 37. ``GET /v1/eval/compare?before=&after=`` and ``harnessed eval compare <before>
<after> --json`` are one composition read twice: ``runner.comparison`` (the store read, which
applies ``scoring.compare``) then ``runner.comparison_payload`` (the emitter). This file holds
the three claims that make that more than a shape:

* **The comparison is the promoter's.** ``scoring.compare`` is substituted on the module object
  and the served body changes with it, so the numbers on the wire demonstrably come from that
  function rather than from something the handler happens to agree with today (MUST rule 12).
* **Nothing re-derives it.** An AST scan reads ``runner.comparison`` and the registered handler
  and finds no equality band, no comparison operator and no arithmetic. The three conditions the
  read does test - a side no ledger holds, and two suites - live in named helpers beside it, so
  the composition itself stays free of operators a second "is this better" could hide in.
* **It is not a verdict.** The payload carries no ``promote``, no ``rule`` and no difference
  figure, and ``promotes()`` raises if either surface reaches it.

The benches plant their runs straight into the ledger (``plant_eval_run``, ticket 096's helper),
because the interesting sides here - a run that recorded nothing, two runs of different suites -
are runs no solver produces in one bench.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harnessed.cli.app import main
from harnessed.errors import EvalError, GatewayError, ParameterError, SuiteMismatch
from harnessed.evals import runner, scoring
from harnessed.gateway import create_app
from harnessed.memory import db
from harnessed.serialize import canonical
from test_gateway_parity import (
    assert_parity,
    cli_bytes,
    client_over,
    endpoint_for,
    plant_eval_run,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "098"

COMPARE_PATH = "/v1/eval/compare"

#: The planted bench: one suite, two runs, one case of each of the four kinds.
BEFORE_ID = "eval_compare_before"
AFTER_ID = "eval_compare_after"
ZERO_ID = "eval_compare_zero"
OTHER_SUITE_ID = "eval_compare_other"
UNKNOWN_ID = "eval_00000000000000000000000000"

OTHER_SUITE = "regression"

#: A soft drop's stored scores, deliberately six decimal places: a rounding pass anywhere between
#: the ledger row and the wire would show up in these digits.
SOFT_BEFORE = 0.987654
SOFT_AFTER = 0.876543

BEFORE_CASES = (("case-a", SOFT_BEFORE, True), ("case-b", 1.0, True), ("case-c", 0.5, True))
AFTER_CASES = (("case-a", SOFT_AFTER, True), ("case-b", 0.0, False), ("case-d", 0.9, True))

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")


# ---- the bench ---------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def planted(clean_env: None, conn: sqlite3.Connection) -> sqlite3.Connection:
    """Four runs: a baseline, a candidate, a run that recorded nothing, and another suite's."""
    plant_eval_run(conn, BEFORE_ID, cases=BEFORE_CASES)
    plant_eval_run(conn, AFTER_ID, cases=AFTER_CASES)
    plant_eval_run(conn, ZERO_ID, cases=())
    plant_eval_run(conn, OTHER_SUITE_ID, cases=(("case-a", 1.0, True),), suite=OTHER_SUITE)
    return conn


def compare_url(before: str, after: str) -> str:
    return f"{COMPARE_PATH}?before={before}&after={after}"


def compare_argv(before: str, after: str) -> list[str]:
    return ["eval", "compare", before, after, "--json"]


def both_halves(client: TestClient, before: str, after: str) -> tuple[bytes, Any]:
    """The CLI line and the HTTP response for one pair, in that order."""
    stdout = cli_bytes(compare_argv(before, after))
    response = client.get(compare_url(before, after))
    assert response.status_code == 200, response.text
    return stdout, response


@pytest.fixture(scope="module")
def evidence() -> Iterator[list[str]]:
    """The ticket's one artefact, accumulated across the tests and written once at teardown."""
    lines: list[str] = ["ticket 098 - GET /v1/eval/compare beside `harnessed eval compare`", ""]
    yield lines
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "compare.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def record(evidence: list[str], heading: str, *body: str) -> None:
    evidence.extend([heading, *(f"  {line}" for line in body), ""])


# ---- AC 1: one composition, read twice, byte-identical ------------------------------------------


def test_the_cli_twin_and_the_route_are_byte_identical_in_decision_37_shape(
    planted: sqlite3.Connection, tmp_harnessed_home: Path, evidence: list[str]
) -> None:
    """The two halves are the same bytes, and those bytes are decision 37's object."""
    with client_over(tmp_harnessed_home) as client:
        stdout, response = both_halves(client, BEFORE_ID, AFTER_ID)
    assert_parity(stdout, response)

    payload = response.json()
    assert set(payload) == {
        "after",
        "before",
        "missing_cases",
        "new_cases",
        "regressions",
        "soft_drops",
    }
    side_keys = {
        "case_count",
        "eval_run_id",
        "label",
        "model",
        "pass_count",
        "provider",
        "samples",
        "score",
        "suite",
    }
    assert set(payload["before"]) == side_keys
    assert set(payload["after"]) == side_keys
    assert payload["before"]["eval_run_id"] == BEFORE_ID
    assert payload["after"]["eval_run_id"] == AFTER_ID
    assert payload["before"]["case_count"] == 3
    assert payload["after"]["case_count"] == 3
    assert payload["before"]["pass_count"] == 3
    assert payload["after"]["pass_count"] == 2
    assert payload["before"]["suite"] == payload["after"]["suite"] == "smoke"

    # The lists are exactly what the promoter's own function returns, in its order.
    expected = scoring.compare(
        scoring.RunResult.from_cases(
            scoring.CaseOutcome(case_id=c, score=s, passed=p) for c, s, p in BEFORE_CASES
        ),
        scoring.RunResult.from_cases(
            scoring.CaseOutcome(case_id=c, score=s, passed=p) for c, s, p in AFTER_CASES
        ),
    )
    assert payload["regressions"] == [
        {"case_id": d.case_id, "score_after": d.score_after, "score_before": d.score_before}
        for d in expected.regressions
    ]
    assert payload["soft_drops"] == [
        {"case_id": d.case_id, "score_after": d.score_after, "score_before": d.score_before}
        for d in expected.soft_drops
    ]
    assert payload["new_cases"] == list(expected.new_cases)
    assert payload["missing_cases"] == list(expected.missing_cases)
    assert [d["case_id"] for d in payload["regressions"]] == ["case-b"]
    assert [d["case_id"] for d in payload["soft_drops"]] == ["case-a"]
    assert payload["new_cases"] == ["case-d"]
    assert payload["missing_cases"] == ["case-c"]

    # `serialize.canonical`: keys sorted, one line, no spaces. The bytes are the proof.
    assert response.content == canonical(payload).encode("utf-8")
    record(
        evidence,
        "a regression, a soft drop, a new case and a missing case",
        f"cli  harnessed {' '.join(compare_argv(BEFORE_ID, AFTER_ID))}",
        stdout[:-1].decode(),
        f"http GET {compare_url(BEFORE_ID, AFTER_ID)}",
        response.content.decode(),
        "parity: equal as bytes",
    )


def test_a_served_score_is_the_ledger_row_with_no_rounding_on_the_wire(
    planted: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC8: ``score_before`` on the wire is the number the row holds, digit for digit."""
    (stored,) = planted.execute(
        "SELECT score FROM eval_results WHERE eval_run_id = ? AND case_id = ?",
        (BEFORE_ID, "case-a"),
    ).fetchone()
    with client_over(tmp_harnessed_home) as client:
        response = client.get(compare_url(BEFORE_ID, AFTER_ID))
    (drop,) = response.json()["soft_drops"]
    assert drop["score_before"] == stored == SOFT_BEFORE
    assert f'"score_before":{canonical(stored)}'.encode() in response.content
    assert b'"score_before":0.99' not in response.content, "a rounding pass reached the wire"


# ---- AC 3: the comparison is the promoter's, and nothing here re-derives it ----------------------


def test_the_served_comparison_changes_with_a_substituted_scoring_compare(
    planted: sqlite3.Connection, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Substituted on the ``scoring`` module the read reaches through, the body follows it.

    Not vacuous in either direction: the untampered body is read in the same test and differs.
    """
    with client_over(tmp_harnessed_home) as client:
        honest = client.get(compare_url(BEFORE_ID, AFTER_ID))
    assert honest.status_code == 200, honest.text

    substitute = scoring.Comparison(
        regressions=(scoring.ScoreDelta(case_id="substituted", score_before=1.0, score_after=0.0),),
        soft_drops=(),
        new_cases=("substituted-new",),
        missing_cases=(),
    )
    with monkeypatch.context() as patch:
        patch.setattr(scoring, "compare", lambda before, after: substitute)
        with client_over(tmp_harnessed_home) as client:
            tampered = client.get(compare_url(BEFORE_ID, AFTER_ID))
    assert tampered.status_code == 200, tampered.text
    assert [d["case_id"] for d in tampered.json()["regressions"]] == ["substituted"]
    assert tampered.json()["new_cases"] == ["substituted-new"]
    assert tampered.json()["soft_drops"] == []
    assert tampered.content != honest.content


def scanned(func: Callable[..., Any]) -> tuple[str, ast.AST]:
    """One callable's own source and the tree of what it *does*, dedented so a nested handler
    parses.

    Type annotations are lifted off first. ``str | None`` on a query parameter is an ``ast.BinOp``
    and is not arithmetic, and a scan that reported it would push the handler into a weaker
    signature to stay green - which is the scan changing the code rather than checking it.
    """
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node.returns = None
        elif isinstance(node, ast.arg | ast.AnnAssign):
            node.annotation = None  # type: ignore[assignment]
    return source, tree


def handler_of(home: Path) -> Callable[..., Any]:
    return endpoint_for(create_app(home / db.DATABASE_FILENAME), COMPARE_PATH)


def operators_in(tree: ast.AST) -> list[str]:
    """Every comparison and every arithmetic operation the tree spells, as a report."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            found.append(f"line {node.lineno}: comparison {ast.unparse(node)}")
        elif isinstance(node, ast.BinOp | ast.AugAssign | ast.UnaryOp):
            found.append(f"line {node.lineno}: arithmetic {ast.unparse(node)}")
    return found


@pytest.mark.usefixtures("tmp_harnessed_home")
def test_the_read_and_the_handler_spell_no_band_no_comparison_and_no_arithmetic(
    tmp_harnessed_home: Path,
) -> None:
    """AC3's scan: the composition is a composition, with nowhere for a second rule to hide."""
    for label, func in (
        ("runner.comparison", runner.comparison),
        (f"the handler for {COMPARE_PATH}", handler_of(tmp_harnessed_home)),
    ):
        source, tree = scanned(func)
        assert "EPSILON" not in source, f"{label} names the equality band"
        assert operators_in(tree) == [], f"{label} computes: {operators_in(tree)}"


def test_the_scan_catches_a_handler_that_computes_a_difference() -> None:
    """Not vacuous: a synthetic handler doing the arithmetic FR-62 exists to prevent is caught."""
    source = textwrap.dedent(
        """
        def computed(before, after):
            if before.score - after.score > EPSILON:
                return {"worse": True}
            return {"worse": False}
        """
    )
    report = operators_in(ast.parse(source))
    assert "EPSILON" in source
    assert len(report) == 2
    assert any("comparison" in line for line in report)
    assert any("arithmetic" in line for line in report)


def test_lifting_the_annotations_off_does_not_lift_the_arithmetic_off() -> None:
    """The scan reads a live, fully annotated function and still finds what its body computes."""

    def annotated(before: str | None = None, after: float | int = 0) -> float | None:
        drop: float | None = after - len(before or "")
        return drop

    _, tree = scanned(annotated)
    report = operators_in(tree)
    assert len(report) == 1
    assert "arithmetic" in report[0]
    assert "len(before or '')" in report[0]


# ---- AC 9: a comparison is not a verdict -------------------------------------------------------


def test_the_payload_carries_no_verdict_and_promotes_is_never_called(
    planted: sqlite3.Connection, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``promote``, no ``rule``, no difference field, and the promoter is not reached."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("the comparison surface called promotes()")

    with monkeypatch.context() as patch:
        patch.setattr(scoring, "promotes", refuse)
        stdout = cli_bytes(compare_argv(BEFORE_ID, AFTER_ID))
        with client_over(tmp_harnessed_home) as client:
            response = client.get(compare_url(BEFORE_ID, AFTER_ID))
    assert response.status_code == 200, response.text
    assert stdout[:-1] == response.content

    forbidden = ("promote", "rule", "verdict", "difference", "delta")
    body = response.content.decode()
    for key in forbidden:
        assert f'"{key}"' not in body, f"the comparison serves a {key} field"
    payload = response.json()
    for side in (payload["before"], payload["after"]):
        for key in forbidden:
            assert key not in side


# ---- AC 4: an eval run id no ledger holds, on either side ---------------------------------------


@pytest.mark.parametrize("side", ["before", "after"])
def test_an_unknown_eval_run_id_is_refused_by_both_halves_and_names_its_side(
    planted: sqlite3.Connection,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    side: str,
    evidence: list[str],
) -> None:
    """AC4, the ticket 079 lesson: the message names which side asked and the id it asked for."""
    before, after = (UNKNOWN_ID, AFTER_ID) if side == "before" else (BEFORE_ID, UNKNOWN_ID)

    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harnessed", *compare_argv(before, after)])
    with pytest.raises(SystemExit) as exited:
        main()
    captured = capsys.readouterr()
    assert exited.value.code not in (0, None), "the CLI accepted an id no ledger holds"
    assert captured.out == "", captured.out
    assert UNKNOWN_ID in captured.err
    assert side in captured.err

    with client_over(tmp_harnessed_home) as client:
        response = client.get(compare_url(before, after))
    assert response.status_code == 404, response.text
    payload = response.json()
    assert set(payload) == {"error", "message"}
    assert payload["error"] == "SurfaceNotFound"
    assert UNKNOWN_ID in payload["message"]
    assert side in payload["message"]
    record(
        evidence,
        f"an eval run id no ledger holds, on the {side} side",
        f"cli  harnessed {' '.join(compare_argv(before, after))}",
        f"exit {exited.value.code}  stderr: {captured.err.strip()}",
        f"http GET {compare_url(before, after)}",
        f"{response.status_code}  {response.content.decode()}",
        "both refuse: yes",
    )


# ---- AC 5: two suites is 400, never a 200 -------------------------------------------------------


def test_two_eval_run_ids_of_different_suites_are_refused_by_both_halves(
    planted: sqlite3.Connection,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    evidence: list[str],
) -> None:
    """AC5: 400 ``SuiteMismatch`` naming both ids and both suites; the CLI exits 1."""
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harnessed", *compare_argv(BEFORE_ID, OTHER_SUITE_ID)])
    with pytest.raises(SystemExit) as exited:
        main()
    captured = capsys.readouterr()
    assert exited.value.code == 1, captured.err
    assert captured.out == "", captured.out

    with client_over(tmp_harnessed_home) as client:
        response = client.get(compare_url(BEFORE_ID, OTHER_SUITE_ID))
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["error"] == "SuiteMismatch"
    for named in (BEFORE_ID, OTHER_SUITE_ID, "smoke", OTHER_SUITE):
        assert named in payload["message"], payload["message"]
        assert named in captured.err, captured.err
    record(
        evidence,
        "two eval run ids of different suites",
        f"cli  harnessed {' '.join(compare_argv(BEFORE_ID, OTHER_SUITE_ID))}",
        f"exit {exited.value.code}  stderr: {captured.err.strip()}",
        f"http GET {compare_url(BEFORE_ID, OTHER_SUITE_ID)}",
        f"{response.status_code}  {response.content.decode()}",
        "never a 200: correct",
    )


def test_suite_mismatch_is_an_eval_error_and_parameter_error_is_a_gateway_error() -> None:
    """Both join the tree where the architecture's error map puts them."""
    assert issubclass(SuiteMismatch, EvalError)
    assert issubclass(ParameterError, GatewayError)


# ---- AC 6: a zero-case side, in each direction --------------------------------------------------


@pytest.mark.parametrize("side", ["before", "after"])
def test_a_zero_case_side_serves_null_and_never_a_zero_score(
    planted: sqlite3.Connection, tmp_harnessed_home: Path, side: str, evidence: list[str]
) -> None:
    """AC6: 200, that side's ``score`` is null, its ``case_count`` is 0, and it serves no 0.0."""
    before, after = (ZERO_ID, AFTER_ID) if side == "before" else (BEFORE_ID, ZERO_ID)
    with client_over(tmp_harnessed_home) as client:
        stdout, response = both_halves(client, before, after)
    assert_parity(stdout, response)

    payload = response.json()
    empty, measured = (
        (payload["before"], payload["after"])
        if side == "before"
        else (payload["after"], payload["before"])
    )
    assert empty["score"] is None, empty
    assert empty["case_count"] == 0
    assert "0.0" not in canonical(empty), f"the zero side served a 0.0: {canonical(empty)}"
    assert measured["score"] is not None, "the measured side lost its score"
    assert b'"score":null' in response.content

    expected = scoring.compare(
        scoring.RunResult.from_cases(()) if side == "before" else _result(BEFORE_CASES),
        _result(AFTER_CASES) if side == "before" else scoring.RunResult.from_cases(()),
    )
    assert payload["new_cases"] == list(expected.new_cases)
    assert payload["missing_cases"] == list(expected.missing_cases)
    assert [d["case_id"] for d in payload["regressions"]] == [
        d.case_id for d in expected.regressions
    ]
    assert [d["case_id"] for d in payload["soft_drops"]] == [d.case_id for d in expected.soft_drops]
    record(
        evidence,
        f"a zero-case run on the {side} side",
        f"cli  harnessed {' '.join(compare_argv(before, after))}",
        stdout[:-1].decode(),
        f"http GET {compare_url(before, after)}",
        response.content.decode(),
        "parity: equal as bytes",
    )


def _result(cases: tuple[tuple[str, float, bool], ...]) -> scoring.RunResult:
    return scoring.RunResult.from_cases(
        scoring.CaseOutcome(case_id=c, score=s, passed=p) for c, s, p in cases
    )


# ---- AC 7: a missing parameter, and the same id on both sides -----------------------------------


@pytest.mark.parametrize(
    "query",
    [
        f"?after={AFTER_ID}",
        f"?before={BEFORE_ID}",
        "",
        f"?before=&after={AFTER_ID}",
        f"?before={BEFORE_ID}&after=",
    ],
)
def test_a_missing_or_empty_parameter_is_a_400_parameter_error(
    planted: sqlite3.Connection, tmp_harnessed_home: Path, query: str, evidence: list[str]
) -> None:
    """AC7: ``required`` refuses it before any store read, in decision 24's one body."""
    with client_over(tmp_harnessed_home) as client:
        response = client.get(f"{COMPARE_PATH}{query}")
    assert response.status_code == 400, response.text
    payload = response.json()
    assert set(payload) == {"error", "message"}
    assert payload["error"] == "ParameterError"
    assert "before" in payload["message"] or "after" in payload["message"]
    assert "detail" not in payload
    record(
        evidence,
        f"a missing or empty parameter: {query or '(no query string)'}",
        f"http GET {COMPARE_PATH}{query}",
        f"{response.status_code}  {response.content.decode()}",
    )


def test_the_cli_twin_requires_both_arguments() -> None:
    """Typer makes both arguments required, so the CLI cannot ask the half-question either."""
    parameters = inspect.signature(runner.comparison).parameters
    assert list(parameters) == ["conn", "before", "after"]
    assert all(p.default is inspect.Parameter.empty for p in parameters.values())


def test_the_same_eval_run_id_on_both_sides_is_a_200_with_four_empty_lists(
    planted: sqlite3.Connection, tmp_harnessed_home: Path
) -> None:
    """AC7's second half: a run compared with itself moved nothing, which is an answer."""
    with client_over(tmp_harnessed_home) as client:
        stdout, response = both_halves(client, BEFORE_ID, BEFORE_ID)
    assert_parity(stdout, response)
    payload = response.json()
    assert payload["regressions"] == []
    assert payload["soft_drops"] == []
    assert payload["new_cases"] == []
    assert payload["missing_cases"] == []
    assert payload["before"] == payload["after"]


# ---- the store read, directly --------------------------------------------------------------------


def test_comparison_is_the_one_store_read_and_carries_scoring_s_own_object(
    planted: sqlite3.Connection,
) -> None:
    """``EvalComparison`` is two runs and ``scoring.Comparison``, carried whole."""
    compared = runner.comparison(planted, BEFORE_ID, AFTER_ID)
    assert isinstance(compared, runner.EvalComparison)
    assert isinstance(compared.comparison, scoring.Comparison)
    assert compared.before.eval_run_id == BEFORE_ID
    assert compared.after.eval_run_id == AFTER_ID
    assert compared.comparison == scoring.compare(compared.before.result, compared.after.result)
    for name in ("EvalComparison", "comparison", "comparison_payload"):
        assert name in runner.__all__, f"{name} is not exported"


def test_comparison_payload_is_the_only_emitter_the_two_surfaces_use(
    planted: sqlite3.Connection,
) -> None:
    """Both call sites reach the emitter by name; neither spells the object out (MUST rule 16)."""
    sources = (
        REPO_ROOT / "src" / "harnessed" / "cli" / "eval_cmd.py",
        REPO_ROOT / "src" / "harnessed" / "gateway" / "routes" / "evals.py",
    )
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "comparison_payload" in text, f"{path.name} does not call the emitter"
        assert '"regressions"' not in text, f"{path.name} spells the payload by hand"
        assert '"soft_drops"' not in text, f"{path.name} spells the payload by hand"

    payload = runner.comparison_payload(runner.comparison(planted, BEFORE_ID, AFTER_ID))
    assert json.loads(canonical(payload)) == payload
