"""``--evidence <path>``: a live record is written whole or not at all (ticket 231, FR-160).

The v5 live routing suite wrote its output over ``evidence/178/routing-live.txt`` and a failed
retry left an empty file there. :func:`write_whole` is the one way a live command puts evidence
on disk: the text goes to ``<path>.tmp`` and is ``os.replace``d onto ``<path>`` only when the run
completed with nothing unscored. Otherwise ``<path>`` is not touched, the partial output is kept
as ``<path>.incomplete``, and :class:`~harnessed.errors.EvidenceIncomplete` names both.

:class:`Capture` tees what a command prints on stdout into a buffer, so the file holds exactly
what the operator saw.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path
from types import TracebackType
from typing import TextIO

from harnessed.errors import EvidenceIncomplete

__all__ = ["Capture", "incomplete_path", "write_whole"]

INCOMPLETE_SUFFIX = ".incomplete"
TMP_SUFFIX = ".tmp"


def incomplete_path(path: Path) -> Path:
    """Where the partial output of a run that did not complete is kept."""
    return path.with_name(path.name + INCOMPLETE_SUFFIX)


def write_whole(path: Path, text: str, *, complete: bool) -> None:
    """Write ``text`` onto ``path`` whole, or leave ``path`` byte-identical and say so.

    Raises :class:`EvidenceIncomplete` when ``complete`` is false, after the partial text is
    kept as ``<path>.incomplete`` and any older partial replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    tmp.write_text(text, encoding="utf-8", newline="")
    if complete:
        os.replace(tmp, path)
        return
    partial = incomplete_path(path)
    os.replace(tmp, partial)
    raise EvidenceIncomplete(
        f"the run did not complete with every case scored, so {path} was left as it was; "
        f"the partial output is in {partial}."
    )


class _Tee(io.TextIOBase):
    """A stdout stand-in that writes through to the real stream and keeps a copy."""

    encoding = "utf-8"

    def __init__(self, real: TextIO, copy: io.StringIO) -> None:
        self._real = real
        self._copy = copy

    def write(self, text: str) -> int:
        self._copy.write(text)
        return self._real.write(text)

    def flush(self) -> None:
        self._real.flush()

    def isatty(self) -> bool:
        return False


class Capture:
    """Tee stdout for the length of a ``with`` block and write it as evidence at the end.

    ``complete`` starts true; a caller whose run left cases unscored sets it false. A block that
    raises keeps whatever was printed as ``<path>.incomplete`` and lets the error propagate, so a
    failing command never touches ``path``. With ``path=None`` this does nothing at all.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.complete = True
        self._copy = io.StringIO()
        self._real: TextIO | None = None

    def __enter__(self) -> Capture:
        if self.path is not None:
            self._real = sys.stdout
            sys.stdout = _Tee(self._real, self._copy)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.path is None or self._real is None:
            return
        sys.stdout = self._real
        if exc_type is not None:
            with contextlib.suppress(EvidenceIncomplete, OSError):
                write_whole(self.path, self._copy.getvalue(), complete=False)
            return
        write_whole(self.path, self._copy.getvalue(), complete=self.complete)
