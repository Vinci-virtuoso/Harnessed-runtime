"""The environment as it was before any pytest plugin was imported (ticket 056).

Loaded via ``-p envsnapshot`` in ``addopts``, which pytest imports during pre-parse, ahead of the
setuptools entry-point plugins. ``deepeval``'s plugin calls python-dotenv on import, so anything
this module does not see is something a plugin put there - which is the only reliable way to tell
an operator's deliberate export from an injected one when both carry the same value.
"""

from __future__ import annotations

import os
from typing import Final

#: A copy, taken at import. Never mutated afterwards.
PRE_PLUGIN_ENV: Final[dict[str, str]] = dict(os.environ)
