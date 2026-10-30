"""THE CRUX. FakeProvider selects its fixture on what the system prompt actually says.

If a fixed fixture were replayed per case, appending a procedure to the system prompt could not
change the output, the score could only rise because somebody staged it, and FR-25 would be
theatre. These tests assert the property that makes the proof honest and the boundary that keeps
it honest: selection sees the prompt and nothing the system computed about its own performance.

``test_prompt_conditional`` is the positive claim; ``test_predicates_cannot_see_outcomes`` is the
security boundary. The rest pin the vocabulary, the ordering rule and determinism.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from harnessed import shipped
from harnessed.config import Settings
from harnessed.errors import FixtureCorpusError, FixtureMissError
from harnessed.providers.fake import PREDICATE_NAMES, FakeProvider
from harnessed.providers.types import ChatRequest, ChatResponse, Message

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = shipped.fake_wire()
FAKE_SOURCE = REPO_ROOT / "src" / "harnessed" / "providers" / "fake.py"

CASE_SCOPE = "smoke-003-json-only"
REFLECT_SCOPE = "_reflect"

#: The exact vocabulary architecture.md permits. Widening it is a gate decision (MUST rule 2),
#: so this literal is the test's own copy rather than a mirror of the thing under test.
PERMITTED_PREDICATES = frozenset(
    {"always", "context_contains_all", "system_contains_all", "system_matches"}
)

#: The procedure text the reflector authors, and the only text that flips the case scope's
#: variant. It carries the two traits that scope's ``_index.yaml`` declares.
PROCEDURE = (
    "When the task asks for structured data, respond with a single JSON object "
    "and no prose before or after it."
)

BASE_SYSTEM = "You are a careful assistant."


# ---- helpers -----------------------------------------------------------------------------------


def settings() -> Settings:
    """A keyless offline ``Settings``, built without reading the process environment."""
    return Settings(
        home=REPO_ROOT / ".harnessed",
        provider="fake",
        model="fake-1",
        small_model="fake-1",
        base_url=None,
        embed_base_url=None,
        embed_model=None,
        retrieval_token_budget=4000,
        max_iterations=12,
    )


def request(scope: str, system: str = "", *, assistant_turns: int = 0) -> ChatRequest:
    """A normalized request whose whole selection key is visible in this call."""
    messages: list[Message] = [Message(role="user", content="Which city?")]
    for turn in range(assistant_turns):
        messages.append(Message(role="assistant", content=f"turn {turn + 1}"))
        messages.append(Message(role="user", content="and then?"))
    return ChatRequest(
        model="fake-1", messages=tuple(messages), system=system, metadata={"scope": scope}
    )


def canonical(response: ChatResponse) -> str:
    """The one byte-level projection of a response. Same key -> same string, always."""
    return json.dumps(dataclasses.asdict(response), sort_keys=True, ensure_ascii=False)


RESPONSE = '{{"response_id": "{rid}", "model": "fake-1", "content": "{rid}"}}'


def write_corpus(root: Path, scope: str, index: str, *files: tuple[str, str]) -> Path:
    """Author a throwaway corpus for the shapes the committed corpus must never hold."""
    directory = root / scope
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "_index.yaml").write_text(index, encoding="utf-8")
    for name, body in files or (("base.json", RESPONSE.format(rid="base")),):
        (directory / name).write_text(body, encoding="utf-8")
    return directory


def committed_indexes() -> list[Path]:
    """Every ``_index.yaml`` in the committed corpus. Never empty, or the scan proves nothing."""
    indexes = sorted(CORPUS.glob("*/_index.yaml"))
    assert indexes, f"no committed index under {CORPUS}"
    return indexes


def declared_predicates(index: Path) -> list[tuple[str, str, Any]]:
    """``(variant name, predicate, declared value)`` for every variant of one index."""
    document = yaml.safe_load(index.read_text(encoding="utf-8"))
    declared = []
    for entry in document["variants"]:
        when = entry["when"]
        if isinstance(when, str):
            declared.append((entry["name"], when, None))
        else:
            predicate, value = next(iter(when.items()))
            declared.append((entry["name"], predicate, value))
    return declared


def _strings(value: Any) -> list[str]:
    """Every string a declared predicate value carries, flattened."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for entry in value for item in _strings(entry)]
    return [repr(value)]


# ---- the crux ------------------------------------------------------------------------------


def test_prompt_conditional() -> None:
    """Same (scope, iteration); two system prompts differing only by the procedure text."""
    provider = FakeProvider(settings())

    without = provider.complete(request(CASE_SCOPE, BASE_SYSTEM))
    with_procedure = provider.complete(request(CASE_SCOPE, f"{BASE_SYSTEM}\n\n{PROCEDURE}"))

    assert canonical(without) != canonical(with_procedure)
    assert without.content != with_procedure.content


def test_a_procedure_missing_the_traits_does_not_flip_the_variant() -> None:
    """The failure path. A candidate that only sounds right is served the base fixture."""
    provider = FakeProvider(settings())
    waffle = f"{BASE_SYSTEM}\n\nPlease try to be tidy and structured in your answers."

    assert canonical(provider.complete(request(CASE_SCOPE, waffle))) == canonical(
        provider.complete(request(CASE_SCOPE, BASE_SYSTEM))
    )


def test_only_some_of_the_traits_is_not_enough() -> None:
    provider = FakeProvider(settings())
    partial = f"{BASE_SYSTEM}\n\nRespond with a single JSON object."

    assert canonical(provider.complete(request(CASE_SCOPE, partial))) == canonical(
        provider.complete(request(CASE_SCOPE, BASE_SYSTEM))
    )


def test_variant_and_iteration_are_independent_axes_of_the_key() -> None:
    """The variant is chosen per request, so a matched variant can still miss its iteration."""
    provider = FakeProvider(settings())
    with pytest.raises(FixtureMissError) as miss:
        provider.complete(request(CASE_SCOPE, f"{BASE_SYSTEM}\n\n{PROCEDURE}", assistant_turns=1))
    assert "with_procedure.2.json" in str(miss.value)


# ---- the security boundary -------------------------------------------------------------------


def test_predicates_cannot_see_outcomes() -> None:
    """AST scan of the module, schema scan of the corpus. MUST rule 2, asserted twice over."""
    tree = ast.parse(FAKE_SOURCE.read_text(encoding="utf-8"), filename=str(FAKE_SOURCE))
    forbidden = ("evals", "improve", "memory")
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)

    offenders = sorted(
        name for name in imported for package in forbidden if package in name.split(".")
    )
    assert offenders == [], f"providers/fake.py imports {offenders}; MUST rule 2 forbids it"

    # A predicate may not name anything the system computed about its own performance.
    outcome_words = (
        "candidate",
        "procedure_id",
        "score",
        "passed",
        "failed",
        "regression",
        "promotion",
        "run_id",
        "improvement",
    )
    for index in committed_indexes():
        for name, predicate, value in declared_predicates(index):
            assert predicate in PERMITTED_PREDICATES, (
                f"{index}: variant {name!r} declares {predicate!r}, outside the permitted "
                f"vocabulary {sorted(PERMITTED_PREDICATES)}"
            )
            declared = " ".join([predicate, *_strings(value)]).casefold()
            for word in outcome_words:
                assert word not in declared, (
                    f"{index}: variant {name!r} keys its predicate on {word!r}, which is an "
                    f"outcome the system computed. That is the staged proof, not this one."
                )


def test_the_engine_declares_exactly_the_permitted_vocabulary() -> None:
    assert set(PREDICATE_NAMES) == PERMITTED_PREDICATES


@pytest.mark.parametrize(
    "when",
    [
        "candidate_id: cand_ab12",
        "eval_score: 0.8",
        "case_failed: true",
        "improvement_run_id: imp_1",
    ],
)
def test_an_unknown_predicate_is_a_load_time_error_naming_file_and_predicate(
    tmp_path: Path, when: str
) -> None:
    key = when.split(":")[0]
    write_corpus(
        tmp_path,
        "staged",
        f"scope: staged\nvariants:\n  - name: good\n    when:\n      {when}\n"
        "  - name: base\n    when: always\n",
    )
    with pytest.raises(FixtureCorpusError) as raised:
        FakeProvider(settings(), root=tmp_path)
    message = str(raised.value)
    assert str(tmp_path / "staged" / "_index.yaml") in message
    assert key in message


# ---- ordering ------------------------------------------------------------------------------


def test_the_first_matching_variant_wins_in_declared_order(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        "ordered",
        "scope: ordered\nvariants:\n"
        "  - name: first\n    when:\n      system_contains_all: ['alpha']\n"
        "  - name: second\n    when:\n      system_contains_all: ['alpha']\n"
        "  - name: base\n    when: always\n",
        ("first.json", RESPONSE.format(rid="first")),
        ("second.json", RESPONSE.format(rid="second")),
        ("base.json", RESPONSE.format(rid="base")),
    )
    provider = FakeProvider(settings(), root=tmp_path)
    assert provider.complete(request("ordered", "alpha")).response_id == "first"


def test_every_committed_scope_declares_base_last_with_an_always_predicate() -> None:
    for index in committed_indexes():
        declared = declared_predicates(index)
        names = [name for name, _, _ in declared]
        assert names[-1] == "base", f"{index}: base must be declared last, got {names}"
        assert declared[-1][1] == "always", f"{index}: the base variant's predicate must be always"


def test_a_base_that_is_not_last_is_a_load_time_error(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        "misordered",
        "scope: misordered\nvariants:\n  - name: base\n    when: always\n"
        "  - name: late\n    when:\n      system_contains_all: ['x']\n",
    )
    with pytest.raises(FixtureCorpusError, match="last"):
        FakeProvider(settings(), root=tmp_path)


# ---- system_contains_all ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "system",
    [
        "RESPOND WITH A SINGLE JSON OBJECT and NO PROSE BEFORE OR AFTER",
        "respond   with a\tsingle JSON\n\nobject, so there is no prose  before or after it",
        "no prose before or after; respond with a single JSON object",
    ],
)
def test_the_system_prompt_is_whitespace_normalized_and_case_folded(system: str) -> None:
    provider = FakeProvider(settings())
    assert provider.complete(request(CASE_SCOPE, system)).response_id.endswith("with_procedure")


def test_a_declared_trait_is_normalized_the_same_way(tmp_path: Path) -> None:
    """Both sides of the comparison get the same treatment, so authoring stays forgiving."""
    write_corpus(
        tmp_path,
        "norm",
        "scope: norm\nvariants:\n"
        '  - name: hit\n    when:\n      system_contains_all:\n        - "  Single   JSON "\n'
        "  - name: base\n    when: always\n",
        ("hit.json", RESPONSE.format(rid="hit")),
        ("base.json", RESPONSE.format(rid="base")),
    )
    provider = FakeProvider(settings(), root=tmp_path)
    assert provider.complete(request("norm", "a single json object")).response_id == "hit"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("'not a list'", "list"),
        ("[]", "non-empty"),
        ("[1]", "string"),
        ("['']", "non-empty"),
    ],
)
def test_a_malformed_contains_all_value_is_a_load_time_error(
    tmp_path: Path, value: str, expected: str
) -> None:
    write_corpus(
        tmp_path,
        "bad",
        f"scope: bad\nvariants:\n  - name: hit\n    when:\n      system_contains_all: {value}\n"
        "  - name: base\n    when: always\n",
    )
    with pytest.raises(FixtureCorpusError, match=expected):
        FakeProvider(settings(), root=tmp_path)


# ---- system_matches --------------------------------------------------------------------------


def test_system_matches_selects_on_the_normalized_prompt(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        "regex",
        "scope: regex\nvariants:\n"
        "  - name: hit\n    when:\n      system_matches: 'step [0-9]+ of [0-9]+'\n"
        "  - name: base\n    when: always\n",
        ("hit.json", RESPONSE.format(rid="hit")),
        ("base.json", RESPONSE.format(rid="base")),
    )
    provider = FakeProvider(settings(), root=tmp_path)
    assert provider.complete(request("regex", "Step 2\nof 7")).response_id == "hit"
    assert provider.complete(request("regex", "no digits here")).response_id == "base"


@pytest.mark.parametrize(
    ("value", "expected"), [("'([unclosed'", "regular expression"), ("7", "string")]
)
def test_a_malformed_regex_is_a_load_time_error(tmp_path: Path, value: str, expected: str) -> None:
    write_corpus(
        tmp_path,
        "badregex",
        f"scope: badregex\nvariants:\n  - name: hit\n    when:\n      system_matches: {value}\n"
        "  - name: base\n    when: always\n",
    )
    with pytest.raises(FixtureCorpusError, match=expected):
        FakeProvider(settings(), root=tmp_path)


# ---- determinism ---------------------------------------------------------------------------


def test_the_same_store_fixtures_and_prompt_return_the_same_bytes() -> None:
    system = f"{BASE_SYSTEM}\n\n{PROCEDURE}"
    first = FakeProvider(settings()).complete(request(CASE_SCOPE, system))
    second = FakeProvider(settings()).complete(request(CASE_SCOPE, system))
    assert canonical(first) == canonical(second)


def test_the_reflect_scope_gates_its_candidate_on_an_observed_failure() -> None:
    """No failing case id in the prompt, no candidate. The authored text is not free."""
    provider = FakeProvider(settings())
    observed = provider.complete(
        request(REFLECT_SCOPE, "These cases did not pass the baseline run: smoke-003-json-only.")
    )
    nothing = provider.complete(
        request(REFLECT_SCOPE, "These cases did not pass the baseline run: none.")
    )

    assert nothing.content == ""
    assert observed.content != ""
    # The authored candidate is the text that actually flips the case scope's variant.
    assert canonical(
        provider.complete(request(CASE_SCOPE, f"{BASE_SYSTEM}\n\n{observed.content}"))
    ) != canonical(provider.complete(request(CASE_SCOPE, BASE_SYSTEM)))


def test_the_consolidate_scope_is_keyed_like_the_gates_and_declares_one_base_variant() -> None:
    """Ticket 247 (decision 81): an ordinary scope, no predicate beyond ``always`` yet."""
    declared = yaml.safe_load((CORPUS / "_consolidate" / "_index.yaml").read_text(encoding="utf-8"))
    assert declared["scope"] == "_consolidate"
    assert declared["variants"] == [{"name": "base", "when": "always"}]
    body = json.loads((CORPUS / "_consolidate" / "base.json").read_text(encoding="utf-8"))
    assert json.loads(body["content"]) == {
        "facts": [
            {
                "text": "The operator prefers British spelling.",
                "actor": "operator",
                "path": None,
                "kind": "preference",
                "relation": "new",
                "replaces": None,
            }
        ]
    }
    served = FakeProvider(settings()).complete(request("_consolidate", "any system prompt"))
    assert served.response_id == body["response_id"]
