"""ACP delegation: bring your own coding agent (FR-22).

The point of this package is that the coding agent is **swappable**. Which CLI runs is a line in
``agents.toml`` and never a literal in the source, which is what
``tests/test_acp.py::test_no_specific_agent_cli_is_named_anywhere_in_the_package`` exists to
enforce.

Six modules, one responsibility each:

* :mod:`~harnessed.acp.config` reads ``agents.toml`` into an :class:`AgentSpec`.
* :mod:`~harnessed.acp.permissions` is the fail-closed matrix. Pure, synchronous, no I/O.
* :mod:`~harnessed.acp.runtime` owns the only asyncio loop in the repository.
* :mod:`~harnessed.acp.client` is the client half of the protocol, and it only buffers.
* :mod:`~harnessed.acp.delegate` spawns, handshakes, prompts, and flushes what it collected.
* ``ledger.py`` (ticket 022) makes a delegation idempotent. It is not part of this ticket.

This package is also the only one in the repository allowed to define a coroutine. Nothing
outside it awaits anything, and a module scan proves it.
"""

from __future__ import annotations

from acp import PROTOCOL_VERSION

from harnessed.acp.config import AgentSpec, agent_named, load_agents
from harnessed.acp.delegate import Delegation
from harnessed.acp.permissions import Decision, Policy, decide

#: ``PROTOCOL_VERSION`` is re-exported deliberately, and it is the one SDK name this package
#: hands out. ``delegate.py`` and ``client.py`` remain the only two modules that *speak* the
#: protocol; a caller that merely wants to report the version a handshake agreed on -
#: ``harnessed agents test`` does - reads the number here instead of importing the SDK itself
#: and quietly becoming a third module that speaks it.
__all__ = [
    "PROTOCOL_VERSION",
    "AgentSpec",
    "Decision",
    "Delegation",
    "Policy",
    "agent_named",
    "decide",
    "load_agents",
]
