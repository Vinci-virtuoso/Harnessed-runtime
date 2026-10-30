"""The read-only HTTP surface: ``GET`` plus SSE over the same ledgers the CLI writes.

Behind the ``gateway`` extra (FR-31). ``fastapi`` is imported here and nowhere else in the
repository, and ``cli/serve_cmd.py`` reaches this package only inside its function body, so a
checkout without the extra fails as a named ``GatewayExtraMissing`` rather than at import time
(decision 21). Importing this package with the extra absent raises ``ImportError`` on purpose:
that is the signal ``serve`` translates.

Everything served is a second view of the stores, never a second source of truth: handlers
return the same payloads the CLI prints, encoded by ``serialize.canonical()`` (decision 19), and
errors leave through ``gateway/errors.py`` in the one body of decision 24.
"""

from __future__ import annotations

from harnessed.gateway.app import create_app

__all__ = ["create_app"]
