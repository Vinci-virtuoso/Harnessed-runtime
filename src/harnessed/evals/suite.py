"""Load a YAML eval suite into ordered cases with stable, authored case ids.

FR-14. A suite is a YAML document naming itself and listing cases in the order they run. Each
case carries the prompt the runner issues and the graders that score the answer.

**Case ids are authored, never derived from list position.** That single decision is what makes
the id load-bearing in the two directions the ticket names:

- ``eval_results`` keys on ``(eval_run_id, case_id, attempt)``. An id derived from position would
  make reordering a suite look, in the ledger, like every case after the moved one changed
  identity, and every before/after comparison the promoter makes would be comparing strangers.
- FakeProvider keys on ``metadata["scope"]``, which the runner sets to the case id
  (``architecture.md``, THE CRUX). The id is therefore also a directory name under
  ``corpus/wire/fake/``, which is why its shape is validated here: a case id may not
  nest, walk or hide, so joining it onto the corpus root stays a join.

Validation is eager and total. A malformed suite raises :class:`SuiteLoadError` naming the file
and the offending field, so a typo fails at load rather than halfway through a scored run.

Graders are *named* here and implemented in ``graders.py`` (ticket 017). This module holds the
vocabulary as :data:`GRADER_NAMES` rather than importing the implementations: the loader's job is
to reject a name nothing will resolve, and it can do that from the names alone.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# No stub package is pinned by architecture.md, and adding one is a gate decision (MUST rule 9),
# so the untyped import is acknowledged here rather than depended on.
import yaml  # type: ignore[import-untyped]

from harnessed import config, shipped
from harnessed.errors import SuiteLoadError

__all__ = [
    "GRADER_NAMES",
    "GROQ_HANDLERS",
    "KIND_MEMORY",
    "KIND_WORK",
    "SEED_PREFIX",
    "SUITE_SUFFIX",
    "VERDICT_PENDING",
    "VERDICT_TRAITS",
    "VERDICTS",
    "Case",
    "GraderSpec",
    "Suite",
    "load_named_suite",
    "listing",
    "listing_payload",
    "load_suite",
    "refuse_shadow",
    "seed_episodes",
    "home_fixtures_dir",
    "home_suites_dir",
    "suite_roots",
]


SUITE_SUFFIX: Final = ".yaml"

#: The grader vocabulary a suite may name. Ticket 017 implements exactly these in ``graders.py``;
#: a suite naming anything else fails to load. Kept here rather than imported from ``graders.py``
#: so that rejecting an unknown name needs the names and nothing else - and so that adding a
#: grader is a deliberate two-file act rather than a side effect of defining a function.
GRADER_NAMES: Final[tuple[str, ...]] = (
    "contains_all",
    "exact_match",
    "json_object",
    "regex_match",
    "tool_called",
    "verdict",
)

#: A case id is a fixture directory name. Letters, digits, hyphen and underscore only: no dot, no
#: separator, no leading punctuation, so ``corpus_root / case.id`` can never leave the corpus.
CASE_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

SUITE_FIELDS: Final = frozenset({"suite", "description", "cases", "kind"})
CASE_FIELDS: Final = frozenset({"id", "prompt", "graders"})
#: A memory case also carries the recorded episodes it is seeded with (ticket 249, FR-152).
MEMORY_CASE_FIELDS: Final = CASE_FIELDS | {"episodes"}

#: A work suite (ticket 235, FR-136) is recorded from real sessions, never authored: its cases
#: carry the operator's input, the reply as served and his own verdict, and no graders.
KIND_WORK: Final = "work"
KIND_AUTHORED: Final = "smoke"
KIND_MEMORY: Final = "memory"
#: A seeded episode's ``run_id``, its source: this prefix plus the case id (ticket 249).
SEED_PREFIX: Final = "seed:"
KINDS: Final = (KIND_WORK, KIND_AUTHORED, "holdout", "routing", "memory")
WORK_CASE_FIELDS: Final = frozenset(
    {"id", "input", "handler", "reference", "verdict", "traits", "session", "seq"}
)
VERDICT_PENDING: Final = "pending"
VERDICT_TRAITS: Final = "traits"
VERDICTS: Final = ("accept", "reject", VERDICT_PENDING, VERDICT_TRAITS)
#: The handlers whose cases carry a FakeProvider fixture (the router's own two Groq routes).
GROQ_HANDLERS: Final = ("groq_chat", "groq_memory")
GRADER_FIELDS: Final = frozenset({"name", "expect", "threshold"})

#: A grader that declares no threshold must clear its scale completely. Defaulting to anything
#: looser would make "passed" quietly depend on which fields an author remembered to write.
DEFAULT_THRESHOLD: Final = 1.0


# ---- value types ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraderSpec:
    """One grader applied to one case: its name, its expectation, and its pass threshold.

    ``expect`` is passed through to the grader unread. This module knows which names exist; what
    each one expects is ``graders.py``'s business, and duplicating that schema here would put the
    grader contract in two places.
    """

    name: str
    expect: Mapping[str, Any] = field(default_factory=dict)
    threshold: float = DEFAULT_THRESHOLD


@dataclass(frozen=True, slots=True)
class Case:
    """One scored unit: an authored id, the prompt to issue, and the graders to apply."""

    id: str
    prompt: str
    graders: tuple[GraderSpec, ...]
    #: Work-suite fields (ticket 235); empty on an authored case.
    handler: str = ""
    reference: str = ""
    verdict: str = ""
    traits: tuple[str, ...] = ()
    #: Memory-suite field (ticket 249): the recorded episodes the case's answer lives across.
    episodes: tuple[str, ...] = ()

    @property
    def pending(self) -> bool:
        """A work case still awaiting the operator's verdict. The runner skips it (ticket 236)."""
        return self.verdict == VERDICT_PENDING


@dataclass(frozen=True, slots=True)
class Suite:
    """An ordered set of cases loaded from one YAML file."""

    name: str
    path: Path
    cases: tuple[Case, ...]
    description: str = ""
    kind: str = KIND_AUTHORED

    @property
    def pending(self) -> tuple[Case, ...]:
        """The cases marked to be skipped by the runner, and counted (ticket 235)."""
        return tuple(case for case in self.cases if case.pending)

    @property
    def case_ids(self) -> tuple[str, ...]:
        """The ids in declared order. What the runner iterates and the ledger keys on."""
        return tuple(case.id for case in self.cases)

    def case(self, case_id: str) -> Case:
        """The case with this id, or a named error. Never a bare ``KeyError`` (FR-2)."""
        for case in self.cases:
            if case.id == case_id:
                return case
        raise SuiteLoadError(
            f"{self.path}: no case with id {case_id!r} in suite {self.name!r}. "
            f"Declared cases: {', '.join(self.case_ids)}."
        )


# ---- loading -------------------------------------------------------------------------------


def _error(path: Path, problem: str) -> SuiteLoadError:
    """Every rejection names the file first, so a message is actionable without a traceback."""
    return SuiteLoadError(f"{path}: {problem}")


def home_suites_dir(home: Path | None = None) -> Path:
    """Where a recorded suite lives by default: ``<home>/suites`` (decision 88, MUST rule 44)."""
    return (config.load().home if home is None else home) / "suites"


def home_fixtures_dir(home: Path | None = None) -> Path:
    """Where a recorded suite's FakeProvider fixtures live: ``<home>/fixtures/fake``."""
    return (config.load().home if home is None else home) / "fixtures" / "fake"


def suite_roots(extra: Path | None = None) -> tuple[Path, ...]:
    """The roots a reader searches, shipped first, then ``extra`` (the home when ``None``).

    A root named twice is searched once, so ``--to <the shipped corpus>`` is not a shadow of itself.
    """
    roots = (shipped.suites_dir(), home_suites_dir() if extra is None else extra)
    return tuple(dict.fromkeys(roots))


def refuse_shadow(name: str, root: Path) -> None:
    """Raise when writing ``name`` under ``root`` would shadow a shipped suite of that name."""
    shipped_file = shipped.suites_dir() / f"{name}{SUITE_SUFFIX}"
    if shipped_file.is_file() and root != shipped.suites_dir():
        raise SuiteLoadError(
            f"the recorded suite {name} would shadow a shipped one: {shipped_file}. "
            f"Choose another name, or pass --to the corpus directory."
        )


def load_named_suite(name: str, root: Path | Sequence[Path] | None = None) -> Suite:
    """Load ``<root>/<name>.yaml``. ``harnessed eval run smoke`` is spelled with this.

    With no ``root`` the shipped corpus is searched first and the home second; a name found in
    both is a :class:`~harnessed.errors.SuiteLoadError`, never a silent shadow (decision 88).
    """
    roots = suite_roots() if root is None else (root,) if isinstance(root, Path) else tuple(root)
    found = [path for path in (r / f"{name}{SUITE_SUFFIX}" for r in roots) if path.is_file()]
    if len(found) > 1:
        raise SuiteLoadError(
            f"the recorded suite {name} would shadow a shipped one: {found[1]} and {found[0]}."
        )
    if not found:
        raise SuiteLoadError(
            f"no suite named {name!r} at {roots[-1] / f'{name}{SUITE_SUFFIX}'}. Available suites: "
            f"{', '.join(_available(roots)) or 'none'}."
        )
    return load_suite(found[0])


def _available(roots: Path | Sequence[Path]) -> tuple[str, ...]:
    searched = (roots,) if isinstance(roots, Path) else tuple(roots)
    names = {
        path.stem for root in searched if root.is_dir() for path in root.glob(f"*{SUITE_SUFFIX}")
    }
    return tuple(sorted(names))


def load_suite(path: Path) -> Suite:
    """Parse and fully validate one suite file. Same file, same order, same ids, every time."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _error(path, f"could not be read: {exc}") from exc

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _error(path, f"is not valid YAML: {exc}") from exc

    if not isinstance(document, dict):
        raise _error(path, "must be a mapping with a 'suite' name and a 'cases' list")

    unknown = sorted(set(document) - SUITE_FIELDS)
    if unknown:
        raise _error(path, f"has unknown field(s) {unknown}. Known fields: {sorted(SUITE_FIELDS)}.")

    name = _read_name(path, document)
    kind = document.get("kind", KIND_AUTHORED)
    if kind not in KINDS:
        raise _error(path, f"'kind' must be one of {list(KINDS)}, got {kind!r}")
    cases = _read_cases(
        path, document.get("cases"), work=kind == KIND_WORK, memory=kind == KIND_MEMORY
    )
    description = document.get("description", "")
    if not isinstance(description, str):
        raise _error(path, "'description' must be a string when present")

    return Suite(name=name, path=path, cases=cases, description=description, kind=kind)


def _read_name(path: Path, document: Mapping[str, Any]) -> str:
    """The suite names itself and the filename names it too; disagreement is a typo."""
    if "suite" not in document:
        raise _error(path, "is missing the required field 'suite', the suite's name")
    name = document["suite"]
    if not isinstance(name, str) or not name.strip():
        raise _error(path, f"'suite' must be a non-empty string name, got {name!r}")
    if name != path.stem:
        raise _error(
            path,
            f"declares the suite name {name!r} but the file is named {path.stem!r}. "
            f"The two must agree, because a suite is referenced by both.",
        )
    return name


def _read_cases(
    path: Path, raw: object, *, work: bool = False, memory: bool = False
) -> tuple[Case, ...]:
    if raw is None:
        raise _error(path, "is missing the required field 'cases', the ordered list of cases")
    if not isinstance(raw, list):
        raise _error(path, f"'cases' must be a list, got {type(raw).__name__}")
    if not raw:
        raise _error(path, "'cases' must declare at least one case; an empty suite scores nothing")

    cases = tuple(
        _read_work_case(path, entry) if work else _read_case(path, entry, memory=memory)
        for entry in raw
    )

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise _error(
                path,
                f"declares the duplicate case id {case.id!r}. Case ids key eval_results rows and "
                f"name fixture scope directories, so each must appear exactly once.",
            )
        seen.add(case.id)
    return cases


def _read_case(path: Path, entry: object, *, memory: bool = False) -> Case:
    if not isinstance(entry, dict):
        raise _error(path, f"case {entry!r} must be a mapping with 'id', 'prompt' and 'graders'")

    known = MEMORY_CASE_FIELDS if memory else CASE_FIELDS
    unknown = sorted(set(entry) - known)
    if unknown:
        raise _error(
            path,
            f"case {entry.get('id')!r} has unknown field(s) {unknown}. "
            f"Known fields: {sorted(known)}.",
        )

    case_id = _read_case_id(path, entry)

    if "prompt" not in entry:
        raise _error(path, f"case {case_id!r} is missing the required field 'prompt'")
    prompt = entry["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise _error(path, f"case {case_id!r} needs a non-empty string 'prompt', got {prompt!r}")

    episodes = _read_episodes(path, case_id, entry) if memory else ()
    return Case(
        id=case_id, prompt=prompt, graders=_read_graders(path, case_id, entry), episodes=episodes
    )


def _read_episodes(path: Path, case_id: str, entry: Mapping[str, Any]) -> tuple[str, ...]:
    """A memory case's recorded episodes: a non-empty list of non-empty strings."""
    raw = entry.get("episodes")
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(text, str) and text.strip() for text in raw)
    ):
        raise _error(path, f"case {case_id!r} needs 'episodes', a non-empty list of strings")
    return tuple(raw)


def _read_work_case(path: Path, entry: object) -> Case:
    """A recorded case: ``id``, ``input``, ``handler``, ``reference`` and a ``verdict``."""
    if not isinstance(entry, dict):
        raise _error(path, f"case {entry!r} must be a mapping")
    unknown = sorted(set(entry) - WORK_CASE_FIELDS)
    if unknown:
        raise _error(
            path,
            f"case {entry.get('id')!r} has unknown field(s) {unknown}. "
            f"Known fields: {sorted(WORK_CASE_FIELDS)}.",
        )
    case_id = _read_case_id(path, entry)
    for required in ("input", "handler", "reference"):
        if not isinstance(entry.get(required), str):
            raise _error(path, f"case {case_id!r} needs a string {required!r}")
    verdict = entry.get("verdict", VERDICT_PENDING)
    if verdict not in VERDICTS:
        raise _error(path, f"case {case_id!r}: verdict {verdict!r} is not one of {list(VERDICTS)}")
    traits = entry.get("traits") or []
    if not isinstance(traits, list) or not all(isinstance(trait, str) for trait in traits):
        raise _error(path, f"case {case_id!r}: 'traits' must be a list of strings")
    if verdict == VERDICT_TRAITS and not traits:
        raise _error(path, f"case {case_id!r}: a 'traits' verdict needs at least one trait")
    return Case(
        id=case_id,
        prompt=entry["input"],
        graders=(),
        handler=entry["handler"],
        reference=entry["reference"],
        verdict=verdict,
        traits=tuple(traits),
    )


def _read_case_id(path: Path, entry: Mapping[str, Any]) -> str:
    if "id" not in entry:
        raise _error(path, f"case {entry!r} is missing the required field 'id'")
    case_id = entry["id"]
    if not isinstance(case_id, str) or not CASE_ID_PATTERN.match(case_id):
        raise _error(
            path,
            f"case id {case_id!r} is not a usable id. A case id names a fixture scope directory "
            f"under corpus/wire/fake/, so it must match {CASE_ID_PATTERN.pattern} - "
            f"letters, digits, hyphen and underscore, with no dot and no path separator.",
        )
    return case_id


def _read_graders(path: Path, case_id: str, entry: Mapping[str, Any]) -> tuple[GraderSpec, ...]:
    if "graders" not in entry:
        raise _error(path, f"case {case_id!r} is missing the required field 'graders'")
    raw = entry["graders"]
    if not isinstance(raw, list):
        raise _error(path, f"case {case_id!r}: 'graders' must be a list, got {raw!r}")
    if not raw:
        raise _error(
            path,
            f"case {case_id!r}: 'graders' must name at least one grader; an ungraded case "
            f"cannot be scored",
        )
    return tuple(_read_grader(path, case_id, spec) for spec in raw)


def _read_grader(path: Path, case_id: str, spec: object) -> GraderSpec:
    if not isinstance(spec, dict):
        raise _error(
            path,
            f"case {case_id!r}: each entry of 'graders' must be a mapping with a 'name', "
            f"got {spec!r}",
        )

    unknown = sorted(set(spec) - GRADER_FIELDS)
    if unknown:
        raise _error(
            path,
            f"case {case_id!r}: grader has unknown field(s) {unknown}. "
            f"Known fields: {sorted(GRADER_FIELDS)}.",
        )

    if "name" not in spec:
        raise _error(path, f"case {case_id!r}: a grader entry in 'graders' is missing its 'name'")
    name = spec["name"]
    if name not in GRADER_NAMES:
        raise _error(
            path,
            f"case {case_id!r}: 'graders' names the unknown grader {name!r}. Known graders: "
            f"{', '.join(GRADER_NAMES)}.",
        )

    expect = spec.get("expect", {})
    if not isinstance(expect, dict):
        raise _error(
            path, f"case {case_id!r}: grader {name!r} needs an 'expect' mapping, got {expect!r}"
        )

    return GraderSpec(
        name=name, expect=dict(expect), threshold=_read_threshold(path, case_id, name, spec)
    )


def _read_threshold(path: Path, case_id: str, name: str, spec: Mapping[str, Any]) -> float:
    """The declared pass line. Absent means 1.0: a grader must clear its scale completely."""
    if "threshold" not in spec:
        return DEFAULT_THRESHOLD
    threshold = spec["threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, int | float):
        raise _error(
            path,
            f"case {case_id!r}: grader {name!r} needs a numeric 'threshold', got " f"{threshold!r}",
        )
    if not 0.0 <= float(threshold) <= 1.0:
        raise _error(
            path,
            f"case {case_id!r}: grader {name!r} declares threshold {threshold!r}, which is "
            f"outside the [0.0, 1.0] range every grader scores on.",
        )
    return float(threshold)


# ---- FR-139: the listing the Evals page and `harnessed eval suites` serve ------------------------


@dataclass(frozen=True, slots=True)
class SuiteRow:
    """One suite as a listing carries it: what it is, how many cases await a verdict, how it last
    scored. The ``latest_*`` fields are ``None`` for a suite that never ran (ticket 238)."""

    suite: str
    kind: str
    description: str
    cases_total: int
    cases_pending: int
    latest_eval_run_id: str | None = None
    latest_score: float | None = None
    latest_started_at: str | None = None
    latest_label: str | None = None


def listing(
    conn: sqlite3.Connection, root: Path | Sequence[Path] | None = None
) -> tuple[SuiteRow, ...]:
    """Every suite under ``root`` in name order, each with the count the store holds as pending
    and its latest scored run. The score is the run's own (``runner.summary_payload``'s, ``None``
    for a run with no cases): nothing here is computed from a case."""
    from harnessed.evals import runner  # runner imports this module; a top import would cycle

    suites_root = suite_roots() if root is None else root
    rows: list[SuiteRow] = []
    for name in _available(suites_root):
        loaded = load_named_suite(name, suites_root)
        latest = runner.latest_of(conn, name)
        served = None if latest is None else runner.summary_payload(latest)
        rows.append(
            SuiteRow(
                suite=loaded.name,
                kind=loaded.kind,
                description=loaded.description,
                cases_total=len(loaded.cases),
                cases_pending=len(loaded.pending),
                latest_eval_run_id=None if latest is None else latest.eval_run_id,
                latest_score=None if served is None else _score_of(served),
                latest_started_at=None if latest is None else latest.started_at,
                latest_label=None if latest is None else latest.label,
            )
        )
    return tuple(rows)


def _score_of(summary: Mapping[str, object]) -> float | None:
    score = summary["score"]
    return None if score is None else float(str(score))


def listing_payload(rows: tuple[SuiteRow, ...]) -> dict[str, object]:
    """The suites as plain data: ``GET /v1/eval/suites`` and ``eval suites --json``, one call.
    The two counts are strings (decision 41); a figure never reaches a screen as arithmetic."""
    return {
        "suites": [
            {
                "suite": row.suite,
                "kind": row.kind,
                "description": row.description,
                "cases_total": str(row.cases_total),
                "cases_pending": str(row.cases_pending),
                "latest_eval_run_id": row.latest_eval_run_id,
                "latest_score": row.latest_score,
                "latest_started_at": row.latest_started_at,
                "latest_label": row.latest_label,
            }
            for row in rows
        ]
    }


# ---- FR-152: the memory suite's seeds ---------------------------------------------------------


def seed_episodes(conn: sqlite3.Connection, suite: Suite) -> int:
    """Seed a memory suite's recorded episodes into the home; answer how many this call wrote.

    Each episode goes through ``episodic.put`` under the source ``seed:<case id>``, the case's
    answer existing only across them. Idempotent: a case whose source already holds its episodes
    is left as it stands, so the second run of the suite writes nothing. A home holding any
    episode that is not a seed is refused (:class:`SuiteLoadError`): the suite measures what
    consolidation made of *these* episodes, and a real session's own would be a different
    measurement under the same name.
    """
    from harnessed.memory import episodic  # a suite loads without a store; only seeding opens one

    foreign = episodic.count_outside(conn, SEED_PREFIX)
    if foreign:
        raise SuiteLoadError(
            f"suite {suite.name!r} seeds its own episodes and refuses a home that already holds "
            f"{foreign} that are not seeds. Run it in a scratch HARNESSED_HOME."
        )
    written = 0
    for case in suite.cases:
        source = f"{SEED_PREFIX}{case.id}"
        if episodic.for_run(conn, source):
            continue
        for text in case.episodes:
            episodic.put(conn, source, "seed", text)
            written += 1
    return written
