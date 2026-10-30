"""The flywheel: reflect on failures, write a candidate, measure it, promote or refuse.

Four modules, one responsibility each, landing in four tickets:

* :mod:`~harnessed.improve.candidate` owns what a candidate *is* - the append-only rule, the
  content-addressed id and the one place a candidate reaches the store (FR-17).
* :mod:`~harnessed.improve.reflector` turns a set of failing cases into that candidate, through
  the ordinary provider seam and nothing else (FR-17).
* ``promoter.py`` decides, with arithmetic and zero model calls (FR-20).
* ``steps.py`` makes the cycle resumable (FR-18).

The package's whole surface area is deliberately one sentence: **a candidate may only append
text to the system prompt.** Never code, never a tool schema, never a policy field (locked
decision 5). That restriction is what keeps the self-improvement surface small enough to reason
about, and :mod:`~harnessed.improve.candidate` enforces it at write time rather than trusting
the caller.

This package imports ``evals``, ``memory``, ``providers.types`` and ``events``, and nothing
else. It never reaches ``loop/`` - the procedure envelope lives in ``loop/prompt.py`` and
arrives as an injected builder, the standing answer to an import-table wall in this repository.
"""

from __future__ import annotations
