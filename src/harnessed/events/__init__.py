"""Events: the observability seam (FR-21).

``bus`` owns publish and subscribe; ``names`` owns the frozen taxonomy. Nothing publishes a name
the taxonomy does not hold, and nothing renames a name it does.
"""

from __future__ import annotations

from harnessed.events.bus import Event, EventBus, Observer
from harnessed.events.names import NAMES, OBSERVER_FAILED

__all__ = ["NAMES", "OBSERVER_FAILED", "Event", "EventBus", "Observer"]
