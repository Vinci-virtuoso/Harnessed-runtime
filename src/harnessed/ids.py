"""The single id generator. Nothing anywhere else builds an identifier inline.

Two kinds only:

* **Opaque run ids** - ``run_`` plus 26 Crockford-base32 characters, monotonic within a process
  and seedable for tests. The leading 10 characters encode a counter rather than a timestamp,
  because ``ids`` is a leaf module and may not import :mod:`harnessed.clock`; a counter gives
  the monotonicity the ordering depends on without a second time source.
* **Content hashes** - ``<prefix>_`` plus the first 16 hex characters of a sha256 over canonical
  JSON, so the same content always yields the same id.
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets

__all__ = ["content_hash", "new_id", "new_run_id", "seed"]

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O or U.
_COUNTER_CHARS = 10
_RANDOM_CHARS = 16

_counter = 0
_rng: random.Random | None = None


def seed(value: int | None) -> None:
    """Make id generation deterministic (an int) or return it to entropy (``None``)."""
    global _counter, _rng
    _counter = 0
    _rng = None if value is None else random.Random(value)


def _encode(number: int, width: int) -> str:
    digits = []
    for _ in range(width):
        number, remainder = divmod(number, len(_ALPHABET))
        digits.append(_ALPHABET[remainder])
    return "".join(reversed(digits))


def _random_body() -> str:
    if _rng is None:
        return "".join(secrets.choice(_ALPHABET) for _ in range(_RANDOM_CHARS))
    return "".join(_rng.choice(_ALPHABET) for _ in range(_RANDOM_CHARS))


def new_id(prefix: str) -> str:
    """An opaque, monotonic, prefixed identifier."""
    global _counter
    _counter += 1
    return f"{prefix}_{_encode(_counter, _COUNTER_CHARS)}{_random_body()}"


def new_run_id() -> str:
    """The id of one run. ``run_`` plus 26 characters."""
    return new_id("run")


def content_hash(prefix: str, payload: object) -> str:
    """``<prefix>_<first 16 hex of sha256>`` over the canonical JSON of ``payload``."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:16]}"
