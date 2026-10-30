"""Deterministic, offline token counting (FR-11, architecture decision 6).

One word-and-punctuation split scaled by 1.3, which approximates BPE subword expansion for
English prose. Standard library only: no model file, no download, no dependency, and therefore
the identical integer on every machine running Python 3.12. That is the whole point - a budget
that could differ between two machines would not protect determinism, it would only look like it
did.

The count is an **approximation and is documented as one**. Real BPE tokenizers differ from it by
roughly plus or minus 15 percent, and considerably more on code, JSON and non-Latin scripts,
where this under-counts. The budget it feeds is a **ceiling**, and FR-11 asserts less than or
equal to that ceiling, which a more accurate counter can only make stricter.

That is what makes the v2 swap to a live tokenizer a one-file change: the provider's own counter
goes behind this exact signature and the default budget drops to 3200 to absorb the gap. The swap
is only safe while this function has exactly one caller path, ``retrieval.py::assemble``, so
there is a test asserting that no second one appeared.
"""

from __future__ import annotations

import math
import re
from typing import Final

__all__ = ["SCALE", "count_tokens"]

#: A word, or a single character of punctuation. Whitespace is a separator and never a token.
_TOKEN_RE: Final = re.compile(r"\w+|[^\w\s]", re.UNICODE)

#: The subword expansion factor. Fixed by architecture decision 6; changing it is a gate decision.
SCALE: Final = 1.3


def count_tokens(text: str) -> int:
    """The approximate token count of ``text``. Deterministic, offline, standard library only."""
    return math.ceil(len(_TOKEN_RE.findall(text)) * SCALE)
