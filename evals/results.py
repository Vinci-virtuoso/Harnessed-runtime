"""Where an eval run's results land. The only module in this suite that names that path.

The path is resolved from ``Settings.home`` on every call, never from a literal, so
``HARNESSED_HOME`` moves it and a test can point one run at a ``tmp_path`` without patching
anything here. ``.harnessed/`` is gitignored, so results are evidence rather than repository
content, and nothing an eval writes ever needs reviewing in a diff.

One file per run, named for the run's own ``eval_`` id. A second run **adds** a file; it does not
overwrite the first. Results whose predecessor is gone cannot show a change, and showing a change
is the only reason to keep them.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from harnessed import config, ids

__all__ = [
    "RESULTS_DIRNAME",
    "SUMMARY_FILENAME",
    "TIERS",
    "CaseResult",
    "ResultError",
    "results_root",
    "write",
]

#: The directory, under ``HARNESSED_HOME``, that every eval run writes into.
RESULTS_DIRNAME: Final = "evals"

#: The counts, rewritten by every run so the newest one is always the one on top.
SUMMARY_FILENAME: Final = "_summary.json"

#: The three tiers, and there is no fourth. Deterministic and live are 0-or-1 facts; judge is a
#: percentage against a threshold. A record that cannot say which kind it is, is not a record.
TIERS: Final[tuple[str, str, str]] = ("offline", "live", "judge")


class ResultError(RuntimeError):
    """A result could not be recorded as declared.

    Deliberately outside the ``HarnessError`` tree, for the same reason ``SocketGuardError`` is:
    the CLI catches ``HarnessError`` broadly, and a malformed eval record rendered as a tidy
    message is a malformed eval record nobody notices.
    """


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case's outcome. Six fields, and ``passed`` is a bool because the tier decides truth.

    ``expected`` and ``observed`` are strings rather than structured values on purpose: a result
    file is read by a person after the run, and the shortest honest rendering of "what I asked for"
    and "what I got" is a sentence fragment.
    """

    id: str
    tier: str
    passed: bool
    expected: str
    observed: str
    detail: str = ""


def results_root() -> Path:
    """``${HARNESSED_HOME}/evals``, resolved fresh on every call."""
    return config.load().home / RESULTS_DIRNAME


def write(cases: Sequence[CaseResult]) -> Path:
    """Record ``cases`` as one new ``.jsonl`` file, refresh the summary, and return the file.

    The directory is created on first use, which is what makes a fresh clone's first eval run
    work without a setup step.
    """
    undeclared = sorted({case.tier for case in cases} - set(TIERS))
    if undeclared:
        raise ResultError(
            f"unknown eval tier(s) {', '.join(undeclared)}. Declared tiers: {', '.join(TIERS)}."
        )

    root = results_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{ids.new_id('eval')}.jsonl"
    target.write_text("".join(f"{_line(case)}\n" for case in cases), encoding="utf-8", newline="\n")
    (root / SUMMARY_FILENAME).write_text(
        json.dumps(_summary(cases), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def _line(case: CaseResult) -> str:
    return json.dumps(asdict(case), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _summary(cases: Sequence[CaseResult]) -> dict[str, object]:
    by_tier = {tier: sum(1 for case in cases if case.tier == tier) for tier in TIERS}
    return {
        "total": len(cases),
        "passed": sum(1 for case in cases if case.passed),
        "failed": sum(1 for case in cases if not case.passed),
        "by_tier": {tier: count for tier, count in by_tier.items() if count},
    }
