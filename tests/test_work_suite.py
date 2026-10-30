"""Ticket 236 (FR-137, decision 83): a work suite runs on its recorded routes and scores like
every other suite.

Every suite and fixture tree is written under ``tmp_path``; the committed ``corpus/suites/`` and
corpus are neither read nor written (MUST rule 8). The stub agent is the ``cli`` stub the router
tests already use.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from harnessed import shipped
from harnessed.acp.config import AgentSpec
from harnessed.cli import eval_cmd, routed
from harnessed.cli.app import app
from harnessed.providers import fake
from harnessed.router import route as route_module
from test_router import _fixed_cli_spec

pytestmark = pytest.mark.usefixtures("clean_env", "tmp_harnessed_home")

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence" / "236"
SUITE = "work-test"
WRITE_UP = "The launch email is ready and names the new signup form."


def _case(number: int, handler: str, verdict: str, traits: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "id": f"{SUITE}-{number:03d}-case",
        "input": f"task number {number}",
        "handler": handler,
        "reference": "as served",
        "verdict": verdict,
        "traits": list(traits),
    }


def _fixture(root: Path, case_id: str, reply: str) -> None:
    scope = root / case_id
    scope.mkdir(parents=True)
    (scope / "_index.yaml").write_text(
        f"scope: {case_id}\nvariants:\n  - name: base\n    when: always\n", encoding="utf-8"
    )
    (scope / "base.json").write_text(
        json.dumps(
            {
                "response_id": f"fake_{case_id}",
                "model": "fake-1",
                "content": reply,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Suites and fixture roots in ``tmp_path``, and an installed ``email`` stub agent."""
    suites_root, corpus = tmp_path / "suites", tmp_path / "fake"
    suites_root.mkdir()
    corpus.mkdir()
    monkeypatch.setattr(shipped, "suites_dir", lambda: suites_root)
    monkeypatch.setattr(fake, "DEFAULT_CORPUS_ROOT", corpus)
    monkeypatch.setenv("HARNESSED_PROVIDER", "fake")
    agents: Mapping[str, AgentSpec] = {"email": _fixed_cli_spec("email", WRITE_UP, tmp_path)}
    monkeypatch.setattr(eval_cmd, "_agents", lambda: agents)
    return suites_root, corpus


def _write_suite(suites_root: Path, cases: list[dict[str, Any]]) -> None:
    (suites_root / f"{SUITE}.yaml").write_text(
        yaml.safe_dump({"suite": SUITE, "kind": "work", "cases": cases}), encoding="utf-8"
    )


def _run(*extra: str) -> dict[str, Any]:
    result = CliRunner().invoke(app, ["eval", "run", SUITE, "--json", *extra])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert isinstance(payload, dict)
    return payload


def _capture(name: str, payload: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def test_a_seeded_wrong_fixture_fails_the_suite(work: tuple[Path, Path]) -> None:
    suites_root, corpus = work
    good, wrong = (
        _case(1, "groq_chat", "accept", ("signup form",)),
        _case(2, "groq_chat", "accept", ("signup form",)),
    )
    _write_suite(suites_root, [good, wrong])
    _fixture(corpus, good["id"], "Use the signup form on the landing page.")
    _fixture(corpus, wrong["id"], "A reply that names nothing the operator wanted.")

    payload = _run()

    by_case = {case["case_id"]: case for case in payload["cases"]}
    assert by_case[good["id"]]["passed"] is True
    assert by_case[wrong["id"]]["passed"] is False
    assert payload["score"] == 0.5 and payload["pass_count"] == 1
    _capture("wrong-fixture.json", payload)


def test_no_case_is_decided_and_every_case_goes_through_dispatch_pinned(
    work: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    suites_root, corpus = work
    cases = [_case(1, "groq_chat", "accept"), _case(2, "email", "accept", ("launch email",))]
    _write_suite(suites_root, cases)
    _fixture(corpus, cases[0]["id"], "Hello there.")
    _fixture(corpus, cases[1]["id"], WRITE_UP)
    decided: list[str] = []
    dispatched: list[route_module.Verdict | None] = []
    real = routed.dispatch

    def spy_decide(*args: object, **kwargs: object) -> object:
        decided.append("decide")
        raise AssertionError("a work case must never be decided")

    def spy_dispatch(*args: Any, **kwargs: Any) -> route_module.Verdict:
        dispatched.append(kwargs.get("pinned"))
        return real(*args, **kwargs)

    monkeypatch.setattr(route_module, "decide", spy_decide)
    monkeypatch.setattr(routed, "dispatch", spy_dispatch)

    payload = _run()

    assert decided == [], "FR-119's spy: 0 Decider calls"
    assert [(v.route, v.handler) for v in dispatched if v] == [
        ("groq_chat", "groq"),
        ("email", "agent"),
    ]
    assert payload["score"] == 1.0
    _capture("spy.json", {"decider_calls": len(decided), "pinned": len(dispatched)})


def test_an_installed_agent_route_delegates_and_traits_match_the_write_up(
    work: tuple[Path, Path],
) -> None:
    suites_root, corpus = work
    ok = _case(1, "email", "accept", ("launch email",))
    file_name = _case(2, "email", "accept", ("report.md",))
    _write_suite(suites_root, [ok, file_name])
    _fixture(corpus, ok["id"], WRITE_UP)
    _fixture(corpus, file_name["id"], WRITE_UP)

    payload = _run()

    by_case = {case["case_id"]: case["passed"] for case in payload["cases"]}
    assert by_case == {ok["id"]: True, file_name["id"]: False}, "traits match reply text only"


def test_a_missing_entry_is_unscored_entry_missing_and_the_run_completes(
    work: tuple[Path, Path],
) -> None:
    suites_root, corpus = work
    kept, missing = _case(1, "groq_chat", "accept"), _case(2, "wireframe", "accept")
    _write_suite(suites_root, [kept, missing])
    _fixture(corpus, kept["id"], "Hello.")

    payload = _run()

    assert [(u["case_id"], u["reason"]) for u in payload["unscored"]] == [
        (missing["id"], "entry_missing")
    ]
    assert payload["case_count"] == 1 and payload["score"] == 1.0
    again = CliRunner().invoke(app, ["eval", "list", "--json"])
    assert again.exit_code == 0


def test_pending_cases_are_skipped_and_counted_in_the_summary(work: tuple[Path, Path]) -> None:
    suites_root, corpus = work
    scored, waiting = _case(1, "groq_chat", "accept"), _case(2, "groq_chat", "pending")
    _write_suite(suites_root, [scored, waiting])
    _fixture(corpus, scored["id"], "Hello.")

    payload = _run()
    prose = CliRunner().invoke(app, ["eval", "run", SUITE, "--fresh"])

    assert payload["pending"] == [waiting["id"]]
    assert payload["case_count"] == 1
    assert "1 pending" in prose.output
    _capture("pending-summary.json", payload)


# Ticket 237 (FR-138, decision 83): the operator's email triage suite, scored offline from the
# recorded fixtures. The committed suite and corpus are only read; the seeded wrong fixture is
# written to a temporary copy. No test opens anything under ``examples/``.
EMAIL_SUITE = "work-email"
EMAIL_EVIDENCE = REPO_ROOT / "evidence" / "237"


@pytest.fixture
def email_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Temporary copies of the committed ``work-email`` suite and its fixtures."""
    suites_root, corpus = tmp_path / "suites", tmp_path / "fake"
    suites_root.mkdir()
    corpus.mkdir()
    shutil.copy(shipped.suites_dir() / f"{EMAIL_SUITE}.yaml", suites_root)
    for source in (shipped.fake_wire()).glob(f"{EMAIL_SUITE}-*"):
        shutil.copytree(source, corpus / source.name)
    monkeypatch.setattr(shipped, "suites_dir", lambda: suites_root)
    monkeypatch.setattr(fake, "DEFAULT_CORPUS_ROOT", corpus)
    monkeypatch.setenv("HARNESSED_PROVIDER", "fake")
    return suites_root, corpus


def _run_email() -> dict[str, Any]:
    result = CliRunner().invoke(app, ["eval", "run", EMAIL_SUITE, "--json", "--fresh"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert isinstance(payload, dict)
    return payload


def test_email_suite_is_a_fully_judged_work_suite() -> None:
    document = yaml.safe_load(
        (shipped.suites_dir() / f"{EMAIL_SUITE}.yaml").read_text(encoding="utf-8")
    )
    assert document["kind"] == "work"
    assert len(document["cases"]) >= 3
    assert {case["verdict"] for case in document["cases"]} == {"accept"}


def test_email_suite_scores_from_its_recorded_fixtures_with_no_decider_or_socket(
    email_copy: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    decided: list[str] = []

    def spy_decide(*args: object, **kwargs: object) -> object:
        decided.append("decide")
        raise AssertionError("a work case must never be decided")

    monkeypatch.setattr(route_module, "decide", spy_decide)

    payload = _run_email()

    assert decided == []
    assert payload["case_count"] >= 3 and payload["unscored"] == []
    assert payload["score"] == 1.0
    EMAIL_EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EMAIL_EVIDENCE / "offline-score.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def test_email_suite_fails_on_a_seeded_wrong_fixture_in_a_temporary_copy(
    email_copy: tuple[Path, Path],
) -> None:
    suites_root, corpus = email_copy
    # An `accept` with no traits passes any reply, so the temporary copy asks each case to name its
    # recorded class: the recorded fixtures still pass, and a wrong one cannot.
    path = suites_root / f"{EMAIL_SUITE}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for case in document["cases"]:
        case["verdict"], case["traits"] = "traits", [case["reference"]]
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert _run_email()["score"] == 1.0
    victim = sorted(corpus.iterdir())[0]
    body = victim / "base.json"
    recorded = json.loads(body.read_text(encoding="utf-8"))
    recorded["content"] = "A reply that names no class at all."
    body.write_text(json.dumps(recorded), encoding="utf-8")

    payload = _run_email()

    assert payload["score"] < 1.0
    assert {case["case_id"]: case["passed"] for case in payload["cases"]}[victim.name] is False
    EMAIL_EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EMAIL_EVIDENCE / "wrong-fixture.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def test_email_example_is_not_packaged() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "examples" not in pyproject.split("[tool.hatch.build", 1)[-1].split("[tool.", 1)[0]
