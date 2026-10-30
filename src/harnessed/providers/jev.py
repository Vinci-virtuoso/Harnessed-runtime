"""The Decider seam: one module owns the Decisions API (decision 57, FR-94).

Everything the router needs to know about TypeSafe's Jev lives here: the URL, the ``alpha``
path, the request shape and the answer shape. Nothing above this module - not ``loop/``, not
``router/`` once ticket 160 creates it - sees any of that; it sees :class:`Decisions`,
:class:`Noul`, :class:`Choice` and :class:`Score`, the internal truth.

The transport is one ``POST`` with stdlib ``urllib.request`` (research.md's phase 0 spike,
measured 2026-09-25): no TypeSafe SDK, no new dependency (MUST rule 9). ``JevDecider`` takes its
opener as a constructor argument so a test can swap it for a recorded double one layer below
``urlopen``, the way ``providers/pricing.py`` does, and never open a socket (MUST rule 18).

**Refuse, never guess.** A response this module cannot parse with confidence - a missing answer,
a choice naming an option nobody asked for, a probability outside ``[0, 1]``, or usage with no
reported cost - is a :class:`DecisionRefused` naming the fault, not a best-effort route. Only a
:class:`DecisionError` falls back to Groq chat (decision 59); anything else is a bug in this
module, not routing policy upstream of it.

The state a caller hands to :meth:`JevDecider.decide` arrives already scrubbed: ``providers/`` may
not import ``memory/``, so the redaction pass is ``router/state.py``'s job (ticket 160), not this
module's.
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Literal, Protocol
from urllib.request import Request, urlopen

from harnessed.config import Settings
from harnessed.errors import (
    DecisionDropped,
    DecisionRefused,
    DecisionTimeout,
    DecisionUnreachable,
)
from harnessed.providers.transport import HTTPException, drop_name, is_transport

__all__ = [
    "DECISIONS_URL",
    "TIMEOUT_S",
    "Choice",
    "Decider",
    "Decisions",
    "JevDecider",
    "Noul",
    "Opener",
    "Question",
    "QuestionKind",
    "Score",
    "build_decider",
    "parse",
    "request_body",
]

#: The one endpoint this module ever names, its ``alpha`` path included (research.md, decision 57).
DECISIONS_URL: Final = "https://openrouter.ai/api/alpha/decisions"

#: How long a decision call may take before it is a timeout, not a route (decision 59).
TIMEOUT_S: Final = 3.0

QuestionKind = Literal["noul", "choice", "score"]


class _Readable(Protocol):
    def __enter__(self) -> _Readable: ...

    def __exit__(self, *exc_info: object) -> None: ...

    def read(self) -> bytes: ...


class Opener(Protocol):
    """What the transport is handed, and what it must give back: a context manager over the
    response body, exactly like ``urlopen`` and ``providers/pricing.py``'s test double.

    A ``Protocol`` rather than a bare ``Callable`` because ``urlopen``'s real signature takes
    ``timeout`` by keyword after a ``data`` parameter this module never passes; the keyword-only
    ``timeout`` here is what makes the default (``urlopen`` itself) and the test double agree on
    calling convention without either naming ``data``.
    """

    def __call__(self, request: Request, *, timeout: float) -> _Readable: ...


@dataclass(frozen=True, slots=True)
class Question:
    """One question in a Decisions call: its name, its kind, its options, what it asks and what
    each option means.

    ``options`` is empty for ``noul``. A ``choice`` question's options are the only values an
    answer may name (research.md's per-option probabilities), and a value outside them is refused
    rather than accepted as a route nobody offered. A ``score`` question's options are its levels,
    least to most, each already written as the description the API scores against.

    ``instructions`` is the question itself and ``criteria`` a choice option's description, sent
    on the wire as the question's ``criteria`` object (ticket 195, the recorded request in
    ``.spec/operator-inputs/195/``). An option with no entry in ``criteria`` is sent as ``null``,
    which the API accepts.
    """

    name: str
    kind: QuestionKind
    options: tuple[str, ...] = ()
    instructions: str = ""
    criteria: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Noul:
    """A yes/no question's answer: a bare probability in ``[0, 1]`` (research.md)."""

    probability: Decimal


@dataclass(frozen=True, slots=True)
class Choice:
    """A multiple-choice answer: the picked option, its confidence, and every option's odds."""

    value: str
    confidence: Decimal
    probabilities: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class Score:
    """A legend-scored answer: the label of the level the API scored, its confidence, and the
    legend (level index to label) it was drawn from."""

    value: str
    confidence: Decimal
    legend: dict[str, str]
    probabilities: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class Decisions:
    """One Decisions call's whole answer: the typed answers by question name, beside the call's
    own identity and cost. ``usage`` and ``cost`` are the exact reported text - never rounded, never
    recomputed (decision 57)."""

    answers: dict[str, Noul | Choice | Score]
    id: str
    model: str
    usage: dict[str, object]
    cost: str


class Decider(Protocol):
    """The whole seam. ``FakeDecider`` (ticket 155) and :class:`JevDecider` are its only members."""

    def decide(self, state: str, questions: tuple[Question, ...]) -> Decisions:
        """Answer every question in ``questions`` against ``state``. Raises a child of
        :class:`~harnessed.errors.DecisionError` and nothing else."""
        ...


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _question_payload(question: Question) -> dict[str, object]:
    """One entry of the request's ``questions`` object (ticket 195): ``type`` and
    ``instructions``, plus ``criteria`` - an option-to-description object for a ``choice``, the
    ordered level list for a ``score``, nothing for a ``noul``."""
    payload: dict[str, object] = {"type": question.kind, "instructions": question.instructions}
    if question.kind == "choice":
        payload["criteria"] = {option: question.criteria.get(option) for option in question.options}
    elif question.kind == "score":
        payload["criteria"] = list(question.options)
    return payload


def request_body(model: str, state: str, questions: tuple[Question, ...]) -> dict[str, object]:
    """The whole Decisions request body: ``questions`` is an object keyed by question name."""
    return {
        "model": model,
        "state": state,
        "questions": {question.name: _question_payload(question) for question in questions},
    }


class JevDecider:
    """The one live transport: a ``POST`` to :data:`DECISIONS_URL` (decision 57, MUST rule 3)."""

    def __init__(
        self,
        model: str | None,
        settings: Settings,
        *,
        opener: Opener = urlopen,  # type: ignore[assignment]
    ) -> None:
        # `None` reads the pinned setting (decision 57, FR-95) rather than defaulting to a
        # literal here, which is how a second, driftable copy of the pin would appear.
        self._model = model if model is not None else settings.router_model
        self._settings = settings
        self._opener = opener

    def decide(self, state: str, questions: tuple[Question, ...]) -> Decisions:
        key = self._settings.require_api_key("openrouter")
        body = json.dumps(request_body(self._model, state, questions)).encode("utf-8")
        request = Request(DECISIONS_URL, data=body, method="POST", headers=_headers(key))
        try:
            with self._opener(request, timeout=TIMEOUT_S) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise DecisionRefused(
                f"the Decisions API refused the request at {DECISIONS_URL}: "
                f"the endpoint answered {exc.code}."
            ) from exc
        except TimeoutError as exc:
            raise DecisionTimeout(f"the Decisions API did not answer within {TIMEOUT_S}s.") from exc
        except (urllib.error.URLError, OSError, HTTPException) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise DecisionTimeout(
                    f"the Decisions API did not answer within {TIMEOUT_S}s."
                ) from exc
            if is_transport(exc):
                name = drop_name(exc)
                raise DecisionDropped(
                    f"the connection to the Decisions API dropped: {name}.", error_class=name
                ) from exc
            raise DecisionUnreachable(
                f"could not reach the Decisions API at {DECISIONS_URL}: {exc}."
            ) from exc
        return parse(raw, questions)


class _Number(str):
    """A JSON number's own text, kept by ``json.loads(parse_float=...)`` so a reported cost is
    never passed through a float (``2.373e-05`` stays ``2.373e-05``)."""


def _probability(where: str, raw: object) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise DecisionRefused(f"{where} is missing a probability.")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise DecisionRefused(f"{where}'s probability {raw!r} is not a number.") from exc
    if value < 0 or value > 1:
        raise DecisionRefused(f"{where}'s probability {value} is outside [0, 1].")
    return value


def _probabilities(where: str, raw: object) -> dict[str, Decimal]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DecisionRefused(f"{where}'s probabilities are not an object.")
    return {
        str(option): _probability(f"{where}'s option {option!r}", value)
        for option, value in raw.items()
    }


def _parse_noul(name: str, raw: dict[str, object]) -> Noul:
    return Noul(probability=_probability(f"{name!r}", raw.get("noul")))


def _parse_choice(question: Question, raw: dict[str, object]) -> Choice:
    value = raw.get("choice")
    if not isinstance(value, str) or (question.options and value not in question.options):
        raise DecisionRefused(
            f"{question.name!r} answered {value!r}, an option not asked "
            f"(asked: {', '.join(question.options)})."
        )
    return Choice(
        value=value,
        confidence=_probability(f"{question.name!r}", raw.get("confidence")),
        probabilities=_probabilities(f"{question.name!r}", raw.get("probabilities")),
    )


def _level(question: Question, raw: object) -> str:
    """The level index a ``score`` names, as the legend's key. The API reports the
    probability-weighted position, so a fractional score is taken to its nearest level."""
    if isinstance(raw, bool) or not isinstance(raw, int | _Number):
        raise DecisionRefused(f"{question.name!r}'s score answer has no score.")
    try:
        level = Decimal(str(raw)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise DecisionRefused(f"{question.name!r}'s score {raw!r} is not a number.") from exc
    return str(int(level))


def _parse_score(question: Question, raw: dict[str, object]) -> Score:
    legend = raw.get("legend")
    if not isinstance(legend, dict):
        raise DecisionRefused(f"{question.name!r}'s score answer has no legend.")
    level = _level(question, raw.get("score"))
    if level not in legend:
        raise DecisionRefused(f"{question.name!r} scored level {level}, which its legend lacks.")
    return Score(
        value=str(legend[level]),
        confidence=_probability(f"{question.name!r}", raw.get("confidence")),
        legend={str(label): str(text) for label, text in legend.items()},
        probabilities=_probabilities(f"{question.name!r}", raw.get("probabilities")),
    )


def _parse_answer(question: Question, raw: object) -> Noul | Choice | Score:
    if not isinstance(raw, dict):
        raise DecisionRefused(f"{question.name!r}'s answer is not an object.")
    if question.kind == "noul":
        return _parse_noul(question.name, raw)
    if question.kind == "choice":
        return _parse_choice(question, raw)
    return _parse_score(question, raw)


def parse(raw: bytes, questions: tuple[Question, ...]) -> Decisions:
    """A Decisions response body into :class:`Decisions`. Pure: the offline tests call it directly
    on a recorded fixture, and :meth:`JevDecider.decide` calls it on what the opener read."""
    try:
        payload = json.loads(raw.decode("utf-8"), parse_float=_Number)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DecisionRefused("the Decisions API answered with a body that is not JSON.") from exc
    if not isinstance(payload, dict):
        raise DecisionRefused("the Decisions API answered with a JSON value that is not an object.")
    response_id = payload.get("id")
    model = payload.get("model")
    if not isinstance(response_id, str) or not isinstance(model, str):
        raise DecisionRefused("the Decisions API answered with no id or no model.")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise DecisionRefused("the Decisions API answered with no usage.cost.")
    cost = usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, int | _Number):
        raise DecisionRefused("the Decisions API answered with no usage.cost.")
    answers_raw = payload.get("answers")
    if not isinstance(answers_raw, dict):
        raise DecisionRefused("the Decisions API answered with no answers object.")
    answers: dict[str, Noul | Choice | Score] = {}
    for question in questions:
        if question.name not in answers_raw:
            raise DecisionRefused(f"the Decisions API gave no answer for {question.name!r}.")
        answers[question.name] = _parse_answer(question, answers_raw[question.name])
    exact = {
        key: Decimal(value) if isinstance(value, _Number) else value for key, value in usage.items()
    }
    return Decisions(answers=answers, id=response_id, model=model, usage=exact, cost=str(cost))


def build_decider(settings: Settings, *, model: str | None = None) -> Decider | None:
    """``HARNESSED_PROVIDER=fake`` builds :class:`~harnessed.providers.fake_decider.FakeDecider`;
    every other provider builds the live :class:`JevDecider` (FR-96, decision 57's Offline
    paragraph) when the routing key resolves, and ``None`` when it does not: routing is off
    (ticket 316, FR-175, decision 105). The chat provider's own name is reused as the offline
    toggle here, because the
    Decider seam always talks to the one Decisions endpoint regardless of which chat provider a
    run uses - ``fake`` is the only value that means "no network" on either seam.

    Imported here rather than at module load: ``fake_decider.py`` imports :class:`Question`,
    :class:`Decisions` and :func:`parse` from this module, and importing it back at the top of
    this file would be a cycle. By the time a caller reaches this function, this module has
    already finished executing, so the import below resolves cleanly.
    """
    from harnessed.providers.fake_decider import FakeDecider

    if settings.require_provider() == "fake":
        return FakeDecider(settings)
    if not settings.routing_on():
        return None
    return JevDecider(model, settings)
