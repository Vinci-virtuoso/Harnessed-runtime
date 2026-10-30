"""Ticket 232 (FR-126, FR-134): every ``GET`` route the gateway registers has a CLI twin.

A route with no twin is a read an operator can make in the dashboard and cannot make in a
terminal. This walks ``create_app``'s own route table and asks, for each ``/v1`` route, whether
it is a row of ``PARITY_SET`` (compared as bytes against its ``--json`` twin), a member of the
byte list (the two routes whose twin writes bytes rather than one JSON object), or a template of
``dashboard/lib/cli-twins.ts`` (the table the provenance drawer shows). A route in none of the
three fails here by name.
"""

from __future__ import annotations

import re
from pathlib import Path

from harnessed.gateway import create_app
from harnessed.memory import db
from test_gateway_parity import OUTSIDE_THE_SET, PARITY_SET

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_TWINS_TS = REPO_ROOT / "dashboard" / "lib" / "cli-twins.ts"

#: The routes whose twin writes bytes, not one JSON object: ``outputs get --out`` and
#: ``sessions stream``. Each has its own byte-comparison test in ``test_gateway_parity.py``.
BYTE_LIST = frozenset({"/v1/outputs/{id}", "/v1/sessions/{id}/stream"})


def shape(route: str) -> str:
    """A route with its query string dropped and every ``{name}`` spelled ``{id}``."""
    return re.sub(r"\{[^}]+\}", "{id}", route.split("?", 1)[0])


def registered(home: Path) -> set[str]:
    application = create_app(home / db.DATABASE_FILENAME)
    return {
        shape(route.path)
        for route in application.routes
        if hasattr(route, "path") and route.path.startswith("/v1")
    }


def ts_twins() -> set[str]:
    return {
        shape(found) for found in re.findall(r'route: "([^"]+)"', CLI_TWINS_TS.read_text("utf-8"))
    }


def test_every_get_route_has_a_twin(tmp_harnessed_home: Path) -> None:
    parity = {shape(pair.route) for pair in PARITY_SET}
    twinned = parity | BYTE_LIST
    missing = sorted(registered(tmp_harnessed_home) - twinned)
    assert not missing, f"routes with no CLI twin in PARITY_SET or the byte list: {missing}"
    undrawn = sorted(registered(tmp_harnessed_home) - ts_twins())
    assert not undrawn, f"routes the provenance drawer shows no twin for: {undrawn}"


def test_the_byte_list_and_the_exclusions_agree(tmp_harnessed_home: Path) -> None:
    excluded = {shape(route) for route in OUTSIDE_THE_SET}
    assert excluded == BYTE_LIST, "a route outside PARITY_SET must be on the byte list, and back"
    assert registered(tmp_harnessed_home) >= BYTE_LIST


def test_a_route_without_a_twin_would_fail_the_walk(tmp_harnessed_home: Path) -> None:
    parity = {shape(pair.route) for pair in PARITY_SET} | BYTE_LIST
    assert "/v1/spend" in parity
    assert "/v1/unmapped" not in parity
    assert sorted((registered(tmp_harnessed_home) | {"/v1/unmapped"}) - parity) == ["/v1/unmapped"]
