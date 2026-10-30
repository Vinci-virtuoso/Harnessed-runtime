"""The one place a secret is removed, before it can be stored or published (FR-13).

Redaction is the real security control in v1. A secret that reaches semantic memory is replayed
into every future system prompt, so the cheapest moment to remove it is before the first write
lands rather than after the leak is discovered. Every store ``put()`` and every event publish
calls :func:`scrub` first, with no exceptions and no "this payload is obviously safe".

Two sources of truth feed it. The pattern table below covers the shapes a credential has when it
arrives from somewhere the process never configured - a pasted key, a header echoed back by an
error body, a PEM file read by a tool. ``config.secret_values()`` covers the keys this process
actually holds, matched verbatim, so a credential with no recognisable shape is still removed.

Replacement is a stable token, ``[redacted:<kind>]``, so redacted text stays deterministic and
diffable: the same input always produces the same output, and scrubbing an already-scrubbed
string is a no-op.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final, overload

from harnessed import config

__all__ = ["KINDS", "scrub"]

# Assembled rather than written out whole: a literal that looks like a credential is what the
# repository's write guard rejects, and this module is not an exception to that rule.
_DASHES: Final = "-" * 5
_PEM = _DASHES + r"BEGIN[A-Z0-9 ]*PRIVATE KEY" + _DASHES

# Every AWS access key id prefix, per the IAM unique-identifier table. Long-term user keys are
# only the first of them; a leaked temporary session key is just as usable.
_AWS_PREFIXES = r"(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA)"

# Ordered, and the order matters: the anthropic prefix is a prefix of the openai one, so the
# narrower pattern has to win first or every Anthropic key would be labelled the wrong kind.
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{8,}")),
    ("groq_key", re.compile(r"gsk_[A-Za-z0-9_\-]{8,}")),
    ("bearer_token", re.compile(r"[Bb]earer\s+[A-Za-z0-9\-._~+/]{8,}=*")),
    ("aws_access_key_id", re.compile(_AWS_PREFIXES + r"[A-Z0-9]{16}")),
    ("private_key", re.compile(_PEM)),
)

# The kind of a value that came from the environment. It names the source rather than the
# variable, because which key is set is not information a stored payload needs to carry.
_ENV_KIND: Final = "env_secret"

KINDS: Final[frozenset[str]] = frozenset({kind for kind, _ in _PATTERNS} | {_ENV_KIND})


def token(kind: str) -> str:
    """The stable replacement for one kind of secret."""
    return f"[redacted:{kind}]"


@overload
def scrub(value: str) -> str: ...


@overload
def scrub(value: Mapping[str, object]) -> dict[str, object]: ...


@overload
def scrub(value: object) -> object: ...


def scrub(value: object) -> object:
    """Return ``value`` with every recognised secret replaced by ``[redacted:<kind>]``.

    Strings are scrubbed; mappings, lists, tuples and sets are scrubbed through, container type
    preserved; every other scalar is returned unchanged, because a payload is JSON-shaped
    (``dict[str, object]``) by contract and nothing else can carry text.
    """
    return _scrub(value, config.secret_values())


def _scrub(value: object, secrets: tuple[str, ...]) -> object:
    """The recursive half. ``secrets`` is resolved once per top-level call, not per node."""
    if isinstance(value, str):
        return _scrub_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            _scrub_text(str(key), secrets): _scrub(item, secrets) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item, secrets) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_scrub(item, secrets) for item in value)
    if isinstance(value, set):
        return {_scrub(item, secrets) for item in value}
    return value


def _scrub_text(text: str, secrets: tuple[str, ...]) -> str:
    """One string, environment values first, then the pattern table in declared order.

    The environment values go first because they are exact: a key this process actually holds is
    known to be a secret, whereas a pattern only says a string looks like one.
    """
    for secret in secrets:
        text = text.replace(secret, token(_ENV_KIND))
    for kind, pattern in _PATTERNS:
        text = pattern.sub(token(kind), text)
    return text
