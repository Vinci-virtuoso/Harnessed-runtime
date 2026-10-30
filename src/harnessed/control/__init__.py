"""The control app: the one surface in the process that writes (FR-77, decisions 43 and 49).

``create_app(home)`` builds a FastAPI app of ``POST`` routes and no ``GET``, mounted by
``dashboard_app`` at ``/control/v1`` beside the read gateway, never merged into it. It calls the
CLI's composition directly and imports nothing from ``harnessed.gateway``.
"""

from __future__ import annotations

from harnessed.control.app import PREFIX, create_app

__all__ = ["PREFIX", "create_app"]
