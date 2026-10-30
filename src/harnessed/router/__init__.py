"""The router: decision 56, "the router sits in the composition, before ``solve``".

``decisions.py`` is the whole package so far - ``record_probe`` (ticket 156, FR-97), the write
half of ``router_probes`` (migration 013). Decision 56 names five modules this package will
eventually hold: ``questions.py``, ``state.py``, ``route.py``, ``decisions.py`` and ``suite.py``.
The rest land with the tickets that need them; nothing here imports ahead of what exists.
"""

from __future__ import annotations
