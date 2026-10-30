"""Where the shipped corpus lives, and the one function that finds it (FR-176, decision 88).

The suites, the routing tasks, the recorded wire bodies and the walkthroughs sit under
``harnessed/corpus/``. A checkout (the editable install of the ``src/`` layout) and an installed
wheel evaluate the same expression over the same relative layout, and neither reads the working
directory. Every other module asks here; no module climbs from its own file toward a repository
root (MUST rule 43). A leaf: stdlib and the error tree only.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from harnessed.errors import CorpusMissing

__all__ = [
    "corpus_root",
    "fake_wire",
    "jev_wire",
    "routing_tasks",
    "suites_dir",
    "walkthroughs_dir",
]

MANIFEST_NAME = "MANIFEST.sha256"


def _package_corpus() -> Path:
    return Path(str(resources.files("harnessed") / "corpus"))


def corpus_root() -> Path:
    """The corpus directory, or :class:`CorpusMissing` when it or its manifest is absent."""
    root = _package_corpus()
    if not root.is_dir():
        raise CorpusMissing(f"The shipped corpus directory is missing: {root}.")
    if not (root / MANIFEST_NAME).is_file():
        raise CorpusMissing(f"The shipped corpus has no {MANIFEST_NAME}: {root / MANIFEST_NAME}.")
    return root


def suites_dir() -> Path:
    """The offline suites: smoke, holdout, memory, work-email."""
    return corpus_root() / "suites"


def routing_tasks() -> Path:
    """The routing suite's task file."""
    return corpus_root() / "routing" / "tasks.yaml"


def fake_wire() -> Path:
    """The recorded bodies ``FakeProvider`` replays, one directory per scope (THE CRUX)."""
    return corpus_root() / "wire" / "fake"


def jev_wire() -> Path:
    """The recorded bodies the routing suite's offline Decider replays."""
    return corpus_root() / "wire" / "jev"


def walkthroughs_dir() -> Path:
    """The walkthroughs that ship in the wheel (decision 106)."""
    return corpus_root() / "walkthroughs"
