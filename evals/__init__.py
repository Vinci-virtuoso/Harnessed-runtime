"""The evaluation suite, outside the package on purpose.

This directory is waku-agent's layout, ported. It sits beside ``src/`` rather than inside
``src/harnessed/`` for four reasons that all point the same way: the import table enforced by
``tests/test_boundaries.py`` reaches only inside the package, ``mypy --strict`` covers
``src/harnessed`` only, coverage measures ``src/harnessed`` only, and nothing here ships in the
wheel. An eval that drives the real loop with the real tool registry has to reach
``harnessed.cli`` and ``harnessed.tools``; from out here it may.

Three tiers, and the difference between them is the whole point.

* **Deterministic** (``deterministic/``) asserts a binary, checkable outcome: the right tool
  fired (or did not), with the right arguments, and the artifact exists. 0 or 1.
* **Live** is the second tier inside each deterministic file, gated on a real key plus
  ``HARNESSED_RUN_LIVE_EVALS=1``, parametrised over ``dataset.jsonl``.
* **Judge** (``judge/``) produces 0-1 percentages against a threshold, not 0/1 truths. Never
  confuse the two, and never let one gate a promotion.

Results are written under ``HARNESSED_HOME``, never into the repository tree; ``results.py`` owns
that and is the only module here that names the path.
"""
