"""The host seam: which operating system and Python this process runs on (decision 95, FR-180).

Also home of :class:`SeamLine`, the one value every platform seam's ``describe()`` returns and
``cli/doctor_cmd.py::platform_section()`` collects. A leaf: it imports nothing internal, so the
seams that are leaves themselves (``proc``, ``acp/jail``) may reach it.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Final

__all__ = ["SEPARATORS", "SeamLine", "describe", "fold_separators", "has_separator"]

#: Every character either OS reads as a path separator, whichever one this process runs on.
SEPARATORS: Final = ("/", "\\")


@dataclass(frozen=True, slots=True)
class SeamLine:
    """One seam's choice on this host: a named implementation, and why when it is a fallback."""

    seam: str
    implementation: str
    reason: str | None = None


def describe(
    *, system: str | None = None, release: str | None = None, python: str | None = None
) -> SeamLine:
    """The OS name and version and the Python version, through ``platform`` (never a fallback)."""
    os_name = f"{system or platform.system()} {release or platform.release()}".strip()
    return SeamLine("host", f"{os_name}, Python {python or platform.python_version()}", None)


def has_separator(text: str) -> bool:
    """Whether ``text`` holds a path separator of either OS."""
    return any(separator in text for separator in SEPARATORS)


def fold_separators(text: str) -> str:
    """``text`` with every separator of either OS a forward slash."""
    for separator in SEPARATORS[1:]:
        text = text.replace(separator, SEPARATORS[0])
    return text
