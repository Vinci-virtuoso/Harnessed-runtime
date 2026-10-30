"""Schedules: the ledger holds the rules, the operating system holds one fixed tick (FR-142,
decision 79).

A schedule is a row of ``schedules``: an ``every`` duration (``15m`` upward) or a five-field
``cron`` rule, the words the CLI says it in, the host's UTC offset when it was added, and
``next_due_at``. **``next_due_at`` is computed here, at write time** - on :func:`add` and after each
run in :func:`advance` - and served as stored: no reader computes a due time (G-41).

:func:`next_match` is a deterministic stdlib minute scan up to 366 days, the cron rule matched in
the schedule's own fixed offset: no DST and no ``zoneinfo`` data (MUST rule 36). The installer's
record (``schedule_installs``) is written here too; the platform half is ``tasks/installer.py``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from harnessed import clock, ids
from harnessed.errors import ParameterError, StorageError, SurfaceNotFound

__all__ = [
    "MIN_EVERY_MINUTES",
    "add",
    "advance",
    "due",
    "last_install",
    "listing",
    "listing_payload",
    "next_match",
    "record_install",
    "record_uninstall",
    "remove",
    "rows",
    "words",
]

#: ``--every`` accepts ``15m`` upward: the OS entry ticks every 15 minutes, so less is not kept.
MIN_EVERY_MINUTES: Final = 15
SCAN_DAYS: Final = 366

_EVERY: Final = re.compile(r"^(\d+)([mhd])$")
_UNIT_MINUTES: Final = {"m": 1, "h": 60, "d": 1440}
_UNIT_WORD: Final = {"m": "min", "h": "h", "d": "d"}
# (low, high) of each of the five cron fields: minute, hour, day of month, month, day of week
_RANGES: Final = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

_COLUMNS: Final = (
    "schedule_id, rule_kind, rule_value, rule_words, utc_offset_minutes, created_at, "
    "last_run_at, next_due_at"
)
_ACTIVE: Final = f"SELECT {_COLUMNS} FROM schedules WHERE removed_at IS NULL"

Row = dict[str, str | None]


def _now() -> datetime:
    return clock.now()


def _row(cursor: sqlite3.Cursor, found: tuple[object, ...]) -> Row:
    names = [column[0] for column in cursor.description]
    return {
        name: None if value is None else str(value)
        for name, value in zip(names, found, strict=False)
    }


# ---- the rule ---------------------------------------------------------------------------------


def parse_every(text: str) -> int:
    """``6h`` -> 360 minutes. Refuses anything under :data:`MIN_EVERY_MINUTES` or malformed."""
    found = _EVERY.match(text.strip())
    if found is None:
        raise ParameterError(f"--every takes a duration like 15m, 6h or 1d, not {text!r}.")
    minutes = int(found.group(1)) * _UNIT_MINUTES[found.group(2)]
    if minutes < MIN_EVERY_MINUTES:
        raise ParameterError(
            f"--every {text} is under {MIN_EVERY_MINUTES}m: the scheduler ticks every "
            f"{MIN_EVERY_MINUTES} minutes, so a shorter rule would never be kept."
        )
    return minutes


def _field(text: str, low: int, high: int, name: str) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        if step_text and (not step_text.isdigit() or int(step_text) < 1):
            raise ParameterError(f"cron {name}: the step in {part!r} must be 1 or more.")
        step = int(step_text) if step_text else 1
        if base == "*":
            start, end = low, high
        else:
            lo_text, dash, hi_text = base.partition("-")
            if not lo_text.isdigit() or (dash and not hi_text.isdigit()):
                raise ParameterError(f"cron {name}: {part!r} is not a number, range or *.")
            start = int(lo_text)
            end = int(hi_text) if dash else (high if step_text else start)
        if not low <= start <= end <= high:
            raise ParameterError(f"cron {name}: {part!r} is outside {low}-{high}.")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(text: str) -> tuple[frozenset[int], ...]:
    """The five fields of ``text`` as sets of values (day of week folded to 0-6, Sunday 0)."""
    fields = text.split()
    if len(fields) != 5:
        raise ParameterError(f"--cron takes five fields (minute hour day month weekday): {text!r}.")
    names = ("minute", "hour", "day of month", "month", "weekday")
    sets = [
        _field(field, low, high, name)
        for field, (low, high), name in zip(fields, _RANGES, names, strict=True)
    ]
    sets[4] = frozenset(day % 7 for day in sets[4])
    return tuple(sets)


def words(kind: str, value: str) -> str:
    """The rule in the CLI's own sentence: ``Every 6 h``, ``Weekdays at 07:00``."""
    if kind == "every":
        found = _EVERY.match(value)
        assert found is not None
        return f"Every {int(found.group(1))} {_UNIT_WORD[found.group(2)]}"
    minute, hour, dom, month, dow = value.split()
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*":
        clock_time = f"{int(hour):02d}:{int(minute):02d}"
        if dow == "*":
            return f"Every day at {clock_time}"
        if dow in ("1-5", "1,2,3,4,5"):
            return f"Weekdays at {clock_time}"
        if dow in ("0,6", "6,0", "6,7", "7,6"):
            return f"Weekends at {clock_time}"
    return f"Cron {value}"


def _day_matches(local: datetime, rule: tuple[frozenset[int], ...], text: str) -> bool:
    if local.month not in rule[3]:
        return False
    dom_ok = local.day in rule[2]
    dow_ok = (local.weekday() + 1) % 7 in rule[4]
    fields = text.split()
    if not fields[2].startswith("*") and not fields[4].startswith("*"):
        return dom_ok or dow_ok  # cron's own rule when both are restricted
    return dom_ok and dow_ok


def next_match(kind: str, value: str, after: datetime, offset_minutes: int) -> datetime | None:
    """The first instant strictly after ``after`` the rule fires, or ``None`` within 366 days.

    ``every`` adds its duration. ``cron`` scans minute by minute in the fixed offset (skipping a
    whole day or hour that cannot match), so the same inputs give the same answer on every host.
    """
    after = after.astimezone(UTC)
    if kind == "every":
        return after + timedelta(minutes=parse_every(value))
    rule = parse_cron(value)
    offset = timedelta(minutes=offset_minutes)
    start = (after + offset).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
    limit = start + timedelta(days=SCAN_DAYS)
    local = start
    while local <= limit:
        if not _day_matches(local, rule, value):
            local = (local + timedelta(days=1)).replace(hour=0, minute=0)
        elif local.hour not in rule[1]:
            local = (local + timedelta(hours=1)).replace(minute=0)
        elif local.minute not in rule[0]:
            local += timedelta(minutes=1)
        else:
            return (local - offset).replace(tzinfo=UTC)
    return None


# ---- the rows ---------------------------------------------------------------------------------


def add(conn: sqlite3.Connection, *, every: str | None = None, cron: str | None = None) -> Row:
    """Store one schedule, ``next_due_at`` computed now, and return its row.

    Exactly one of ``every`` and ``cron``; a rule that never fires within 366 days is refused.
    """
    if (every is None) == (cron is None):
        raise ParameterError("give exactly one of --every and --cron.")
    kind, value = ("every", every) if every is not None else ("cron", cron)
    assert value is not None
    value = value.strip()
    if kind == "every":
        parse_every(value)
    else:
        parse_cron(value)
        value = " ".join(value.split())
    offset = clock.utc_offset_minutes()
    now = _now()
    following = next_match(kind, value, now, offset)
    if following is None:
        raise ParameterError(f"the rule {value!r} never fires within {SCAN_DAYS} days.")
    schedule_id = ids.new_id("sch")
    try:
        conn.execute(
            "INSERT INTO schedules (schedule_id, rule_kind, rule_value, rule_words, "
            "utc_offset_minutes, created_at, next_due_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                schedule_id,
                kind,
                value,
                words(kind, value),
                offset,
                clock.isoformat(now),
                clock.isoformat(following),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise StorageError(f"could not store the schedule: {exc}") from exc
    return _get(conn, schedule_id)


def _get(conn: sqlite3.Connection, schedule_id: str) -> Row:
    cursor = conn.execute(f"SELECT {_COLUMNS} FROM schedules WHERE schedule_id = ?", (schedule_id,))
    found = cursor.fetchone()
    if found is None:
        raise SurfaceNotFound(f"no schedule {schedule_id!r}: the ledger holds none.")
    return _row(cursor, found)


def rows(conn: sqlite3.Connection) -> tuple[Row, ...]:
    """Every schedule not removed, oldest first."""
    cursor = conn.execute(f"{_ACTIVE} ORDER BY created_at, schedule_id")
    return tuple(_row(cursor, found) for found in cursor.fetchall())


def remove(conn: sqlite3.Connection, schedule_id: str) -> None:
    """Mark a schedule removed. A schedule that is not there, or already gone, is not found."""
    changed = conn.execute(
        "UPDATE schedules SET removed_at = ? WHERE schedule_id = ? AND removed_at IS NULL",
        (clock.isoformat(_now()), schedule_id),
    ).rowcount
    conn.commit()
    if changed == 0:
        raise SurfaceNotFound(f"no schedule {schedule_id!r}: the ledger holds none.")


def due(conn: sqlite3.Connection) -> tuple[Row, ...]:
    """The schedules whose ``next_due_at`` has come, earliest first (one indexed read)."""
    cursor = conn.execute(
        f"{_ACTIVE} AND next_due_at <= ? ORDER BY next_due_at, schedule_id",
        (clock.isoformat(_now()),),
    )
    return tuple(_row(cursor, found) for found in cursor.fetchall())


def advance(conn: sqlite3.Connection, schedule_id: str) -> Row:
    """Record that the schedule ran now and compute its next due time from now."""
    row = _get(conn, schedule_id)
    now = _now()
    following = next_match(
        str(row["rule_kind"]), str(row["rule_value"]), now, int(str(row["utc_offset_minutes"]))
    )
    conn.execute(
        "UPDATE schedules SET last_run_at = ?, next_due_at = ? WHERE schedule_id = ?",
        (
            clock.isoformat(now),
            None if following is None else clock.isoformat(following),
            schedule_id,
        ),
    )
    conn.commit()
    return _get(conn, schedule_id)


# ---- the installer's record -------------------------------------------------------------------


def last_install(conn: sqlite3.Connection) -> Row | None:
    """The newest ``schedule_installs`` row, or ``None`` when nothing was ever installed."""
    cursor = conn.execute("SELECT * FROM schedule_installs ORDER BY install_id DESC LIMIT 1")
    found = cursor.fetchone()
    return None if found is None else _row(cursor, found)


def record_install(conn: sqlite3.Connection, where: str, entry: Recorded) -> bool:
    """Record an install; ``False`` (and no row) when the same entry is already recorded live."""
    newest = last_install(conn)
    if (
        newest is not None
        and newest["uninstalled_at"] is None
        and newest["where"] == where
        and newest["entry_sha256"] == entry.fingerprint
    ):
        return False
    stamp = clock.isoformat(_now())
    conn.execute(
        "UPDATE schedule_installs SET uninstalled_at = ? WHERE uninstalled_at IS NULL",
        (stamp,),
    )
    conn.execute(
        'INSERT INTO schedule_installs ("where", entry_name, command, arguments, working_dir, '
        "entry_sha256, installed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            where,
            entry.entry_name,
            entry.python,
            entry.arguments,
            entry.working_dir,
            entry.fingerprint,
            stamp,
        ),
    )
    conn.commit()
    return True


def record_uninstall(conn: sqlite3.Connection) -> bool:
    """Mark the live install row uninstalled; ``False`` when none is live."""
    changed = conn.execute(
        "UPDATE schedule_installs SET uninstalled_at = ? WHERE uninstalled_at IS NULL",
        (clock.isoformat(_now()),),
    ).rowcount
    conn.commit()
    return changed > 0


class Recorded(Protocol):
    """What the record keeps of an entry (``installer.Rendered`` satisfies it)."""

    @property
    def entry_name(self) -> str: ...
    @property
    def python(self) -> str: ...
    @property
    def arguments(self) -> str: ...
    @property
    def working_dir(self) -> str: ...
    @property
    def fingerprint(self) -> str: ...


# ---- the schedules read -----------------------------------------------------------------------

_REASONS: Final = {
    "where": "it is not installed in the operating system's scheduler",
    "last_run_at": "it has not run yet",
    "next_due_at": "the rule does not fire within 366 days",
}


def listing(conn: sqlite3.Connection) -> tuple[Row, ...]:
    """Every active schedule with ``installed`` and ``where`` from the newest install row.

    The ledger alone: this never runs the operating system's scheduler tools.
    """
    newest = last_install(conn)
    live = newest is not None and newest["uninstalled_at"] is None
    out: list[Row] = []
    for row in rows(conn):
        row["installed"] = "true" if live else "false"
        row["where"] = newest["where"] if live and newest is not None else None
        out.append(row)
    return tuple(out)


_SERVED: Final = (
    "schedule_id",
    "rule_kind",
    "rule_value",
    "rule_words",
    "installed",
    "where",
    "last_run_at",
    "next_due_at",
)


def json_payload(row: Mapping[str, str | None]) -> dict[str, str | None]:
    """One schedule as served: every field a string or ``null`` with its ``<field>_reason``."""
    out: dict[str, str | None] = {}
    for name in _SERVED:
        out[name] = row.get(name)
        if name in _REASONS:
            out[f"{name}_reason"] = _REASONS[name] if row.get(name) is None else None
    return out


def listing_payload(found: tuple[Row, ...]) -> dict[str, object]:
    """The one wire shape of the schedules read: ``{"schedules": [...]}``."""
    return {"schedules": [json_payload(row) for row in found]}
