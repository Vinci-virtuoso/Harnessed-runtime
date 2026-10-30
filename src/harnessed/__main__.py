"""``python -m harnessed`` resolves to the same entry point as the ``harnessed`` script."""

from __future__ import annotations

from harnessed.cli.app import main

if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()
