"""The one in-process event bus (FR-21).

Publish a name from :mod:`harnessed.events.names` with a JSON-shaped payload; every
subscribed observer sees it, and a row lands in the ``events`` table keyed ``(run_id, seq)``.
This is the seam v2's SSE gateway attaches to, which is why it is built in phase 2 and kept
this small: an SSE frame is one event, its ``event:`` field is the name and its ``data:`` field
is the payload.

Three guarantees hold at the publish boundary rather than at any call site.

* **The bus stamps the envelope.** ``run_id``, ``ts`` and a monotonic per-run ``seq`` come from
  here, never from a publisher, so ordering is a property of the bus and not of a convention.
  A payload that supplies one of those keys is an :class:`EventError`, not a silent overwrite.
* **The bus redacts.** Every payload passes ``redact.scrub()`` before it reaches an observer or
  the table (All AI agents MUST rule 7). An observer is arbitrary code and the table outlives
  the run, so this is the last honest moment to remove a credential.
* **The bus isolates observers.** This module holds the only broad ``except`` in the repository,
  around the observer invocation and nowhere else. A failing observer is reported as
  ``observer.failed`` rather than swallowed, and the run continues.

**One deliberate crossing of the import table.** The plan draws ``events -> config``, but the
same plan maps FR-13 onto this file (every publish is redacted) and FR-21 onto ``001_init.sql``
(every publish is a row), and both need a module under ``memory/``. The crossing is exactly
``memory.db`` and ``memory.redact``, neither of which imports ``events`` back, and
``tests/test_events.py`` enumerates it so it cannot widen unnoticed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from harnessed import clock, ids
from harnessed.errors import EventError, UnknownEventError
from harnessed.events.names import NAMES, OBSERVER_FAILED
from harnessed.memory import db, redact

__all__ = ["RESERVED_KEYS", "Event", "EventBus", "Observer"]

#: The envelope fields the bus owns. A publisher supplying one of them is a bug, not an override.
RESERVED_KEYS: Final[frozenset[str]] = frozenset({"run_id", "seq", "ts"})

_INSERT: Final = (
    "INSERT INTO events (run_id, seq, name, payload, created_at) VALUES (?, ?, ?, ?, ?)"
)
_LAST_SEQ: Final = "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?"


@dataclass(frozen=True, slots=True)
class Event:
    """One published event: the frozen name plus the envelope the bus stamped on it."""

    name: str
    run_id: str
    seq: int
    ts: str
    payload: Mapping[str, object] = field(default_factory=dict)


#: An observer is any callable taking one :class:`Event`. Its return value is ignored and its
#: failure is isolated, so an observer can never be load-bearing for a run.
Observer = Callable[[Event], None]


class EventBus:
    """One run's events. ``seq`` is monotonic within the bus, which is what makes it per-run."""

    def __init__(self, run_id: str | None = None, conn: sqlite3.Connection | None = None) -> None:
        """A bus for a new run, or - handed a ``run_id`` the store already holds - for that run
        continued: ``seq`` picks up after its last event, so a session's follow-up publishes on
        the same id without a gap or a clash (FR-80, decision 51)."""
        self.run_id = ids.new_run_id() if run_id is None else run_id
        self._conn = conn
        self._observers: list[Observer] = []
        self._seq = 0
        if conn is not None and run_id is not None:
            self._seq = int(conn.execute(_LAST_SEQ, (run_id,)).fetchone()[0])
        self._reporting_failure = False

    def subscribe(self, observer: Observer) -> Observer:
        """Register ``observer``. It sees every event published after this call, in order."""
        self._observers.append(observer)
        return observer

    def publish(self, name: str, payload: Mapping[str, object] | None = None) -> Event:
        """Stamp, redact, persist and deliver one event.

        Raises :class:`UnknownEventError` when ``name`` is outside the frozen taxonomy, so a
        typo fails a test rather than vanishing, and :class:`EventError` when the payload tries
        to supply a field the bus owns.
        """
        if name not in NAMES:
            raise UnknownEventError(
                f"event name {name!r} is not in the frozen taxonomy. Add it to "
                f"events/names.py in the same commit as this publish, and only if the "
                f"<domain>.<noun>.<past-tense-verb> scheme still holds."
            )
        supplied = sorted(RESERVED_KEYS.intersection(payload or {}))
        if supplied:
            raise EventError(
                f"event {name!r} supplied {', '.join(supplied)} in its payload. The bus stamps "
                f"{', '.join(sorted(RESERVED_KEYS))} and a publisher never does."
            )

        self._seq += 1
        event = Event(
            name=name,
            run_id=self.run_id,
            seq=self._seq,
            ts=clock.isoformat(clock.now()),
            payload=redact.scrub(dict(payload or {})),
        )
        self._persist(event)
        self._notify(event)
        return event

    def _persist(self, event: Event) -> None:
        """Write one row through the single store write entry point, when there is a store."""
        if self._conn is None:
            return
        body = json.dumps(event.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        db.write(self._conn, ((_INSERT, (event.run_id, event.seq, event.name, body, event.ts)),))

    def _notify(self, event: Event) -> None:
        """Hand the event to every observer, isolating each one's failure from the others."""
        for observer in tuple(self._observers):
            try:
                observer(event)
            except Exception as exc:
                # The only broad catch in the repository. An observer is arbitrary code and the
                # run does not depend on it, so its failure becomes an event rather than an exit.
                self._observer_failed(observer, event, exc)

    def _observer_failed(self, observer: Observer, event: Event, exc: Exception) -> None:
        """Publish ``observer.failed``, once, without recursing when the report itself fails."""
        if self._reporting_failure:
            return
        self._reporting_failure = True
        try:
            self.publish(
                OBSERVER_FAILED,
                {
                    "observer": _describe(observer),
                    "event": event.name,
                    "event_seq": event.seq,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
            )
        finally:
            self._reporting_failure = False


def _describe(observer: Observer) -> str:
    """A stable, human-readable name for an observer: a function's, or its class's."""
    named = getattr(observer, "__qualname__", None)
    return named if isinstance(named, str) else type(observer).__name__
