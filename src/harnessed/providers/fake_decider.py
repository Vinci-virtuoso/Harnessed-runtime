"""FakeDecider: a lookup-table Decider that replays a committed, hand-reviewed corpus.

FR-96 (UJ-21). Decision 57's Offline paragraph: this is what lets routing stay inside every
offline test the same way :class:`~harnessed.providers.fake.FakeProvider` keeps FR-25 offline
(THE CRUX). Nothing here calls the Decisions API and nothing here opens a socket.

**The selection key is the same triple**, computed purely from what :meth:`FakeDecider.decide`
is handed: ``scope`` is the caller's declared scope if it names one, else ``sha256`` of the
state's ``Task:`` line, truncated to 12 hex characters, exactly as
:meth:`~harnessed.providers.fake.FakeProvider.scope_of` falls back for a request with no declared
scope; ``iteration`` is the caller's declared hop plus one; ``variant`` is the first variant whose
predicate matches the state text, ``base`` last and mandatory. **Declared, not read off a
request**: :class:`~harnessed.providers.jev.Decider` answers ``decide(state, questions)`` with a
plain string, not a request object with a ``metadata`` mapping, so ``scope`` and ``hop`` are
keyword-only parameters nobody but a test (today) passes explicitly - the router ticket that
calls this seam for real reaches it the same way :class:`~harnessed.providers.jev.JevDecider` is
reached, with neither declared, and gets the hash fallback and iteration 1 like a first message
always does.

The corpus is validated **eagerly**, at construction, exactly like ``providers/fake.py``: every
``_index.yaml`` is parsed and every response file is decoded as JSON, so a corpus typo fails the
first test rather than a request buried in it. Each response file MUST carry a non-empty ``task``
string naming what it answers (MUST rule 8: fixtures are reviewed data, and a fixture that does
not say what it is for cannot be reviewed as one). A miss at request time is loud -
``FixtureMissError`` naming the exact path looked for, never a ``KeyError`` and never a
``DecisionError`` child: a missing fixture must fail the test that needed it, not fall back to
Groq chat the way a live refusal does (decision 57).

The predicate vocabulary is ``always``, ``state_contains_all`` and ``state_matches``, over the
state text handed to ``decide`` rather than over a system prompt - the Decider seam has no system
prompt, only the labelled state ``router/state.py`` builds (decision 58). Widening this vocabulary
is the same gate as widening ``providers/fake.py``'s (All AI agents MUST rule 2's twin): this
module imports nothing from ``evals/``, ``improve/`` or ``memory/``, asserted by
``tests/test_boundaries.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

# No stub package is pinned by architecture.md, and adding one is a gate decision
# (MUST rule 9); `providers/fake.py` acknowledges the same untyped import.
import yaml  # type: ignore[import-untyped]

from harnessed import host, shipped
from harnessed.config import Settings
from harnessed.errors import FixtureCorpusError, FixtureMissError
from harnessed.providers.jev import Decisions, Question, parse

__all__ = [
    "DEFAULT_CORPUS_ROOT",
    "PREDICATE_NAMES",
    "FakeDecider",
    "Variant",
]

#: The committed corpus, found relative to this file so the lookup never depends on the working
#: directory. ``fake_decider.py`` -> ``providers`` -> ``harnessed`` -> ``src`` -> repository root.
DEFAULT_CORPUS_ROOT: Final[Path] = shipped.jev_wire()

#: The mandatory last variant of every scope, exactly as ``providers/fake.py`` requires one.
BASE_VARIANT: Final = "base"

INDEX_NAME: Final = "_index.yaml"

#: The one label the state format (decision 58) guarantees every call carries, and the line this
#: module hashes when no scope is declared.
TASK_LABEL: Final = "Task:"


# ---- the predicate vocabulary, over state text rather than a system prompt ----------------------


def normalize_state(state: str) -> str:
    """The one projection of state text every predicate sees: collapsed and case-folded, exactly
    as ``providers/fake.py::normalize_system`` folds a system prompt."""
    return " ".join(state.split()).casefold()


def _task_line(state: str) -> str:
    """The text of the state's ``Task:`` line, or the whole state if none is labelled that way.

    ``router/state.py`` (decision 58) always opens with ``Task:``; the fallback exists only so a
    hand-written test state with no such line still hashes to something stable, rather than this
    module refusing to compute a scope at all.
    """
    for line in state.splitlines():
        if line.startswith(TASK_LABEL):
            return line[len(TASK_LABEL) :].strip()
    return state.strip()


def _corpus_error(path: Path, problem: str) -> FixtureCorpusError:
    return FixtureCorpusError(f"{path}: {problem}")


def _parse_nothing(path: Path, name: str, value: object) -> None:
    if value is not None:
        raise _corpus_error(path, f"variant {name!r}: the 'always' predicate takes no value")
    return None


def _parse_traits(path: Path, name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _corpus_error(
            path,
            f"variant {name!r}: 'state_contains_all' needs a non-empty list of strings, got "
            f"{value!r}",
        )
    traits = []
    for entry in value:
        if not isinstance(entry, str):
            raise _corpus_error(
                path,
                f"variant {name!r}: every 'state_contains_all' trait must be a string, got "
                f"{entry!r}",
            )
        trait = " ".join(entry.split()).casefold()
        if not trait:
            raise _corpus_error(
                path,
                f"variant {name!r}: a 'state_contains_all' trait must be non-empty once "
                f"whitespace is collapsed; {entry!r} is not",
            )
        traits.append(trait)
    return tuple(traits)


def _parse_pattern(path: Path, name: str, value: object) -> re.Pattern[str]:
    if not isinstance(value, str):
        raise _corpus_error(
            path, f"variant {name!r}: 'state_matches' needs a string pattern, got {value!r}"
        )
    try:
        return re.compile(value)
    except re.error as exc:
        raise _corpus_error(
            path, f"variant {name!r}: {value!r} is not a valid regular expression: {exc}"
        ) from exc


@dataclass(frozen=True, slots=True)
class _Predicate:
    parse: Callable[[Path, str, Any], Any]
    matches: Callable[[Any, str], bool]


#: One entry per permitted predicate - the keys of this table are the vocabulary. Widening it is
#: a gate decision (All AI agents MUST rule 2's twin), same as ``providers/fake.py``'s table.
_PREDICATES: Final[dict[str, _Predicate]] = {
    "always": _Predicate(parse=_parse_nothing, matches=lambda _value, _state: True),
    "state_contains_all": _Predicate(
        parse=_parse_traits,
        matches=lambda traits, state: all(trait in normalize_state(state) for trait in traits),
    ),
    "state_matches": _Predicate(
        parse=_parse_pattern,
        matches=lambda pattern, state: pattern.search(normalize_state(state)) is not None,
    ),
}

PREDICATE_NAMES: Final[tuple[str, ...]] = tuple(sorted(_PREDICATES))


@dataclass(frozen=True, slots=True)
class Variant:
    """One declared variant of a scope: a name and the parsed predicate that selects it."""

    name: str
    predicate: str
    value: Any = None

    def matches(self, state: str) -> bool:
        return _PREDICATES[self.predicate].matches(self.value, state)


@dataclass(frozen=True, slots=True)
class _Scope:
    """One loaded scope directory: its ordered variants and its raw, undecoded response bytes.

    The bytes stay raw rather than pre-parsed into :class:`~harnessed.providers.jev.Decisions`,
    because :meth:`FakeDecider.decide` is asked a different ``questions`` tuple on every call and
    :func:`~harnessed.providers.jev.parse` needs that tuple to know which answers to require. That
    is also what makes AC 1 literal: the fixture is parsed through the exact same function the
    live transport calls, not a copy of it.
    """

    name: str
    path: Path
    variants: tuple[Variant, ...]
    responses: Mapping[tuple[str, int], bytes]

    def select(self, state: str) -> Variant:
        for variant in self.variants:
            if variant.matches(state):
                return variant
        raise FixtureCorpusError(  # pragma: no cover - a mandatory `base` makes this unreachable
            f"no variant matched in {self.path / INDEX_NAME}, which means the mandatory "
            f"{BASE_VARIANT!r} variant is missing and load-time validation let it through."
        )


def _read_variant(path: Path, entry: object) -> Variant:
    if not isinstance(entry, dict):
        raise _corpus_error(path, f"variant {entry!r} must be a mapping with 'name' and 'when'")

    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise _corpus_error(path, f"variant {entry!r} needs a non-empty string 'name'")
    if "." in name or host.has_separator(name):
        raise _corpus_error(
            path, f"variant name {name!r} may not contain a dot or a path separator"
        )

    when = entry.get("when")
    if isinstance(when, str):
        predicate, value = when, None
    elif isinstance(when, dict) and len(when) == 1:
        predicate, value = next(iter(when.items()))
    else:
        raise _corpus_error(
            path,
            f"variant {name!r} needs a 'when' that is either a bare predicate name or a "
            f"single-key mapping, got {when!r}",
        )

    if predicate not in _PREDICATES:
        raise _corpus_error(
            path,
            f"variant {name!r} uses the unknown predicate {predicate!r}. Permitted predicates: "
            f"{', '.join(PREDICATE_NAMES)}. Widening this vocabulary is a gate decision.",
        )
    return Variant(
        name=name, predicate=predicate, value=_PREDICATES[predicate].parse(path, name, value)
    )


def _read_index(path: Path) -> tuple[Variant, ...]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _corpus_error(path, f"is not valid YAML: {exc}") from exc

    if not isinstance(document, dict):
        raise _corpus_error(path, "must be a mapping with 'scope' and 'variants' keys")

    declared_scope = document.get("scope")
    if declared_scope != path.parent.name:
        raise _corpus_error(
            path,
            f"declares scope {declared_scope!r} but lives in directory "
            f"{path.parent.name!r}. The two must agree.",
        )

    raw_variants = document.get("variants")
    if not isinstance(raw_variants, list) or not raw_variants:
        raise _corpus_error(path, "must declare a non-empty ordered 'variants' list")

    variants = tuple(_read_variant(path, entry) for entry in raw_variants)

    names = [variant.name for variant in variants]
    if len(set(names)) != len(names):
        raise _corpus_error(path, f"declares a duplicate variant name in {names}")
    if names[-1] != BASE_VARIANT:
        raise _corpus_error(
            path,
            f"must declare the mandatory {BASE_VARIANT!r} variant last, so that every request "
            f"matches something. Declared order: {names}.",
        )
    if variants[-1].predicate != "always":
        raise _corpus_error(
            path,
            f"the {BASE_VARIANT!r} variant's predicate must be 'always', not "
            f"{variants[-1].predicate!r}.",
        )
    return variants


def _split_stem(path: Path) -> tuple[str, int]:
    """``base.json`` -> ``("base", 1)``, ``base.2.json`` -> ``("base", 2)``."""
    stem = path.name[: -len(".json")]
    variant, _, suffix = stem.partition(".")
    if not suffix:
        return variant, 1
    if not suffix.isdigit() or int(suffix) < 1:
        raise _corpus_error(
            path, f"iteration suffix {suffix!r} must be a positive integer, as in 'base.2.json'"
        )
    return variant, int(suffix)


def _load_response(path: Path) -> bytes:
    """Validate one response file and return its exact committed bytes, unchanged.

    Decoded only to check it is well-formed JSON and carries the ``task`` it answers (MUST rule
    8); the bytes handed back are the file's own, so nothing this module does can drift them from
    what a reviewer approved.
    """
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _corpus_error(path, f"is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _corpus_error(path, "must hold a JSON object")
    task = payload.get("task")
    if not isinstance(task, str) or not task.strip():
        raise _corpus_error(path, "needs a non-empty 'task' string naming the task text it answers")
    return raw


def _load_scope(directory: Path) -> _Scope:
    variants = _read_index(directory / INDEX_NAME)
    declared = {variant.name for variant in variants}

    responses: dict[tuple[str, int], bytes] = {}
    for path in sorted(directory.glob("*.json")):
        variant, iteration = _split_stem(path)
        if variant not in declared:
            raise _corpus_error(
                path,
                f"is named for the variant {variant!r}, which "
                f"{directory / INDEX_NAME} does not declare. Declared: {sorted(declared)}.",
            )
        responses[variant, iteration] = _load_response(path)

    return _Scope(name=directory.name, path=directory, variants=variants, responses=responses)


def _load_corpus(roots: tuple[Path, ...]) -> dict[str, _Scope]:
    """Load and validate every scope under ``roots``, shipped first. All of it, at construction.

    The first root is the shipped corpus and must exist; a later one (the home) may be absent. A
    scope named in two roots is a :class:`~harnessed.errors.FixtureCorpusError`: a recorded
    fixture never shadows a shipped one (decision 88).
    """
    root = roots[0]
    if not root.is_dir():
        raise FixtureCorpusError(
            f"fixture corpus root {root} does not exist. FakeDecider replays a committed "
            f"corpus; there is nothing to replay from."
        )
    scopes: dict[str, _Scope] = {}
    for each in roots:
        for index in sorted(each.glob(f"*/{INDEX_NAME}")):
            scope = _load_scope(index.parent)
            if scope.name in scopes:
                raise FixtureCorpusError(
                    f"the recorded fixture scope {scope.name} would shadow a shipped one: "
                    f"{index.parent} and {scopes[scope.name].path}."
                )
            scopes[scope.name] = scope
    return scopes


def _filename(variant: str, iteration: int) -> str:
    return f"{variant}.json" if iteration == 1 else f"{variant}.{iteration}.json"


class FakeDecider:
    """Replay the committed Decisions corpus. Same state, same key, same bytes - every time."""

    def __init__(self, settings: Settings, root: Path | tuple[Path, ...] | None = None) -> None:
        self.settings = settings
        self.roots = (
            (DEFAULT_CORPUS_ROOT,) if root is None else (root,) if isinstance(root, Path) else root
        )
        self.root = self.roots[0]
        self._scopes = _load_corpus(self.roots)

    @property
    def scopes(self) -> tuple[str, ...]:
        """The scope names this corpus can serve, sorted."""
        return tuple(sorted(self._scopes))

    @staticmethod
    def scope_of(state: str, declared: str | None = None) -> str:
        """``declared`` if given, else ``sha256`` of the state's ``Task:`` line - THE CRUX's
        fallback, restated for a Decider that is handed text instead of a request."""
        if declared:
            return declared
        return sha256(_task_line(state).encode("utf-8")).hexdigest()[:12]

    def decide(
        self,
        state: str,
        questions: tuple[Question, ...],
        *,
        scope: str | None = None,
        hop: int = 0,
    ) -> Decisions:
        """Answer ``questions`` from the corpus, or miss loudly saying where it looked.

        ``scope`` and ``hop`` are the declared halves of the key (see the module docstring); a
        caller that gives neither gets the same first-message answer every time, which is what
        keeps a resumed run's replay deterministic.
        """
        scope_name = self.scope_of(state, scope)
        iteration = hop + 1

        loaded = self._scopes.get(scope_name)
        if loaded is None:
            raise FixtureMissError(
                f"no fixture directory for scope {scope_name!r} at iteration {iteration}. "
                f"Expected {self.root / scope_name}. Add the directory with an "
                f"{INDEX_NAME}, or declare a scope this corpus has: {', '.join(self.scopes)}."
            )

        variant = loaded.select(state)
        raw = loaded.responses.get((variant.name, iteration))
        if raw is None:
            raise FixtureMissError(
                f"no fixture for key (scope={scope_name!r}, iteration={iteration}, "
                f"variant={variant.name!r}). Looked for "
                f"{loaded.path / _filename(variant.name, iteration)}."
            )

        return parse(raw, questions)
