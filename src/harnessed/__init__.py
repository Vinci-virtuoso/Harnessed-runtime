"""Harnessed - a legible, offline-deterministic agent harness.

The four leaf modules re-exported here import nothing else internal and are the single source
for their concern: ``errors`` for the exception tree, ``config`` for environment reading,
``ids`` for identifier generation, ``clock`` for time. The version is the installed
metadata's alone (decision 90): ``importlib.metadata.version("harnessed-runtime")``,
never a literal here.
"""

from __future__ import annotations

__all__ = ["clock", "config", "errors", "ids"]

from harnessed import clock, config, errors, ids
