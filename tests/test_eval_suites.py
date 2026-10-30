"""FR-14: YAML suites load into ordered cases with stable, authored case ids.

Two invariants carry the weight here and they point in opposite directions. A case id is the key
``eval_results`` rows are written under, so a reordered suite must not silently reassign history;
and a case id is the fixture ``scope`` FakeProvider selects on (architecture.md, THE CRUX), so a
renamed case must not silently start missing its corpus. Both are properties of *authored* ids,
which is why the loader never derives one from list position.

Every malformed-suite test writes its YAML into ``tmp_path``. The committed corpus is reviewed
code (All AI agents MUST rule 8) and a deliberately broken suite is not something to commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessed import shipped
from harnessed.errors import EvalError, HarnessError, SuiteLoadError
from harnessed.evals.suite import (
    GRADER_NAMES,
    Case,
    GraderSpec,
    Suite,
    load_named_suite,
    load_suite,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = shipped.fake_wire()
SMOKE_PATH = shipped.suites_dir() / "smoke.yaml"

#: FR-14 and the PRD's success criteria: "1 smoke suite with at least 5 cases". Asserted as a
#: floor rather than as an equality, so a later case addition is an addition and not a test edit
#: (ticket 006's lesson about pinned literals).
MINIMUM_SMOKE_CASES = 5


def write_suite(tmp_path: Path, body: str, name: str = "sample.yaml") -> Path:
    """Write one suite document. The default filename matches the ``suite: sample`` every
    sample below declares, because the loader checks that pair and a mismatch would mask the
    one defect each sample is actually about."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# A suite that loads. Every malformed sample below is this document with one thing wrong, so a
# failure is attributable to the one edit and not to a second mistake in the sample.
GOOD = """
suite: sample
cases:
  - id: sample-001
    prompt: What is the capital of Nigeria?
    graders:
      - name: contains_all
        expect:
          values: ["Abuja"]
"""


# ---- the committed smoke suite -----------------------------------------------------------------


def test_smoke_suite_loads_the_expected_cases() -> None:
    suite = load_suite(SMOKE_PATH)

    assert suite.name == "smoke"
    assert len(suite.cases) >= MINIMUM_SMOKE_CASES
    assert len(suite.cases) == len(set(suite.case_ids))
    assert all(isinstance(case, Case) for case in suite.cases)
    assert all(case.id and case.prompt for case in suite.cases)


def test_smoke_suite_is_reachable_by_name() -> None:
    """``load_named_suite('smoke')`` and the path form return the same document."""
    assert load_named_suite("smoke") == load_suite(SMOKE_PATH)
    assert shipped.suites_dir() / "smoke.yaml" == SMOKE_PATH


def test_case_ids_are_stable_across_loads() -> None:
    """Same file, two loads, identical ids in identical order.

    This is the ``eval_results`` half of the invariant: the ledger keys on the case id, so an id
    that moved between loads would file two runs of one case as two different cases.
    """
    first = load_suite(SMOKE_PATH)
    second = load_suite(SMOKE_PATH)

    assert first.case_ids == second.case_ids
    assert first == second


def test_case_ids_are_authored_not_positional(tmp_path: Path) -> None:
    """Reordering a suite reorders the cases and renames nothing.

    Ids derived from list position would make a reordering look, in the ledger, like every case
    after the moved one changed identity.
    """
    forward = "suite: s\ncases:\n" + "".join(
        f"  - id: case-{n}\n    prompt: p{n}\n    graders:\n      - name: exact_match\n"
        for n in ("a", "b", "c")
    )
    backward = "suite: s\ncases:\n" + "".join(
        f"  - id: case-{n}\n    prompt: p{n}\n    graders:\n      - name: exact_match\n"
        for n in ("c", "b", "a")
    )

    first = load_suite(write_suite(tmp_path, forward, name="s.yaml"))
    second = load_suite(write_suite(tmp_path, backward, name="s.yaml"))

    assert first.case_ids == ("case-a", "case-b", "case-c")
    assert second.case_ids == ("case-c", "case-b", "case-a")
    assert set(first.case_ids) == set(second.case_ids)
    assert first.case("case-a").prompt == second.case("case-a").prompt


def test_every_case_has_a_fixture_scope_directory() -> None:
    """The FakeProvider half: a case id is the ``scope`` its corpus directory is named for.

    The runner sets ``metadata["scope"]`` to the case id (ticket 019), so a case with no
    directory is a ``FixtureMissError`` at run time. Asserting it here makes the corpus and the
    suite a matched pair at load time instead.
    """
    suite = load_suite(SMOKE_PATH)

    missing = [
        case.id for case in suite.cases if not (FIXTURE_ROOT / case.id / "_index.yaml").is_file()
    ]
    assert missing == []


def test_at_least_one_case_has_a_with_procedure_variant() -> None:
    """The flywheel needs a case whose prompt can genuinely change the served response.

    Without one, FR-25's causal chain has no link to move: every candidate would serve ``base``
    and the promoter would correctly reject forever.
    """
    suite = load_suite(SMOKE_PATH)

    with_procedure = [
        case.id
        for case in suite.cases
        if (FIXTURE_ROOT / case.id / "with_procedure.json").is_file()
    ]
    assert with_procedure, "no smoke case has a with_procedure variant"


def test_every_grader_named_by_the_smoke_suite_is_known() -> None:
    suite = load_suite(SMOKE_PATH)

    named = {spec.name for case in suite.cases for spec in case.graders}
    assert named <= set(GRADER_NAMES)
    assert named, "the smoke suite names no graders at all"


def test_grader_names_are_sorted_and_unique() -> None:
    assert list(GRADER_NAMES) == sorted(set(GRADER_NAMES))


def test_case_lookup_by_id() -> None:
    suite = load_suite(SMOKE_PATH)
    first = suite.cases[0]

    assert suite.case(first.id) is first
    with pytest.raises(SuiteLoadError, match="no case"):
        suite.case("no-such-case")


# ---- the routing suite is neither smoke nor holdout (ticket 178, FR-118) -------------------------

ROUTING_TASKS = shipped.routing_tasks()


def test_no_routing_task_is_a_smoke_or_holdout_case() -> None:
    """The routing suite never reaches the promoter: none of its tasks is a scored eval case."""
    import yaml  # type: ignore[import-untyped]

    routing = yaml.safe_load(ROUTING_TASKS.read_text(encoding="utf-8"))["tasks"]
    routing_ids = {task["id"] for task in routing}
    routing_prompts = {task["task"].casefold() for task in routing}
    assert len(routing_ids) == 28

    for name in ("smoke", "holdout"):
        suite = load_named_suite(name)
        assert not routing_ids & set(suite.case_ids), name
        assert not routing_prompts & {case.prompt.casefold() for case in suite.cases}, name


def test_routing_is_not_a_named_eval_suite() -> None:
    """``routing`` lives in ``corpus/routing/``, outside the suites the runner and promoter load."""
    assert not (shipped.suites_dir() / "routing.yaml").exists()
    with pytest.raises(SuiteLoadError):
        load_named_suite("routing")


# ---- validation: every rejection names the file and the field ----------------------------------


def test_unknown_grader_names_the_file_and_the_field(tmp_path: Path) -> None:
    path = write_suite(tmp_path, GOOD.replace("contains_all", "vibes_check"))

    with pytest.raises(SuiteLoadError) as caught:
        load_suite(path)

    message = str(caught.value)
    assert str(path) in message
    assert "vibes_check" in message
    assert "graders" in message
    assert "contains_all" in message, "the message must list the permitted names"


def test_duplicate_case_id_names_the_duplicate(tmp_path: Path) -> None:
    path = write_suite(
        tmp_path,
        GOOD
        + """  - id: sample-001
    prompt: Ask it twice.
    graders:
      - name: contains_all
        expect:
          values: ["Abuja"]
""",
    )

    with pytest.raises(SuiteLoadError) as caught:
        load_suite(path)

    message = str(caught.value)
    assert str(path) in message
    assert "sample-001" in message
    assert "duplicate" in message.casefold()


@pytest.mark.parametrize(
    ("removed", "body"),
    [
        ("suite", "cases:\n  - id: a\n    prompt: p\n    graders:\n      - name: exact_match\n"),
        ("cases", "suite: sample\n"),
        ("id", "suite: sample\ncases:\n  - prompt: p\n    graders:\n      - name: exact_match\n"),
        ("prompt", "suite: sample\ncases:\n  - id: a\n    graders:\n      - name: exact_match\n"),
        ("graders", "suite: sample\ncases:\n  - id: a\n    prompt: p\n"),
        ("name", "suite: sample\ncases:\n  - id: a\n    prompt: p\n    graders:\n      - {}\n"),
    ],
)
def test_missing_required_field_names_the_file_and_the_field(
    tmp_path: Path, removed: str, body: str
) -> None:
    path = write_suite(tmp_path, body)

    with pytest.raises(SuiteLoadError) as caught:
        load_suite(path)

    message = str(caught.value)
    assert str(path) in message
    assert removed in message


@pytest.mark.parametrize(
    ("problem", "body"),
    [
        ("not a mapping", "- just\n- a\n- list\n"),
        ("empty document", "\n"),
        ("cases not a list", "suite: sample\ncases: {a: b}\n"),
        ("empty cases", "suite: sample\ncases: []\n"),
        ("case not a mapping", "suite: sample\ncases:\n  - just a string\n"),
        (
            "blank suite name",
            "suite: '  '\ncases:\n  - id: a\n    prompt: p\n"
            "    graders:\n      - name: exact_match\n",
        ),
        (
            "empty graders",
            "suite: sample\ncases:\n  - id: a\n    prompt: p\n    graders: []\n",
        ),
        (
            "graders not a list",
            "suite: sample\ncases:\n  - id: a\n    prompt: p\n    graders: {name: exact_match}\n",
        ),
        (
            "grader not a mapping",
            "suite: sample\ncases:\n  - id: a\n    prompt: p\n    graders:\n      - exact_match\n",
        ),
        (
            "unknown case field",
            "suite: sample\ncases:\n  - id: a\n    prompt: p\n    weight: 3\n"
            "    graders:\n      - name: exact_match\n",
        ),
        (
            "unknown grader field",
            "suite: sample\ncases:\n  - id: a\n    prompt: p\n"
            "    graders:\n      - name: exact_match\n        cheat: true\n",
        ),
        (
            "expect not a mapping",
            "suite: sample\ncases:\n  - id: a\n    prompt: p\n"
            "    graders:\n      - name: exact_match\n        expect: [1, 2]\n",
        ),
        (
            "threshold out of range",
            "suite: sample\ncases:\n  - id: a\n    prompt: p\n"
            "    graders:\n      - name: exact_match\n        threshold: 1.5\n",
        ),
        (
            "threshold not a number",
            "suite: sample\ncases:\n  - id: a\n    prompt: p\n"
            "    graders:\n      - name: exact_match\n        threshold: high\n",
        ),
        (
            "blank prompt",
            "suite: sample\ncases:\n  - id: a\n    prompt: '   '\n"
            "    graders:\n      - name: exact_match\n",
        ),
        (
            "prompt not a string",
            "suite: sample\ncases:\n  - id: a\n    prompt: 7\n"
            "    graders:\n      - name: exact_match\n",
        ),
        (
            "unknown top-level field",
            "suite: sample\nweights: {a: 1}\ncases:\n  - id: a\n    prompt: p\n"
            "    graders:\n      - name: exact_match\n",
        ),
    ],
)
def test_malformed_suite_is_a_named_load_error(tmp_path: Path, problem: str, body: str) -> None:
    path = write_suite(tmp_path, body)

    with pytest.raises(SuiteLoadError) as caught:
        load_suite(path)

    assert str(path) in str(caught.value), problem


@pytest.mark.parametrize(
    "case_id", ["../escape", "with/slash", "with\\backslash", "  ", "with.dot", ""]
)
def test_case_id_must_be_usable_as_a_fixture_directory_name(tmp_path: Path, case_id: str) -> None:
    """A case id becomes a corpus directory name, so it may not walk or nest.

    Rejecting the shape at load time is what keeps ``FIXTURE_ROOT / case.id`` a join and never a
    traversal, the same discipline ``tools/`` applies to a model-supplied path.
    """
    path = write_suite(
        tmp_path,
        f"suite: sample\ncases:\n  - id: {case_id!r}\n    prompt: p\n"
        f"    graders:\n      - name: exact_match\n",
    )

    with pytest.raises(SuiteLoadError) as caught:
        load_suite(path)

    assert "id" in str(caught.value)


def test_invalid_yaml_is_a_load_error(tmp_path: Path) -> None:
    path = write_suite(tmp_path, "suite: sample\ncases:\n  - id: [unclosed\n")

    with pytest.raises(SuiteLoadError) as caught:
        load_suite(path)

    assert str(path) in str(caught.value)


def test_missing_file_is_a_load_error(tmp_path: Path) -> None:
    path = tmp_path / "absent.yaml"

    with pytest.raises(SuiteLoadError) as caught:
        load_suite(path)

    assert str(path) in str(caught.value)


def test_unknown_named_suite_lists_what_is_available(tmp_path: Path) -> None:
    (tmp_path / "smoke.yaml").write_text(GOOD.replace("suite: sample", "suite: smoke"), "utf-8")

    with pytest.raises(SuiteLoadError) as caught:
        load_named_suite("holdout", root=tmp_path)

    assert "smoke" in str(caught.value)


def test_declared_name_must_match_the_filename(tmp_path: Path) -> None:
    """The suite names itself and the file names it too; disagreement is a typo, not a nuance."""
    path = write_suite(tmp_path, GOOD, name="sample.yaml")
    assert load_suite(path).name == "sample"

    mismatched = write_suite(tmp_path, GOOD, name="other.yaml")
    with pytest.raises(SuiteLoadError) as caught:
        load_suite(mismatched)

    assert "sample" in str(caught.value)
    assert "other" in str(caught.value)


# ---- the error is in the tree --------------------------------------------------------------


def test_suite_load_error_is_an_eval_error() -> None:
    """Raised from the tree, never extending it (architecture.md's error contract)."""
    assert issubclass(SuiteLoadError, EvalError)
    assert issubclass(EvalError, HarnessError)


# ---- value types are frozen ------------------------------------------------------------------


def test_loaded_values_are_immutable() -> None:
    suite = load_suite(SMOKE_PATH)
    case = suite.cases[0]

    for target, field in ((suite, "name"), (case, "id"), (case.graders[0], "name")):
        with pytest.raises(AttributeError):
            setattr(target, field, "mutated")

    assert isinstance(suite, Suite)
    assert isinstance(case.graders[0], GraderSpec)
    assert isinstance(suite.cases, tuple)
    assert isinstance(case.graders, tuple)


# ---- Ticket 235: a work suite and its verdicts -------------------------------------------------


def test_a_work_suite_loads_with_its_verdicts_and_counts_the_pending(tmp_path: Path) -> None:
    body = """
suite: work
kind: work
cases:
  - {id: work-001-a, input: hi, handler: groq_chat, reference: hello, verdict: pending}
  - {id: work-002-b, input: yo, handler: groq_chat, reference: hey, verdict: accept}
  - id: work-003-c
    input: sup
    handler: groq_chat
    reference: nm
    verdict: traits
    traits: [polite]
"""
    loaded = load_suite(write_suite(tmp_path, body, "work.yaml"))

    assert loaded.kind == "work"
    assert [case.pending for case in loaded.cases] == [True, False, False]
    assert [case.id for case in loaded.pending] == ["work-001-a"]
    assert loaded.cases[2].traits == ("polite",)


@pytest.mark.parametrize(
    "case",
    [
        "{id: w-001-a, input: hi, handler: g, reference: r, verdict: maybe}",
        "{id: w-001-a, input: hi, handler: g, reference: r, verdict: traits}",
        "{id: w-001-a, input: hi, handler: g}",
    ],
)
def test_a_malformed_work_case_is_a_named_load_error(tmp_path: Path, case: str) -> None:
    body = f"suite: work\nkind: work\ncases:\n  - {case}\n"
    with pytest.raises(SuiteLoadError, match="w-001-a"):
        load_suite(write_suite(tmp_path, body, "work.yaml"))


# ---- Ticket 238: the listing the Evals page serves --------------------------------------------


def test_the_listing_counts_pending_as_strings_and_nulls_a_suite_that_never_ran(
    tmp_path: Path, tmp_harnessed_home: Path
) -> None:
    from harnessed.evals.suite import listing, listing_payload
    from harnessed.memory import db
    from test_gateway_parity import plant_eval_run

    write_suite(
        tmp_path,
        "suite: work\nkind: work\ndescription: mine\ncases:\n"
        "  - {id: a, input: x, handler: groq_chat, reference: r, verdict: pending}\n"
        "  - {id: b, input: y, handler: groq_chat, reference: r, verdict: accept}\n",
        "work.yaml",
    )
    write_suite(
        tmp_path,
        "suite: done\nkind: work\ncases:\n"
        "  - {id: a, input: x, handler: groq_chat, reference: r, verdict: accept}\n",
        "done.yaml",
    )
    conn = db.connect()
    try:
        plant_eval_run(conn, "eval_planted", cases=(("a", 0.5, False),), suite="done")
        served = listing_payload(listing(conn, tmp_path))["suites"]
    finally:
        conn.close()
    by_name = {row["suite"]: row for row in served}  # type: ignore[index]
    assert list(by_name) == ["done", "work"]
    work, done = by_name["work"], by_name["done"]
    assert (work["kind"], work["description"]) == ("work", "mine")
    assert (work["cases_total"], work["cases_pending"]) == ("2", "1")
    assert [work[k] for k in work if k.startswith("latest_")] == [None] * 4
    assert (done["cases_total"], done["cases_pending"]) == ("1", "0")
    assert done["latest_eval_run_id"] == "eval_planted"
    assert done["latest_score"] == 0.5
    assert done["latest_label"] == "baseline"


# ---- ticket 249: the memory suite --------------------------------------------------------------


def memory_suite() -> object:
    from harnessed.evals.suite import load_named_suite

    return load_named_suite("memory")


def test_the_memory_suite_loads_with_its_five_recorded_episodes() -> None:
    from harnessed.evals.suite import KIND_MEMORY

    loaded = memory_suite()
    assert loaded.kind == KIND_MEMORY  # type: ignore[attr-defined]
    assert loaded.case_ids == ("memory-001-spelling",)  # type: ignore[attr-defined]
    case = loaded.case("memory-001-spelling")  # type: ignore[attr-defined]
    assert len(case.episodes) == 5
    # The answer exists only across the episodes: no one of them states it.
    assert not any("british" in episode.lower() for episode in case.episodes)
    assert (FIXTURE_ROOT / "memory-001-spelling" / "_index.yaml").is_file()


def test_seeding_is_idempotent_and_refuses_a_home_with_a_real_episode(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    from harnessed.evals.suite import SEED_PREFIX, seed_episodes
    from harnessed.memory import db, episodic

    loaded = memory_suite()
    conn = db.connect()
    try:
        assert seed_episodes(conn, loaded) == 5  # type: ignore[arg-type]
        assert seed_episodes(conn, loaded) == 0  # type: ignore[arg-type]
        seeded = episodic.for_run(conn, f"{SEED_PREFIX}memory-001-spelling")
        assert len(seeded) == 5 and {e.run_id for e in seeded} == {"seed:memory-001-spelling"}
        episodic.put(conn, "run_real", "loop", "an operator's own session")
        with pytest.raises(SuiteLoadError, match="scratch HARNESSED_HOME"):
            seed_episodes(conn, loaded)  # type: ignore[arg-type]
        assert len(episodic.for_run(conn, "seed:memory-001-spelling")) == 5
    finally:
        conn.close()


def test_a_memory_case_without_episodes_is_a_named_load_error(tmp_path: Path) -> None:
    path = write_suite(
        tmp_path,
        "suite: sample\nkind: memory\ncases:\n  - id: memory-009-x\n    prompt: hi\n"
        "    graders:\n      - name: contains_all\n        expect: {values: [a]}\n",
    )
    with pytest.raises(SuiteLoadError, match="episodes"):
        load_suite(path)


def test_consolidation_raises_the_memory_score_strictly_offline(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    """The ticket's proof: score, consolidate by hand, score again, in a scratch home."""
    import json

    from typer.testing import CliRunner

    from harnessed.cli.app import app

    cli = CliRunner()

    def score() -> float:
        result = cli.invoke(app, ["eval", "run", "memory", "--fresh", "--json"])
        assert result.exit_code == 0, result.output
        return float(json.loads(result.output)["score"])

    before = score()
    consolidated = cli.invoke(app, ["memory", "consolidate", "--json"])
    assert consolidated.exit_code == 0, consolidated.output
    assert json.loads(consolidated.output)["status"] == "done"
    after = score()
    assert (before, after) == (0.0, 1.0)
    assert after > before


def test_a_name_in_both_the_shipped_corpus_and_the_home_is_refused_at_load(
    tmp_path: Path,
) -> None:
    shipped_dir, home_dir = tmp_path / "shipped", tmp_path / "home"
    for each in (shipped_dir, home_dir):
        each.mkdir()
        (each / "smoke.yaml").write_bytes(SMOKE_PATH.read_bytes())
    (home_dir / "mine.yaml").write_bytes(
        SMOKE_PATH.read_bytes().replace(b"suite: smoke", b"suite: mine")
    )

    with pytest.raises(SuiteLoadError, match="the recorded suite smoke would shadow a shipped one"):
        load_named_suite("smoke", (shipped_dir, home_dir))
    assert load_named_suite("mine", (shipped_dir, home_dir)).path == home_dir / "mine.yaml"
    assert "mine" in str(_missing(shipped_dir, home_dir))


def _missing(*roots: Path) -> SuiteLoadError:
    try:
        load_named_suite("nope", roots)
    except SuiteLoadError as exc:
        return exc
    raise AssertionError("expected a SuiteLoadError")
