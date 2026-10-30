"""A dropped connection, told apart from an answer, and the bounded retry that survives one.

FR-160, decision 72. The v5 live routing suite failed about one call in eight on
``SSLV3_ALERT_BAD_RECORD_MAC`` and "Remote end closed connection" while single calls passed, and
one drop voided a 28-call run. A drop is not an answer: the endpoint never spoke. So the one
thing retried is a call whose exception :func:`is_transport` recognises, at most
:data:`TRANSPORT_RETRIES` more times after :data:`BACKOFF_S`. An HTTP status, a provider error
body, a read timeout, a parse failure and a model answer are all answers (or are ambiguous about
whether the request landed), and none is retried.

Only ``cli/eval_cmd.py`` wraps anything in :class:`RetryingProvider` / :class:`RetryingDecider`,
and only for a live ``eval run``; a routed console message still falls back on a dropped
Decisions call exactly as before. This module is the one place besides the adapters that names
a vendor exception, and the vendor SDKs are imported where they are needed so an offline run
never loads one.
"""

from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Protocol, cast

from harnessed.errors import DecisionDropped, TransportDropped
from harnessed.providers.types import ChatRequest, ChatResponse

if TYPE_CHECKING:  # pragma: no cover - ``jev`` imports this module for ``is_transport``
    from harnessed.providers.jev import Decider, Decisions, Question

__all__ = [
    "BACKOFF_S",
    "HTTPException",
    "TRANSPORT_RETRIES",
    "OnRetry",
    "RetryingDecider",
    "RetryingProvider",
    "drop_name",
    "dropped",
    "is_transport",
]

#: At most this many more calls after the first (gate ruling 2026-10-03).
TRANSPORT_RETRIES: Final = 2

#: The wait before retry one, then retry two.
BACKOFF_S: Final[tuple[float, ...]] = (1.0, 3.0)

_DROPS: Final[tuple[type[BaseException], ...]] = (
    ssl.SSLError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
)


#: What a half-read response body raises besides an ``OSError``: ``http.client``'s own errors are
#: ``HTTPException``s, which a caller reading a body catches to hand to :func:`is_transport`.
#: Exported so ``providers/jev.py`` need not import ``http.client`` itself.
HTTPException = http.client.HTTPException


class OnRetry(Protocol):
    """Told about each retry just before its wait: ``call`` is ``chat`` or ``decision``,
    ``attempt`` the retry's number from 1, ``error_class`` the drop's name."""

    def __call__(self, call: str, attempt: int, error_class: str, backoff_s: float) -> None: ...


def _sdk_types() -> tuple[tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
    """The SDK connection errors, and the SDK status errors, for whichever SDKs are installed."""
    connection: list[type[BaseException]] = []
    status: list[type[BaseException]] = []
    try:
        import openai

        connection.append(openai.APIConnectionError)
        status.append(openai.APIStatusError)
    except ImportError:  # pragma: no cover - the SDKs are declared dependencies
        pass
    try:
        import anthropic

        connection.append(anthropic.APIConnectionError)
        status.append(anthropic.APIStatusError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(connection), tuple(status)


def _is_connect_timeout(exc: BaseException) -> bool:
    """httpx's ``ConnectTimeout``, matched by name: httpx is the SDKs' transitive dependency and
    nothing under ``src/`` imports it (``tests/test_gateway_app.py``)."""
    return any(cls.__name__ == "ConnectTimeout" for cls in type(exc).__mro__)


def _drop(exc: BaseException) -> BaseException | None:
    """The exception in ``exc``'s chain that is the lost connection, or ``None``."""
    connection, status = _sdk_types()
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (urllib.error.HTTPError, *status)):
            return None
        if isinstance(current, _DROPS):
            return current
        if isinstance(current, urllib.error.URLError):
            reason = current.reason
            if isinstance(reason, TimeoutError):
                return reason
            if isinstance(reason, BaseException) and (found := _drop(reason)) is not None:
                return found
        elif connection and isinstance(current, connection):
            if type(current).__name__ == "APITimeoutError":
                cause = current.__cause__
                return cause if cause is not None and _is_connect_timeout(cause) else None
            return current.__cause__ or current
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return None


def is_transport(exc: BaseException) -> bool:
    """True when ``exc`` (or anything it was raised from or while handling) is a lost connection.

    Walks ``__cause__`` and ``__context__``. A status, a provider error body, a read timeout, a
    parse failure and a model answer are false; a bare ``TimeoutError`` is a read timeout, a
    ``TimeoutError`` inside a ``URLError`` or an httpx connect timeout is a connect timeout.
    """
    return _drop(exc) is not None


def drop_name(exc: BaseException) -> str:
    """The class name of the drop inside ``exc``, for the ledger and the unscored list."""
    found = _drop(exc)
    return type(exc if found is None else found).__name__


def dropped(provider: str, exc: BaseException) -> TransportDropped:
    """The :class:`TransportDropped` an adapter raises for a connection error ``is_transport``
    accepted, naming the provider and the underlying drop, never the request."""
    name = drop_name(exc)
    return TransportDropped(f"the connection to {provider!r} dropped: {name}.", error_class=name)


def _retrying(
    call: str,
    attempt_call: Callable[[], object],
    dropped: type[TransportDropped] | type[DecisionDropped],
    on_retry: OnRetry | None,
    sleep: Callable[[float], None],
) -> object:
    attempts = 0
    while True:
        attempts += 1
        try:
            return attempt_call()
        except (TransportDropped, DecisionDropped) as exc:
            if attempts > TRANSPORT_RETRIES:
                raise dropped(
                    str(exc), error_class=getattr(exc, "error_class", ""), attempts=attempts
                ) from exc
            backoff = BACKOFF_S[attempts - 1]
            if on_retry is not None:
                on_retry(call, attempts, getattr(exc, "error_class", ""), backoff)
            sleep(backoff)


class RetryingProvider:
    """A ``Provider`` that retries a :class:`TransportDropped` from the one it wraps."""

    def __init__(
        self,
        inner: object,
        *,
        on_retry: OnRetry | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._on_retry = on_retry
        self._sleep = sleep

    def complete(self, request: ChatRequest) -> ChatResponse:
        result = _retrying(
            "chat",
            lambda: self._inner.complete(request),  # type: ignore[attr-defined]
            TransportDropped,
            self._on_retry,
            self._sleep,
        )
        return cast(ChatResponse, result)


class RetryingDecider:
    """A ``Decider`` that retries a :class:`DecisionDropped` from the one it wraps."""

    def __init__(
        self,
        inner: Decider,
        *,
        on_retry: OnRetry | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._on_retry = on_retry
        self._sleep = sleep

    def decide(self, state: str, questions: tuple[Question, ...]) -> Decisions:
        result = _retrying(
            "decision",
            lambda: self._inner.decide(state, questions),
            DecisionDropped,
            self._on_retry,
            self._sleep,
        )
        return cast("Decisions", result)
