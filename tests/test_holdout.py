"""FR-19: a holdout the reflector never reads, and a decision that fails closed without it.

The Vision's core bet is that a system may not grade its own homework. Five claims carry it,
and each is measured against the thing that would quietly break it:

* **The reflection payload carries zero holdout case ids.** Measured over the whole
  :class:`~harnessed.providers.types.ChatRequest` the reflector actually issues - system prompt,
  messages and metadata - rather than over the system prompt alone, which is the only field a
  fixture predicate reads but not the only field a leak could ride in.
* **Blindness is a guard, not a convention.** ``reflect()`` takes the case ids it is permitted to
  see and refuses anything else before it builds a request. The refusal is checked, and so is the
  fact that nothing reached the model.
* **The reflector has no path to the suite file.** An import scan: the module reads no YAML, opens
  no file, and imports nothing that could resolve a suite by name. A text scan then asserts the
  module never spells the holdout suite's name or any of its case ids, so a future edit cannot
  reintroduce the coupling as a hard-coded string.
* **The decision consumes three result sets and fails closed without the third.** A missing
  holdout rejects, and the rejection is the same fail-closed rule a missing case takes
  (``architecture.md``: "fails the decision closed, same as a missing holdout result under
  FR-19"). Absent that, the promoter would promote on the suite the reflector just optimized for,
  which is the failure mode this file exists to prevent.
* **The corpus and the suite are a matched pair.** Every holdout case has a fixture scope
  directory, and the ``_reflect`` corpus - the only fixtures the reflector's own request can
  select - names none of them. A predicate that recognised a holdout id would be the staged
  version of the proof.
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Iterator
from inspect import signature
from pathlib import Path

import pytest
import yaml

from harnessed import shipped
from harnessed.errors import ImproveError
from harnessed.evals.runner import CaseResult
from harnessed.evals.scoring import CaseOutcome, Decision, RunResult
from harnessed.evals.suite import Suite, load_named_suite, load_suite
from harnessed.improve import promoter as promoter_module
from harnessed.improve import reflector as reflector_module
from harnessed.memory import db
from harnessed.providers.types import ChatRequest, ChatResponse

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
REFLECTOR_PATH = PACKAGE_ROOT / "improve" / "reflector.py"
PROMOTER_PATH = PACKAGE_ROOT / "improve" / "promoter.py"
SUITES_ROOT = shipped.suites_dir()
FIXTURE_ROOT = shipped.fake_wire()
REFLECT_INDEX = FIXTURE_ROOT / "_reflect" / "_index.yaml"

#: FR-19's floor, asserted as an inequality so a later ticket may add a case without editing this.
MINIMUM_HOLDOUT_CASES = 3

MODEL = "fake-1"


# ---- the two suites ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def holdout() -> Suite:
    return load_suite(SUITES_ROOT / "holdout.yaml")


@pytest.fixture(scope="module")
def smoke() -> Suite:
    return load_suite(SUITES_ROOT / "smoke.yaml")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "harness.db")
    try:
        yield conn
    finally:
        conn.close()


class CapturingModel:
    """Records every request it is handed and answers with nothing.

    An empty answer is a null candidate, so the reflection completes without writing a row: this
    test is about the payload the reflector *sends*, and a stub that produced a candidate would
    drag the store into an assertion that has nothing to do with it.
    """

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(response_id="resp_1", model=request.model, content="")


def failure(case_id: str) -> CaseResult:
    return CaseResult(case_id=case_id, score=0.0, passed=False, detail="graders disagreed")


def outcome(case_id: str, score: float, *, passed: bool) -> CaseOutcome:
    return CaseOutcome(case_id=case_id, score=score, passed=passed)


def request_text(request: ChatRequest) -> str:
    """Every field of a request a case id could ride in, flattened into one searchable string."""
    parts = [request.model, request.system]
    parts += [message.content for message in request.messages]
    parts += [f"{key}={value}" for key, value in sorted(request.metadata.items())]
    return "\n".join(parts)


# ---- AC 4: the suite itself ---------------------------------------------------------------------


def test_the_holdout_suite_declares_at_least_three_cases(holdout: Suite) -> None:
    assert holdout.name == "holdout"
    assert len(holdout.cases) >= MINIMUM_HOLDOUT_CASES


def test_the_holdout_is_reachable_by_name(holdout: Suite) -> None:
    assert load_named_suite("holdout") == holdout


def test_the_holdout_shares_no_case_id_with_the_smoke_suite(holdout: Suite, smoke: Suite) -> None:
    """Zero overlap in both directions. One shared id would make the holdout partly seen."""
    assert set(holdout.case_ids) & set(smoke.case_ids) == set()
    assert len(set(holdout.case_ids)) == len(holdout.case_ids)


# ---- AC 5: the corpus ---------------------------------------------------------------------------


def test_every_holdout_case_has_a_fixture_scope_directory(holdout: Suite) -> None:
    """The runner sets ``metadata['scope']`` to the case id, so a missing directory is a miss."""
    missing = [
        case.id for case in holdout.cases if not (FIXTURE_ROOT / case.id / "_index.yaml").is_file()
    ]
    assert missing == []


def test_no_reflect_predicate_names_a_holdout_case(holdout: Suite) -> None:
    """The reflector's own fixtures may not recognise a holdout id.

    A predicate that did would let the served reflection depend on the unseen suite, which is the
    staged version of the proof this project argues against.
    """
    text = REFLECT_INDEX.read_text(encoding="utf-8")
    document = yaml.safe_load(text)

    named = [case_id for case_id in holdout.case_ids if case_id in text]
    assert named == []
    for variant in document["variants"]:
        assert not _mentions(variant["when"], holdout.case_ids)


def _mentions(node: object, case_ids: tuple[str, ...]) -> bool:
    """True when any holdout case id appears anywhere inside a ``when:`` clause."""
    if isinstance(node, str):
        return any(case_id in node for case_id in case_ids)
    if isinstance(node, dict):
        return any(_mentions(value, case_ids) for value in node.values())
    if isinstance(node, list):
        return any(_mentions(item, case_ids) for item in node)
    return False


# ---- AC 1: the reflection payload ---------------------------------------------------------------


def test_the_reflection_payload_contains_zero_holdout_case_ids(
    store: sqlite3.Connection, holdout: Suite, smoke: Suite
) -> None:
    model = CapturingModel()
    reflector_module.reflect(
        model,
        [failure(case_id) for case_id in smoke.case_ids],
        store,
        visible=smoke.case_ids,
        model_name=MODEL,
    )

    payload = request_text(model.requests[0])
    assert [case_id for case_id in holdout.case_ids if case_id in payload] == []
    assert all(case_id in payload for case_id in smoke.case_ids)


def test_the_reflector_refuses_a_case_id_it_may_not_see(
    store: sqlite3.Connection, holdout: Suite, smoke: Suite
) -> None:
    """A leak is an error, not a silent filter: dropping it would hide a composition bug."""
    model = CapturingModel()
    unseen = holdout.case_ids[0]

    with pytest.raises(ImproveError) as caught:
        reflector_module.reflect(
            model,
            [failure(smoke.case_ids[0]), failure(unseen)],
            store,
            visible=smoke.case_ids,
            model_name=MODEL,
        )

    assert unseen in str(caught.value)
    assert model.requests == []


def test_the_guard_runs_before_the_model_is_asked(
    store: sqlite3.Connection, holdout: Suite
) -> None:
    """With nothing visible at all, every case is refused and no request is built."""
    model = CapturingModel()
    with pytest.raises(ImproveError):
        reflector_module.reflect(
            model, [failure(holdout.case_ids[0])], store, visible=(), model_name=MODEL
        )
    assert model.requests == []


# ---- AC 1, structural: no path to the suite file ------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module)
    return modules


def test_the_reflector_has_no_path_to_a_suite_file() -> None:
    """It cannot load a suite, read a file, or resolve a path. The blindness is structural."""
    modules = _imported_modules(REFLECTOR_PATH)

    assert "harnessed.evals.suite" not in modules
    assert {"pathlib", "os", "io", "yaml"} & modules == set()
    # Exact equality, not a prefix rule: ``from harnessed.improve import candidate`` records the
    # bare package, and admitting a prefix would let ``harnessed`` match everything under it.
    assert {module for module in modules if module.startswith("harnessed")} == {
        "harnessed.errors",
        "harnessed.evals.runner",
        "harnessed.improve",
        "harnessed.improve.candidate",
        "harnessed.providers.types",
    }


def test_the_reflector_source_never_spells_the_unseen_suite(holdout: Suite) -> None:
    """Not in code, not in a docstring, not in a comment. A string is a path too."""
    source = REFLECTOR_PATH.read_text(encoding="utf-8")

    assert "holdout" not in source.casefold()
    assert [case_id for case_id in holdout.case_ids if case_id in source] == []


# ---- AC 2 and 3: the decision -------------------------------------------------------------------


def test_the_decision_takes_three_result_sets() -> None:
    """``decide(before, after, holdout)``, positional, in that order."""
    parameters = list(signature(promoter_module.decide).parameters)
    assert parameters[:3] == ["before", "after", "holdout"]


def test_the_decision_consumes_the_holdout_result() -> None:
    before = RunResult.from_cases([outcome("a", 0.0, passed=False)])
    after = RunResult.from_cases([outcome("a", 1.0, passed=True)])
    held = RunResult.from_cases([outcome("h", 1.0, passed=True)])

    decision = promoter_module.decide(before, after, held)

    assert isinstance(decision, Decision)
    assert decision.promote is True
    assert decision.rule == "score"


@pytest.mark.parametrize("absent", [None, RunResult.from_cases([])], ids=["none", "empty"])
def test_a_missing_holdout_result_rejects(absent: RunResult | None) -> None:
    """The same candidate that promotes with a holdout is refused without one."""
    before = RunResult.from_cases([outcome("a", 0.0, passed=False)])
    after = RunResult.from_cases([outcome("a", 1.0, passed=True)])

    decision = promoter_module.decide(before, after, absent)

    assert bool(decision) is False
    assert decision.promote is False
    assert decision.rule == promoter_module.MISSING_HOLDOUT
    assert decision.rule in {"score", "tiebreak", "no_gain", "regression", "missing_case"}


def test_the_rejection_still_reports_what_it_saw() -> None:
    """Fail-closed is not fail-silent: the scores it did have are on the decision."""
    before = RunResult.from_cases([outcome("a", 0.25, passed=False)])
    after = RunResult.from_cases([outcome("a", 1.0, passed=True)])

    decision = promoter_module.decide(before, after, None)

    assert decision.score_before == before.score
    assert decision.score_after == after.score
    assert decision.pass_count_before == 0
    assert decision.pass_count_after == 1


def test_the_promoter_cannot_reach_a_provider() -> None:
    """Zero model calls, forever, made structural: it has nothing to reach one with."""
    modules = _imported_modules(PROMOTER_PATH)

    assert {module for module in modules if module.startswith("harnessed.")} <= {
        "harnessed.errors",
        "harnessed.evals.scoring",
    }
    assert not any(module.startswith("harnessed.providers") for module in modules)
    assert "holdout" in PROMOTER_PATH.read_text(encoding="utf-8")
