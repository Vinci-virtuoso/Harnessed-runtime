"""Repository-root conftest: the one hook that must run whatever the target is.

``tests/conftest.py`` owns the scrub, but a conftest's hooks are only registered for the
directories pytest collects it from - so ``uv run pytest evals/deterministic`` never loaded it, and
the live eval tier stayed open for exactly the command the eval suite is run with. This file sits
at the rootdir, so it is loaded for every target, and it is the only reason the guarantee holds
process-wide rather than for one directory (ticket 056).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conftest import scrub_injected_dotenv  # noqa: E402

# Run at IMPORT, not from ``pytest_configure``. The decision recorded on ticket 056 was to
# neutralise in ``pytest_configure``, and that turned out to be too late: ``evals/conftest.py``
# imports ``evals.helpers``, which computes ``RUN_LIVE`` and ``HAS_KEY`` from ``os.environ`` at
# import time, and conftest modules are imported before the configure hook fires. Scrubbing there
# left the live tier already decided. The rootdir conftest is imported after every plugin entry
# point has loaded - which is when the injection happens - and before any other conftest, so this
# is the earliest point that is still late enough. Same boundary, earlier line.
scrub_injected_dotenv()


def pytest_configure(config: object) -> None:
    """Belt and braces: re-run after plugins, in case one injects during its own configure."""
    del config
    scrub_injected_dotenv()
