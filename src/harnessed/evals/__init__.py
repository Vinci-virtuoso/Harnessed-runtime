"""Evals: the arithmetic the milestone's thesis rests on.

``suite.py`` loads YAML suites into ordered cases with authored ids (FR-14). ``graders.py``
scores one case deterministically (FR-15), ``scoring.py`` owns every score comparison (Q1/Q2)
and ``runner.py`` drives the loop and writes ``eval_results`` (FR-16). Each lands in its own
ticket; this package exists so the first of them has somewhere to live.
"""

from __future__ import annotations
