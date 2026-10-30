"""Extract the README's install blocks by marker (ticket 324; ticket 325 shares this extractor).

A block is the fenced code that follows an HTML comment marker such as ``<!-- install:windows -->``.
"""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"

OSES = ("windows", "macos", "linux")

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def block_after(text: str, marker: str) -> str:
    """The body of the first fenced block after ``<!-- marker -->``, newline-terminated."""
    tag = f"<!-- {marker} -->"
    start = text.index(tag) + len(tag)
    match = _FENCE.search(text, start)
    if match is None or text[start : match.start()].strip():
        raise ValueError(f"no fenced block directly after {tag}")
    return match.group(1)


def install_blocks(text: str) -> dict[str, str]:
    """Block 1 for each OS, keyed by OS name."""
    return {name: block_after(text, f"install:{name}") for name in OSES}


def run_block(text: str) -> str:
    """Block 2, the one shared by every OS."""
    return block_after(text, "run")
