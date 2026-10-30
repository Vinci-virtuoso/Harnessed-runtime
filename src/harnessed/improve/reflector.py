"""The reflector: failing cases in, one candidate out (FR-17).

An eval failure is the only thing that starts a reflection, and the case ids of those failures
are the only thing that distinguishes one reflection from another. That is not a style
preference; it is what keeps the FR-25 proof honest. ``FakeProvider`` selects its fixture on
observable properties of the request, and the only property a predicate may read is the system
prompt (``architecture.md``, THE CRUX), so **the failing case ids are carried in the system
prompt**. If the baseline run did not actually fail the case, its id is not in the prompt, the
authored variant does not match, ``base`` answers with empty content and there is no candidate.
The authored candidate stays gated on a genuinely observed failure.

**What it is allowed to look at is an argument, not a lookup** (FR-19). ``reflect()`` takes
``visible``, the complete set of case ids this reflection may read, and refuses any failure
outside it before a request is built. That is what makes the blindness structural: this module
imports nothing that can resolve a suite by name, opens no file, and has no way to learn that a
set of cases it was not given exists at all.

Everything else about this module is deliberately absent:

* **No fixture special case.** The reflection is an ordinary
  :class:`~harnessed.providers.types.ChatRequest` with ``metadata["scope"] = "_reflect"``, the
  same shape the retrieval gate uses for ``_gate``. Nothing in ``providers/`` knows this module
  exists.
* **No error absorption.** A provider that raises ends the reflection. There is no fail-open
  reading of FR-17 - a reflection that silently produced nothing would look exactly like a
  reflection that found nothing to say, and those are different facts. An answer that fails
  candidate validation is a third fact and is handled as a null reflection, not a crash: the
  provider answered, so there is no provider error to absorb, only a candidate to refuse.
* **No second question.** One request, one answer. A reflector that could iterate would make
  the candidate a function of how many times it was asked rather than of what failed.
* **No score, no ranking, no comparison.** Whether the candidate is any good is
  ``improve/promoter.py``'s question, decided with arithmetic and zero model calls.

An empty answer is a null reflection, not an error: it is how the corpus says "no candidate for
these failures", and ``harnessed improve run --dry-run`` reports exactly that.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from typing import Final, Protocol

from harnessed.errors import ImproveError, InvalidCandidateError
from harnessed.evals.runner import CaseResult
from harnessed.improve import candidate as candidate_module
from harnessed.improve.candidate import Candidate, Publish
from harnessed.providers.types import ChatRequest, ChatResponse, Message

__all__ = [
    "MAX_TOKENS",
    "REFLECTION_WRITTEN",
    "REFLECT_SCOPE",
    "Model",
    "reflect",
]

#: The fixture scope every reflection request declares. FakeProvider keys selection on it.
REFLECT_SCOPE: Final = "_reflect"

#: Published once per completed reflection, whether or not it produced a candidate.
REFLECTION_WRITTEN: Final = "improve.reflection.written"

#: A procedure is a sentence or two. A ceiling this low is also what keeps it append-only in
#: practice: there is no room for a schema, and a truncated one would fail validation anyway.
MAX_TOKENS: Final = 512

_SYSTEM_PREAMBLE: Final = (
    "You improve an agent by writing one short procedure it can append to its system prompt. "
    "The agent just failed the eval cases listed below. Write plain prose that tells the agent "
    "what to do differently, in at most three sentences. Append-only means append-only: no "
    "code, no tool declarations, no settings the operator owns. If the failures do not suggest "
    "a procedure, answer with nothing at all."
)

_USER_INSTRUCTION: Final = (
    "Write the procedure, or answer with nothing if these failures do not suggest one."
)


class Model(Protocol):
    """What the reflector knows about a provider: it turns one request into one response.

    Structurally identical to ``providers.base.Provider``, and restated here rather than
    imported for the same reason ``loop/engine.py`` and ``memory/gate.py`` restate it: the
    import table admits ``providers.types`` and nothing else under ``providers/``. Structural
    typing makes the duplication free - every adapter satisfies both.
    """

    def complete(self, request: ChatRequest) -> ChatResponse: ...


def reflect(
    model: Model,
    failures: Sequence[CaseResult],
    conn: sqlite3.Connection,
    *,
    visible: Collection[str],
    model_name: str,
    provenance: str = "",
    publish: Publish | None = None,
) -> Candidate | None:
    """Ask once about ``failures``, and write the answer as a candidate when there is one.

    ``visible`` is the complete set of case ids this reflection is permitted to see - in practice
    the ids of the suite the caller just ran. It is required and it is an allowlist: a failure
    whose id is not in it never reaches the request. See :func:`_admit` for why that is a refusal
    rather than a filter.

    Returns ``None`` when there is nothing to reflect on and when the answer is empty. Both are
    the same fact from the caller's side - no candidate - and neither is an error.
    """
    cases = candidate_module.sources(_admit(failures, visible))
    if not cases:
        return None

    request = _request(failures, cases, model_name=model_name)
    response = model.complete(request)
    text = response.content.strip()

    if publish is not None:
        publish(
            REFLECTION_WRITTEN,
            {
                "scope": REFLECT_SCOPE,
                "source_case_ids": list(cases),
                "produced_candidate": bool(text),
            },
        )
    if not text:
        return None
    try:
        return candidate_module.write(conn, text, cases, provenance=provenance, publish=publish)
    except InvalidCandidateError:
        # The provider answered, but not with append-only prose. That is a null reflection, not a
        # provider failure (there is none to absorb) and not a crash: the same "no candidate" an
        # empty answer produces, so one over-broad phrase cannot abort the whole cycle and every
        # resume of it (finding 6).
        return None


def _admit(failures: Sequence[CaseResult], visible: Collection[str]) -> list[str]:
    """The case ids of ``failures``, or an error naming the first one that was not permitted.

    An allowlist rather than a denylist: this module is given what it may look at and cannot name
    what it may not, which is the whole of FR-19's blindness expressed as an argument. Nothing
    here resolves a suite, opens a file or knows how many suites exist.

    The refusal is loud on purpose. Silently dropping an id the caller should never have handed
    over would turn a composition bug into a quieter reflection, and a quieter reflection looks
    exactly like a smaller set of failures. Those are different facts.
    """
    permitted = set(visible)
    for result in failures:
        if result.case_id not in permitted:
            raise ImproveError(
                f"reflection was handed the case id {result.case_id!r}, which is outside the "
                f"{len(permitted)} id(s) it may see. A reflection may only read the results of "
                f"the suite it was run for (FR-19); the caller must filter before it asks."
            )
    return [result.case_id for result in failures]


def _request(
    failures: Sequence[CaseResult], cases: Sequence[str], *, model_name: str
) -> ChatRequest:
    """The one request a reflection issues.

    The failing case ids sit in ``system`` because that is the only field a fixture predicate
    may read, which is what makes the authored variant conditional on a real failure rather
    than on an identity the system assigned itself.
    """
    return ChatRequest(
        model=model_name,
        messages=(Message(role="user", content=_USER_INSTRUCTION),),
        system=_system_prompt(failures, cases),
        max_tokens=MAX_TOKENS,
        metadata={"scope": REFLECT_SCOPE},
    )


def _system_prompt(failures: Sequence[CaseResult], cases: Sequence[str]) -> str:
    """The preamble, then one line per failing case, ordered by case id."""
    worst = {result.case_id: result for result in sorted(failures, key=_by_score)}
    lines = [f"- {case_id}: {_describe(worst[case_id])}" for case_id in cases if case_id in worst]
    return "\n".join([_SYSTEM_PREAMBLE, "", "Failing cases:", *lines])


def _by_score(result: CaseResult) -> float:
    """Sort worst last, so the dictionary above keeps the lowest score per case id."""
    return -result.score


def _describe(result: CaseResult) -> str:
    """One failing case in one line: what it scored and what the graders said."""
    detail = " ".join(result.detail.split())
    return f"scored {result.score:.2f}. {detail}".strip()
