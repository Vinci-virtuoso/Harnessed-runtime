"""The CLI: a thin render layer over library entry points.

Every command is a short Typer function that calls a pure-ish function in the owning subpackage
and hands the result to :mod:`harnessed.cli.render`. That thinness is what lets the
four-command proof run in-process and in a shell with identical numbers.
"""

from __future__ import annotations
