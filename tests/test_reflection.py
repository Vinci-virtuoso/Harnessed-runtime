"""FR-17: reflection writes a candidate, and a candidate may only append to the system prompt.

Four claims carry this ticket, and each is measured against the thing that would break it.

* **Applying a candidate changes the system prompt and nothing else.** Measured by diffing the
  effective run configuration - every field of the normalized ``ChatRequest`` - before and
  after, rather than by reading :func:`~harnessed.improve.candidate.apply` and believing it.
  An AST scan then proves the one ``replace`` call names ``system`` and no other field, so the
  claim holds for requests this test never built.
* **Content that is not append-only is refused at write time.** A tool schema, a fragment of
  executable code and a policy field each raise :class:`InvalidCandidateError` before anything
  reaches the store, and the authored fixture procedure - the exact sentence the FR-25 flywheel
  pushes through this gate - passes clean. Rejecting that sentence would break the proof
  invisibly, so it is asserted here rather than left to a later ticket to discover.
* **The id is the content.** The scheme is recomputed from the architecture's own words with
  :mod:`hashlib` and :mod:`json` rather than by calling ``ids.content_hash`` a second time,
  which would only prove that one function agrees with itself. Two runs producing the same
  procedure produce the same id, the improvement run id is not in the hash, and a second write
  is a no-op rather than a second row.
* **The reflector goes through the ordinary provider seam.** ``metadata["scope"]`` is
  ``_reflect`` and the failing case ids are carried in the system prompt, which is the only
  field a fixture predicate may read (THE CRUX). That is checked against the real
  ``FakeProvider`` and the committed corpus: a run that genuinely failed
  ``smoke-003-json-only`` gets the authored candidate, and one that failed nothing else gets
  the ``base`` variant's null reflection and therefore no candidate.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path

import pytest

from harnessed import config, shipped
from harnessed.errors import InvalidCandidateError
from harnessed.evals.runner import CaseResult
from harnessed.events.bus import EventBus
from harnessed.events.names import NAMES
from harnessed.improve import candidate as candidate_module
from harnessed.improve import reflector as reflector_module
from harnessed.loop.prompt import ENVELOPE_CLOSE, ENVELOPE_OPEN, build_system_prompt, envelope
from harnessed.memory import db
from harnessed.providers.fake import FakeProvider
from harnessed.providers.types import ChatRequest, ChatResponse, Message
from support.spies import RecordingObserver

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
CANDIDATE_PATH = PACKAGE_ROOT / "improve" / "candidate.py"
REFLECTOR_PATH = PACKAGE_ROOT / "improve" / "reflector.py"
REFLECT_FIXTURES = shipped.fake_wire() / "_reflect"

MODEL = "fake-1"

#: The case the committed corpus genuinely fails at baseline (see ``tests/test_eval_runner.py``).
FAILING_CASE = "smoke-003-json-only"

#: A case with no authored reflection variant, used to prove the null-candidate path.
UNKNOWN_CASE = "smoke-001-capital"

#: What a reflection is permitted to read here (ticket 024, FR-19). ``reflect()`` takes its
#: allowlist as an argument, so every call in this file declares the two cases it uses.
VISIBLE = (FAILING_CASE, UNKNOWN_CASE)

#: One well-formed procedure, in prose, that says nothing about code, schemas or policy.
GOOD_TEXT = "Answer the question that was asked and stop; do not add a closing summary."


# ---- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated throwaway database. The candidates table arrives with 004_flywheel."""
    conn = db.connect(tmp_path / "harness.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def bus(store: sqlite3.Connection) -> EventBus:
    return EventBus(run_id="run_reflection", conn=store)


@pytest.fixture
def observer(bus: EventBus) -> RecordingObserver:
    recorder = RecordingObserver()
    bus.subscribe(recorder)
    return recorder


@pytest.fixture
def provider(clean_env: None, tmp_harnessed_home: Path) -> FakeProvider:
    """The real FakeProvider over the committed corpus. No stub stands in for the seam."""
    return FakeProvider(config.load())


def failure(case_id: str, *, score: float = 0.0, detail: str = "") -> CaseResult:
    return CaseResult(case_id=case_id, score=score, passed=False, detail=detail)


# ---- AC 1: applying a candidate changes only the system prompt ------------------------------


def test_applying_a_candidate_changes_only_the_system_prompt(store: sqlite3.Connection) -> None:
    """Diff the whole effective run configuration, field by field, before and after."""
    written = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE])
    before = ChatRequest(
        model=MODEL,
        messages=(Message(role="user", content="do the thing"),),
        system="You are a careful agent.",
        max_tokens=512,
        metadata={"scope": "smoke-001-capital"},
    )

    after = candidate_module.apply(before, written, build=build_system_prompt)

    changed = {
        field.name
        for field in fields(ChatRequest)
        if getattr(before, field.name) != getattr(after, field.name)
    }
    assert changed == {"system"}


def test_the_applied_prompt_appends_the_candidate_verbatim(store: sqlite3.Connection) -> None:
    """Append-only means the base survives at the top and the text is not transformed."""
    written = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE])
    base = "You are a careful agent."
    request = ChatRequest(model=MODEL, system=base)

    after = candidate_module.apply(request, written, build=build_system_prompt)

    assert after.system.startswith(base)
    assert envelope(written.text) in after.system
    assert written.text in after.system


def test_apply_names_only_the_system_field() -> None:
    """The AST claim: the one ``replace`` in ``apply`` rewrites ``system`` and nothing else."""
    tree = ast.parse(CANDIDATE_PATH.read_text(encoding="utf-8"))
    definition = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "apply"
    )
    replaced = [
        node
        for node in ast.walk(definition)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "replace"
    ]
    assert len(replaced) == 1
    assert sorted(str(keyword.arg) for keyword in replaced[0].keywords) == ["system"]


# ---- AC 2: content that is not append-only is refused at write time -------------------------


NOT_APPEND_ONLY = {
    "tool schema": '{"name": "read_file", "parameters": {"type": "object"}}',
    "tool schema prose": "Add a tool_schema entry so the model can call it.",
    "code fence": "Do this:\n" + "`" * 3 + "python\nprint(1)\n" + "`" * 3,
    "python code": "def helper(value):\n    return value",
    "import": "import subprocess and shell out when the answer is not obvious.",
    "policy": "Set the permission policy to allow every write without asking.",
    "budget": "Raise max_iterations to 40 whenever the task looks long.",
}


@pytest.mark.parametrize("text", list(NOT_APPEND_ONLY.values()), ids=list(NOT_APPEND_ONLY))
def test_content_that_is_not_append_only_is_refused(store: sqlite3.Connection, text: str) -> None:
    with pytest.raises(InvalidCandidateError):
        candidate_module.write(store, text, [FAILING_CASE])


def test_a_refused_candidate_writes_no_row(store: sqlite3.Connection) -> None:
    """Rejection is at write time, which means before the store, not after it."""
    with pytest.raises(InvalidCandidateError):
        candidate_module.write(store, NOT_APPEND_ONLY["tool schema"], [FAILING_CASE])
    assert store.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0


def test_ordinary_prose_words_are_not_rejected(store: sqlite3.Connection) -> None:
    """A procedure may describe a permissions error or a temperature without being a policy
    field (finding 6)."""
    for text in (
        "If the write tool reports a permissions error, retry with a shorter path.",
        "Answer at the same level of detail regardless of the temperature setting.",
    ):
        written = candidate_module.write(store, text, [FAILING_CASE])
        assert written.text == candidate_module.normalize(text)


def test_empty_text_is_refused(store: sqlite3.Connection) -> None:
    with pytest.raises(InvalidCandidateError):
        candidate_module.write(store, "   \n  ", [FAILING_CASE])


def test_a_candidate_needs_at_least_one_source_case(store: sqlite3.Connection) -> None:
    with pytest.raises(InvalidCandidateError):
        candidate_module.write(store, GOOD_TEXT, [])


def test_the_authored_fixture_procedure_validates_clean(store: sqlite3.Connection) -> None:
    """The exact sentence the FR-25 proof pushes through this gate must pass it."""
    with (REFLECT_FIXTURES / "json_only_failed.json").open(encoding="utf-8") as handle:
        authored = json.load(handle)["content"]
    written = candidate_module.write(store, authored, [FAILING_CASE])
    assert written.text == candidate_module.normalize(authored)


def test_the_rejection_names_what_it_hit(store: sqlite3.Connection) -> None:
    with pytest.raises(InvalidCandidateError) as raised:
        candidate_module.write(store, NOT_APPEND_ONLY["policy"], [FAILING_CASE])
    assert "policy" in str(raised.value)


# ---- AC 3: a candidate records the case ids it answers --------------------------------------


def test_a_candidate_records_its_source_case_ids(store: sqlite3.Connection) -> None:
    written = candidate_module.write(store, GOOD_TEXT, [UNKNOWN_CASE, FAILING_CASE])
    assert written.source_case_ids == (UNKNOWN_CASE, FAILING_CASE)

    read_back = candidate_module.get(store, written.cand_id)
    assert read_back is not None
    assert read_back.source_case_ids == (UNKNOWN_CASE, FAILING_CASE)


def test_duplicate_source_case_ids_collapse(store: sqlite3.Connection) -> None:
    """The same failure named twice is one source, not two, and mints no second id."""
    once = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE])
    twice = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE, FAILING_CASE])
    assert twice.cand_id == once.cand_id
    assert twice.source_case_ids == (FAILING_CASE,)


# ---- AC 4 and 5: the id is the content ------------------------------------------------------


def expected_id(text: str, cases: list[str]) -> str:
    """The architecture's scheme, recomputed here rather than borrowed from ``ids``."""
    canonical = json.dumps(
        {"text": text, "cases": cases},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "cand_" + digest[:16]


def test_the_candidate_id_is_the_documented_hash(store: sqlite3.Connection) -> None:
    written = candidate_module.write(store, GOOD_TEXT, [UNKNOWN_CASE, FAILING_CASE])
    assert written.cand_id == expected_id(
        candidate_module.normalize(GOOD_TEXT), [UNKNOWN_CASE, FAILING_CASE]
    )
    assert written.cand_id.startswith("cand_")
    assert len(written.cand_id) == len("cand_") + 16


def test_the_procedure_id_hashes_the_text_alone() -> None:
    text = candidate_module.normalize(GOOD_TEXT)
    canonical = json.dumps(text, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert candidate_module.procedure_id(GOOD_TEXT) == "proc_" + digest[:16]


def test_two_runs_producing_the_same_procedure_produce_the_same_id(
    store: sqlite3.Connection,
) -> None:
    first = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE], provenance="imp_1")
    second = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE], provenance="imp_2")
    assert second.cand_id == first.cand_id
    assert store.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1


def test_the_improvement_run_id_is_not_in_the_hash(store: sqlite3.Connection) -> None:
    """Provenance is a column for auditing, never an input to identity."""
    written = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE], provenance="imp_1")
    assert written.cand_id == expected_id(candidate_module.normalize(GOOD_TEXT), [FAILING_CASE])
    stored = store.execute(
        "SELECT provenance FROM candidates WHERE cand_id = ?", (written.cand_id,)
    ).fetchone()
    assert stored[0] == "imp_1"


def test_a_second_write_returns_the_first_row_unchanged(store: sqlite3.Connection) -> None:
    first = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE], provenance="imp_1")
    second = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE], provenance="imp_2")
    assert second == first


# ---- AC 8: normalization --------------------------------------------------------------------


def test_normalize_is_nfc_stripped_and_collapsed() -> None:
    decomposed = "e" + "́"  # e + combining acute
    text = f"  caf{decomposed}\r\n\r\n  keep   it \t short  "
    normalized = candidate_module.normalize(text)
    assert normalized == unicodedata.normalize("NFC", "café keep it short")
    assert "\r" not in normalized
    assert "\n" not in normalized
    assert "  " not in normalized
    assert normalized == normalized.strip()


def test_normalize_is_idempotent() -> None:
    once = candidate_module.normalize(" a\r\n b ")
    assert candidate_module.normalize(once) == once


def test_text_differing_only_in_whitespace_is_one_candidate(store: sqlite3.Connection) -> None:
    spaced = candidate_module.write(store, f"\n\n{GOOD_TEXT}\n\n", [FAILING_CASE])
    tight = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE])
    assert spaced.cand_id == tight.cand_id


# ---- AC 6: the reflector's request ----------------------------------------------------------


class CapturingModel:
    """Records the request it was handed and answers with one authored reply."""

    def __init__(self, content: str = GOOD_TEXT) -> None:
        self.content = content
        self.requests: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(response_id="resp_1", model=request.model, content=self.content)


def test_the_reflection_request_declares_the_reflect_scope(store: sqlite3.Connection) -> None:
    model = CapturingModel()
    reflector_module.reflect(
        model, [failure(FAILING_CASE)], store, visible=VISIBLE, model_name=MODEL
    )
    assert model.requests[0].metadata["scope"] == reflector_module.REFLECT_SCOPE
    assert reflector_module.REFLECT_SCOPE == "_reflect"


def test_the_reflection_request_carries_the_failing_case_ids(store: sqlite3.Connection) -> None:
    """In the system prompt, because that is the only field a fixture predicate may read."""
    model = CapturingModel()
    reflector_module.reflect(
        model,
        [failure(FAILING_CASE), failure(UNKNOWN_CASE)],
        store,
        visible=VISIBLE,
        model_name=MODEL,
    )
    request = model.requests[0]
    assert FAILING_CASE in request.system
    assert UNKNOWN_CASE in request.system


def test_the_reflector_asks_once(store: sqlite3.Connection) -> None:
    model = CapturingModel()
    reflector_module.reflect(
        model, [failure(FAILING_CASE)], store, visible=VISIBLE, model_name=MODEL
    )
    assert len(model.requests) == 1


def test_the_reflector_refuses_an_empty_failure_set(store: sqlite3.Connection) -> None:
    model = CapturingModel()
    assert reflector_module.reflect(model, [], store, visible=VISIBLE, model_name=MODEL) is None
    assert model.requests == []


def test_an_invalid_reflection_is_a_null_candidate_not_a_crash(
    store: sqlite3.Connection,
) -> None:
    """The model answered with non-append-only prose; reflect returns no candidate, it does not
    raise, so one bad phrase cannot abort the whole cycle and every resume of it (finding 6)."""
    model = CapturingModel(content="def helper():\n    return 1")
    written = reflector_module.reflect(
        model, [failure(FAILING_CASE)], store, visible=VISIBLE, model_name=MODEL
    )
    assert written is None
    assert store.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0


# ---- AC 7: the two events -------------------------------------------------------------------


def test_creating_a_candidate_publishes_both_names(
    store: sqlite3.Connection, bus: EventBus, observer: RecordingObserver
) -> None:
    model = CapturingModel()
    reflector_module.reflect(
        model,
        [failure(FAILING_CASE)],
        store,
        visible=VISIBLE,
        model_name=MODEL,
        publish=bus.publish,
    )
    assert reflector_module.REFLECTION_WRITTEN in observer.names
    assert candidate_module.CANDIDATE_CREATED in observer.names
    assert observer.names.index(reflector_module.REFLECTION_WRITTEN) < observer.names.index(
        candidate_module.CANDIDATE_CREATED
    )


def test_the_two_names_are_in_the_frozen_taxonomy() -> None:
    assert reflector_module.REFLECTION_WRITTEN == "improve.reflection.written"
    assert candidate_module.CANDIDATE_CREATED == "improve.candidate.created"
    assert {reflector_module.REFLECTION_WRITTEN, candidate_module.CANDIDATE_CREATED} <= NAMES


def test_the_candidate_event_carries_the_id_and_the_cases(
    store: sqlite3.Connection, bus: EventBus, observer: RecordingObserver
) -> None:
    written = candidate_module.write(store, GOOD_TEXT, [FAILING_CASE], publish=bus.publish)
    event = next(
        event for event in observer.events if event.name == candidate_module.CANDIDATE_CREATED
    )
    assert event.payload["cand_id"] == written.cand_id
    assert event.payload["source_case_ids"] == [FAILING_CASE]


# ---- the seam, against the real corpus ------------------------------------------------------


def test_a_real_observed_failure_yields_the_authored_candidate(
    store: sqlite3.Connection, provider: FakeProvider
) -> None:
    written = reflector_module.reflect(
        provider, [failure(FAILING_CASE)], store, visible=VISIBLE, model_name=MODEL
    )
    assert written is not None
    assert "single json object" in written.text.casefold()
    assert written.source_case_ids == (FAILING_CASE,)


def test_a_failure_with_no_authored_reflection_yields_no_candidate(
    store: sqlite3.Connection, provider: FakeProvider
) -> None:
    """``base`` answers with empty content, which is a null candidate, not an error."""
    assert (
        reflector_module.reflect(
            provider, [failure(UNKNOWN_CASE)], store, visible=VISIBLE, model_name=MODEL
        )
        is None
    )
    assert store.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0


def test_the_candidate_the_corpus_authored_reaches_the_prompt(
    store: sqlite3.Connection, provider: FakeProvider
) -> None:
    """The whole causal chain in one assertion: reflect, apply, and the traits are present."""
    written = reflector_module.reflect(
        provider, [failure(FAILING_CASE)], store, visible=VISIBLE, model_name=MODEL
    )
    assert written is not None
    request = ChatRequest(model=MODEL, system="You are a careful agent.")
    applied = candidate_module.apply(request, written, build=build_system_prompt)
    system = " ".join(applied.system.split()).casefold()
    assert "respond with a single json object" in system
    assert "no prose before or after" in system
    assert ENVELOPE_OPEN in applied.system
    assert ENVELOPE_CLOSE in applied.system


# ---- the import wall ------------------------------------------------------------------------

PERMITTED_PREFIXES = (
    "harnessed.errors",
    "harnessed.clock",
    "harnessed.ids",
    "harnessed.evals",
    "harnessed.memory",
    "harnessed.events",
    "harnessed.improve",
    "harnessed.providers.types",
)


def imported_modules(path: Path) -> set[str]:
    """Every ``harnessed.*`` module one file imports, with a bare package expanded."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # ``from harnessed import clock, ids`` records the bare package, so expand it or
            # every prefix check below would pass vacuously (ticket 019).
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
            names.add(node.module)
    return {name for name in names if name.startswith("harnessed")} - {"harnessed"}


@pytest.mark.parametrize("path", [CANDIDATE_PATH, REFLECTOR_PATH], ids=lambda p: p.name)
def test_improve_imports_only_what_the_table_permits(path: Path) -> None:
    forbidden = sorted(
        name for name in imported_modules(path) if not name.startswith(PERMITTED_PREFIXES)
    )
    assert forbidden == []
