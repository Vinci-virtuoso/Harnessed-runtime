"""The single time source. Nothing else in the repository calls ``datetime.now()``.

Wall-clock values never enter a comparison a test asserts on: a test that needs a fixed instant
calls :func:`freeze`, and the ``conftest.py`` fixture does it on the test's behalf.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

__all__ = [
    "freeze",
    "frozen_at",
    "isoformat",
    "now",
    "parse",
    "sleep",
    "unfreeze",
    "utc_offset_minutes",
]

_frozen: datetime | None = None


def now() -> datetime:
    """The current instant in UTC, or the frozen instant when one is set."""
    if _frozen is not None:
        return _frozen
    return datetime.now(UTC)


def sleep(seconds: float) -> None:
    """Pause the calling thread. A caller that waits takes this as an injectable default."""
    time.sleep(seconds)


def freeze(instant: datetime) -> None:
    """Pin :func:`now` to ``instant`` until :func:`unfreeze`."""
    global _frozen
    _frozen = instant.astimezone(UTC)


def unfreeze() -> None:
    """Release the pin set by :func:`freeze`."""
    global _frozen
    _frozen = None


def frozen_at() -> datetime | None:
    """The pinned instant, or ``None`` when the clock is running."""
    return _frozen


def isoformat(instant: datetime) -> str:
    """ISO-8601 UTC with a ``Z`` suffix. The only timestamp format written anywhere."""
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse(text: str) -> datetime:
    """Inverse of :func:`isoformat`."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def utc_offset_minutes() -> int:
    """The host's offset from UTC at :func:`now`, in whole minutes (a schedule stores it fixed)."""
    offset = now().astimezone().utcoffset()
    return 0 if offset is None else int(offset.total_seconds() // 60)
