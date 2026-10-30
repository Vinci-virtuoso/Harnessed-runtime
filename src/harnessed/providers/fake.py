"""FakeProvider: a lookup-table model that replays a committed corpus of normalized responses.

FR-2. This is what makes every later phase testable offline - no key, no socket, no vendor SDK.
It satisfies the same :class:`~harnessed.providers.base.Provider` Protocol as the live
adapters, so nothing above ``providers/`` can tell the difference.

The selection key is the triple ``(scope, iteration, variant)``, computed purely from the
normalized request (see ``architecture.md``, THE CRUX). Nothing is remembered between calls: a
resumed run reconstructs the same key from the same request and therefore replays the same
bytes. That is the whole determinism claim, and it is why there is no counter on this class.

The corpus is validated **eagerly**, at construction: every ``_index.yaml`` is parsed and every
response file is decoded, so a corpus typo fails the first test rather than the forty-first.
Misses at request time are loud - ``FixtureMissError`` naming the exact path it looked for, never
a bare ``KeyError``.

Two boundaries this module must keep:

- It imports nothing from ``evals/``, ``improve/`` or ``memory/``. A fixture may not be selected
  on anything the system computed about its own performance (All AI agents MUST rule 2).
- It touches the process environment nowhere. Configuration arrives as a
  :class:`~harnessed.config.Settings` built by ``config.py``, the one reader of it (rule 3), and
  the one knob this module asks for by name - :func:`~harnessed.config.fake_latency_ms`, a test
  affordance that changes wall time and nothing else - is a call into that same module, which is
  why ``tests/test_fake_provider.py``'s text scan over this file still passes.

The predicate vocabulary lives in :data:`_PREDICATES` and is the entire security boundary of the
FR-25 proof: ``always``, ``system_contains_all``, ``system_matches`` and ``context_contains_all``,
and nothing else. A predicate reads the request's system prompt, which is content a candidate must
actually contain, or - for ``context_contains_all``, the one term ticket 249 added at the gate - the
retrieved context block of the user turn, which is memory the operator's own sessions wrote. Never
an identity the system assigned it. That is the difference between this proof and a staged
one, and widening the table requires revising ``architecture.md`` at a gate (rule 2).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

# No stub package is pinned by architecture.md, and adding one is a gate decision
# (MUST rule 9), so the untyped import is acknowledged here rather than depended on.
import yaml  # type: ignore[import-untyped]

from harnessed import host, shipped
from harnessed.config import Settings, fake_latency_ms
from harnessed.errors import FixtureCorpusError, FixtureMissError
from harnessed.providers.types import (
    STOP_REASONS,
    ChatRequest,
    ChatResponse,
    StopReason,
    ToolCall,
    Usage,
)

__all__ = [
    "CONTEXT_CLOSE",
    "CONTEXT_OPEN",
    "DEFAULT_CORPUS_ROOT",
    "PREDICATE_NAMES",
    "FakeProvider",
    "Variant",
]

#: The retrieved-context markers ``loop/prompt.py`` wraps a run's memory in (ticket 083). Restated
#: as literals because ``providers/`` may not import ``loop/``; ``tests/test_prompt_prefix.py``
#: pins them equal to the originals.
CONTEXT_OPEN: Final = "<retrieved_context>"
CONTEXT_CLOSE: Final = "</retrieved_context>"

#: The committed corpus, found relative to this file so the lookup never depends on the working
#: directory. ``fake.py`` -> ``providers`` -> ``harnessed`` -> ``src`` -> repository root.
DEFAULT_CORPUS_ROOT: Final[Path] = shipped.fake_wire()

#: The mandatory last variant of every scope. Its predicate is ``always``, which is what makes
#: "directory exists but no variant matched" impossible.
BASE_VARIANT: Final = "base"

INDEX_NAME: Final = "_index.yaml"


# ---- the predicate vocabulary ------------------------------------------------------------------


def normalize_system(request: ChatRequest) -> str:
    """The one projection of a system prompt every predicate sees: collapsed and case-folded.

    Whitespace is collapsed to single spaces and the result is case-folded, so a candidate is
    judged on what it says rather than on how it was wrapped or capitalized. Declared trait
    strings go through exactly the same function, so both sides of a comparison are normalized
    identically and authoring a trait stays forgiving.
    """
    return " ".join(request.system.split()).casefold()


def _parse_nothing(path: Path, name: str, value: object) -> None:
    """``always`` takes no argument. Anything attached to it is a corpus typo, not a nuance."""
    if value is not None:
        raise _corpus_error(path, f"variant {name!r}: the 'always' predicate takes no value")
    return None


#: The tags ``loop/prompt.py`` wraps retrieved memory in. Restated, not imported: this module
#: reaches no package above ``providers/`` (a test scans for it).
_CONTEXT_BLOCK: Final = re.compile(r"<retrieved_context>(.*?)</retrieved_context>", re.DOTALL)


def normalize_context(request: ChatRequest) -> str:
    """The retrieved context text of the request, collapsed and case-folded like the system prompt.

    Only what sits between the context tags of a user turn: not the system prompt, not the task,
    not any other message. A request carrying no retrieved context projects to the empty string.
    """
    blocks = (
        block
        for message in request.messages
        if message.role == "user"
        for block in _CONTEXT_BLOCK.findall(message.content)
    )
    return " ".join(" ".join(blocks).split()).casefold()


def _parse_traits(
    path: Path, name: str, value: object, predicate: str = "system_contains_all"
) -> tuple[str, ...]:
    """``system_contains_all`` declares a non-empty list of non-empty strings, pre-normalized."""
    if not isinstance(value, list):
        raise _corpus_error(
            path, f"variant {name!r}: '{predicate}' needs a list of strings, got {value!r}"
        )
    if not value:
        raise _corpus_error(
            path, f"variant {name!r}: '{predicate}' needs a non-empty list of strings"
        )
    traits = []
    for entry in value:
        if not isinstance(entry, str):
            raise _corpus_error(
                path,
                f"variant {name!r}: every '{predicate}' trait must be a string, got {entry!r}",
            )
        trait = " ".join(entry.split()).casefold()
        if not trait:
            raise _corpus_error(
                path,
                f"variant {name!r}: a '{predicate}' trait must be non-empty once "
                f"whitespace is collapsed; {entry!r} is not",
            )
        traits.append(trait)
    return tuple(traits)


def _parse_pattern(path: Path, name: str, value: object) -> re.Pattern[str]:
    """``system_matches`` declares one regex, compiled at load so a typo is a load-time error."""
    if not isinstance(value, str):
        raise _corpus_error(
            path, f"variant {name!r}: 'system_matches' needs a string pattern, got {value!r}"
        )
    try:
        return re.compile(value)
    except re.error as exc:
        raise _corpus_error(
            path, f"variant {name!r}: {value!r} is not a valid regular expression: {exc}"
        ) from exc


@dataclass(frozen=True, slots=True)
class _Predicate:
    """One permitted predicate: how its declared value is validated, and how it decides.

    ``parse`` runs once at construction and returns the form ``matches`` consumes - collapsed
    traits, a compiled pattern - so a request-time decision is a comparison and never a parse.
    """

    parse: Callable[[Path, str, Any], Any]
    matches: Callable[[Any, ChatRequest], bool]


#: One entry per permitted predicate. The keys of this table *are* the vocabulary - validation
#: reads them, so registering a predicate is the only way to widen what a corpus may declare, and
#: widening it requires a gate (MUST rule 2). Every predicate here reads the system prompt and
#: nothing else: no candidate id, no procedure id, no score, no pass or fail, no run id.
_PREDICATES: Final[dict[str, _Predicate]] = {
    "always": _Predicate(parse=_parse_nothing, matches=lambda _value, _request: True),
    "system_contains_all": _Predicate(
        parse=_parse_traits,
        matches=lambda traits, request: all(trait in normalize_system(request) for trait in traits),
    ),
    "system_matches": _Predicate(
        parse=_parse_pattern,
        matches=lambda pattern, request: pattern.search(normalize_system(request)) is not None,
    ),
    # Ticket 249 (gate ruling 2026-10-03): reads the retrieved context of the user turn, which is
    # memory the operator's sessions wrote, and never the system prompt or the task.
    "context_contains_all": _Predicate(
        parse=lambda path, name, value: _parse_traits(path, name, value, "context_contains_all"),
        matches=lambda traits, request: all(
            trait in normalize_context(request) for trait in traits
        ),
    ),
}

#: The vocabulary, as a stable tuple, for anything that wants to assert it without importing the
#: table. Sorted so the error messages and the assertion read the same way every time.
PREDICATE_NAMES: Final[tuple[str, ...]] = tuple(sorted(_PREDICATES))


@dataclass(frozen=True, slots=True)
class Variant:
    """One declared variant of a scope: a name and the parsed predicate that selects it."""

    name: str
    predicate: str
    value: Any = None

    def matches(self, request: ChatRequest) -> bool:
        return _PREDICATES[self.predicate].matches(self.value, request)


@dataclass(frozen=True, slots=True)
class _Scope:
    """One loaded scope directory: its ordered variants and its decoded responses."""

    name: str
    path: Path
    variants: tuple[Variant, ...]
    responses: Mapping[tuple[str, int], ChatResponse]

    def select(self, request: ChatRequest) -> Variant:
        """The first variant whose predicate matches. ``base`` guarantees there is one."""
        for variant in self.variants:
            if variant.matches(request):
                return variant
        raise FixtureCorpusError(  # pragma: no cover - a mandatory `base` makes this unreachable
            f"no variant matched in {self.path / INDEX_NAME}, which means the mandatory "
            f"{BASE_VARIANT!r} variant is missing and load-time validation let it through."
        )


# ---- corpus loading ----------------------------------------------------------------------------


def _corpus_error(path: Path, problem: str) -> FixtureCorpusError:
    return FixtureCorpusError(f"{path}: {problem}")


def _read_index(path: Path) -> tuple[Variant, ...]:
    """Parse and validate one ``_index.yaml`` into its ordered variants."""
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


def _read_variant(path: Path, entry: object) -> Variant:
    """One ``{name, when}`` declaration, with its predicate checked against the vocabulary."""
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


def _decode_response(path: Path) -> ChatResponse:
    """Decode one normalized response file. Every rejection is a corpus error, not a miss."""
    # Decoded from an open corpus file, deliberately not from a string: the string form of this
    # call is quarantined to ``translate.py``, the only module permitted to parse model output
    # (architecture.md), and ticket 003 guards that with a text scan over this package. A
    # committed fixture is reviewed data, not model output, so it is read as a file.
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise _corpus_error(path, f"is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _corpus_error(path, "must hold a JSON object")

    known = {"response_id", "model", "content", "tool_calls", "stop_reason", "usage"}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise _corpus_error(path, f"has unknown field(s) {unknown}. Known fields: {sorted(known)}")

    for required in ("response_id", "model"):
        if not isinstance(payload.get(required), str) or not payload[required]:
            raise _corpus_error(path, f"needs a non-empty string {required!r}")

    stop_reason = payload.get("stop_reason", "end_turn")
    if stop_reason not in STOP_REASONS:
        raise _corpus_error(
            path,
            f"stop_reason {stop_reason!r} is not in the normalized vocabulary "
            f"{list(STOP_REASONS)}",
        )

    return ChatResponse(
        response_id=payload["response_id"],
        model=payload["model"],
        content=_decode_str(path, payload.get("content", ""), "content"),
        tool_calls=_decode_tool_calls(path, payload.get("tool_calls", [])),
        stop_reason=_cast_stop_reason(stop_reason),
        usage=_decode_usage(path, payload.get("usage", {})),
    )


def _cast_stop_reason(value: object) -> StopReason:
    """Narrow an already-validated stop word to the ``StopReason`` literal."""
    assert isinstance(value, str) and value in STOP_REASONS
    return value


def _decode_str(path: Path, value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _corpus_error(path, f"{field!r} must be a string, got {type(value).__name__}")
    return value


def _decode_tool_calls(path: Path, value: object) -> tuple[ToolCall, ...]:
    if not isinstance(value, list):
        raise _corpus_error(path, "'tool_calls' must be a list")
    calls = []
    for entry in value:
        if not isinstance(entry, dict):
            raise _corpus_error(path, f"tool call {entry!r} must be a mapping")
        arguments = entry.get("arguments", {})
        if not isinstance(arguments, dict):
            raise _corpus_error(path, "a tool call's 'arguments' must be a JSON object")
        calls.append(
            ToolCall(
                id=_decode_str(path, entry.get("id", ""), "tool call id"),
                name=_decode_str(path, entry.get("name", ""), "tool call name"),
                arguments=arguments,
            )
        )
    return tuple(calls)


def _decode_usage(path: Path, value: object) -> Usage:
    if not isinstance(value, dict):
        raise _corpus_error(path, "'usage' must be a mapping")
    counters = {}
    for field in ("input_tokens", "output_tokens"):
        count = value.get(field, 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise _corpus_error(path, f"usage.{field} must be a non-negative integer")
        counters[field] = count
    return Usage(**counters)


def _load_scope(directory: Path) -> _Scope:
    """Load one scope directory: its index, then every response file it declares."""
    variants = _read_index(directory / INDEX_NAME)
    declared = {variant.name for variant in variants}

    responses: dict[tuple[str, int], ChatResponse] = {}
    for path in sorted(directory.glob("*.json")):
        variant, iteration = _split_stem(path)
        if variant not in declared:
            raise _corpus_error(
                path,
                f"is named for the variant {variant!r}, which "
                f"{directory / INDEX_NAME} does not declare. Declared: {sorted(declared)}.",
            )
        responses[variant, iteration] = _decode_response(path)

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
            f"fixture corpus root {root} does not exist. FakeProvider replays a committed "
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


# ---- the provider ------------------------------------------------------------------------------


class FakeProvider:
    """Replay the committed corpus. Same request, same key, same bytes - across processes."""

    def __init__(self, settings: Settings, root: Path | tuple[Path, ...] | None = None) -> None:
        self.settings = settings
        self.roots = (
            (DEFAULT_CORPUS_ROOT, settings.home / "fixtures" / "fake")
            if root is None
            else (root,)
            if isinstance(root, Path)
            else root
        )
        self.root = self.roots[0]
        self._scopes = _load_corpus(self.roots)
        # Read once, here, for the same reason the corpus is loaded here: a malformed value is a
        # loud failure at construction rather than a surprise on the fortieth request. Held as
        # seconds because that is what `time.sleep` takes; the variable is milliseconds because
        # that is the scale of the window it exists to widen.
        self._latency_seconds = fake_latency_ms() / 1000

    @property
    def scopes(self) -> tuple[str, ...]:
        """The scope names this corpus can serve, sorted. Handy for a corpus smoke test."""
        return tuple(sorted(self._scopes))

    @staticmethod
    def scope_of(request: ChatRequest) -> str:
        """``metadata["scope"]``, falling back to ``sha256`` of the first user message text.

        The fallback is what keeps an ad-hoc run deterministic: no caller has to know about the
        corpus for the same prompt to reach the same fixture. A leading ``<retrieved_context>``
        block, and the blank line after it, is stripped before hashing (ticket 083), so the same
        task reaches the same fixture whatever memory the run happened to retrieve.
        """
        scope = request.metadata.get("scope")
        if scope:
            return scope
        first_user = next((m.content for m in request.messages if m.role == "user"), "")
        if first_user.startswith(CONTEXT_OPEN) and CONTEXT_CLOSE in first_user:
            task = first_user.split(CONTEXT_CLOSE, 1)[1]
            first_user = task.removeprefix("\n").removeprefix("\n")
        return sha256(first_user.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def iteration_of(request: ChatRequest) -> int:
        """``1 + assistant messages``. Derived from the request, never from a held counter."""
        return 1 + sum(1 for message in request.messages if message.role == "assistant")

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Answer one request from the corpus, or miss loudly saying where it looked."""
        scope_name = self.scope_of(request)
        iteration = self.iteration_of(request)

        scope = self._scopes.get(scope_name)
        if scope is None:
            raise FixtureMissError(
                f"no fixture directory for scope {scope_name!r} at iteration {iteration}. "
                f"Expected {self.root / scope_name}. Add the directory with an "
                f"{INDEX_NAME}, or set metadata['scope'] to a scope the corpus declares: "
                f"{', '.join(self.scopes)}."
            )

        variant = scope.select(request)
        response = scope.responses.get((variant.name, iteration))
        if response is None:
            raise FixtureMissError(
                f"no fixture for key (scope={scope_name!r}, iteration={iteration}, "
                f"variant={variant.name!r}). Looked for "
                f"{scope.path / _filename(variant.name, iteration)}."
            )

        # The whole of the FR-53 affordance: a run that takes its time, so a browser has a window
        # in which the session is both discoverable and unfinished. Off by default, and after the
        # lookup rather than before it, so a corpus miss still fails as fast as it ever did.
        if self._latency_seconds:
            time.sleep(self._latency_seconds)
        return response


def _filename(variant: str, iteration: int) -> str:
    """The file one key maps to. Iteration 1 has no suffix, which keeps the corpus readable."""
    return f"{variant}.json" if iteration == 1 else f"{variant}.{iteration}.json"
