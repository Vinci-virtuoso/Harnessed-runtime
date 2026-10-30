"""FR-42: ``doctor`` tells the truth about the ladder and about the gateway.

Ticket 073, the close of v2. Two claims, both of which used to be strings.

* **The ladder is renumbered.** The gateway is rung 3 and the v2 milestone; the dashboard is
  rung 4 (a constant ``v3`` until ticket 125 made it a check; ``tests/test_dashboard_cmd.py``).
  A scan over every ``Rung(...)`` call in ``cli/`` proves no description still
  names the dashboard as v2, in ``doctor`` or in ``init``.
* **The gateway rung is computed, never compared.** ``_rung_gateway`` imports
  ``harnessed.gateway`` inside the check, builds the app and counts ``app.routes``. ``live`` is
  decided by that count and nothing else: an app that imports but registers zero routes is the
  case that makes this structural rather than a string comparison, and it is asserted
  explicitly (MUST rule 12). The extra absent is ``not yet`` with the install command, because
  a rung the operator has not climbed is not a failure (FR-29).

The ticket's acceptance criteria say ``pass`` and ``skip``. The ladder's vocabulary is ``live``,
``not yet`` and ``broken``, and ``tests/test_cli.py`` refuses any ``Check`` that can report a
literal ``skip``, so the mapping is ``pass`` -> ``live`` and ``skip`` -> ``not yet``; the ticket
records it.
"""

from __future__ import annotations

import ast
import io
import json
import shutil
import sys
import urllib.error
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from harnessed.acp import config as acp_config
from harnessed.cli import doctor_cmd, init_cmd
from harnessed.cli.app import app, main
from harnessed.config import Settings, load
from harnessed.errors import GatewayExtraMissing
from harnessed.memory import db
from harnessed.providers import jev

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_ROOT = REPO_ROOT / "src" / "harnessed" / "cli"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "073"
RATCHET_FILE = REPO_ROOT / ".github" / "coverage-ratchet"

#: Decision 25: the ratchet this ticket owns, and the number it ends the milestone at.
RATCHET_AT_CLOSE = 85

split = CliRunner(mix_stderr=False)


def evidence(name: str, text: str) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / name).write_text(text, encoding="utf-8")


def ladder_lines(rungs: tuple[doctor_cmd.Rung, ...]) -> str:
    return "\n".join(f"rung {r.number}  {r.name:<14}{r.state:<8} {r.detail}" for r in rungs)


def gateway_installed() -> bool:
    try:
        import harnessed.gateway  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# 1. The ladder names the right rungs
# ---------------------------------------------------------------------------


def test_doctor_reports_the_gateway_at_rung_three_and_the_dashboard_at_rung_four(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rungs = doctor_cmd.run_rungs(load())
    assert [rung.number for rung in rungs] == [1, 2, 3, 4, 5]
    assert rungs[2].name == "Gateway"
    assert "v2" in rungs[2].detail
    assert rungs[3].name == "Dashboard"
    assert rungs[3].state in {"live", "not yet", "broken"}, "rung 4 is a check, not a constant"
    assert "v2" not in rungs[3].detail
    assert rungs[4].name == "router.probe"
    evidence(
        "doctor-ladder.txt",
        f"gateway extra installed: {gateway_installed()}\n\n{ladder_lines(rungs)}\n",
    )


def rung_calls() -> list[tuple[Path, ast.Call]]:
    """Every ``Rung(...)`` call in ``cli/``, so a stale ladder in ``init`` is caught too."""
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted(CLI_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Rung"
            ):
                found.append((path, node))
    return found


def literals_of(call: ast.Call) -> set[str]:
    return {
        arg.value
        for arg in [*call.args, *(kw.value for kw in call.keywords)]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }


def test_no_rung_description_names_the_dashboard_as_v2() -> None:
    """The count the PRD asks for, over every ladder the CLI prints, not just doctor's."""
    calls = rung_calls()
    assert calls, "no Rung(...) call was found; the scan is looking at the wrong directory"
    stale = [
        f"{path.name}:{call.lineno}"
        for path, call in calls
        if {"Dashboard", "v2"} <= literals_of(call)
    ]
    assert len(stale) == 0, stale


def test_init_and_doctor_agree_on_the_upper_rungs(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``init`` prints a ladder too. Two ladders that disagree is the defect with a second face.

    ``init``'s is the four onboarding rungs (``cli/onboarding.py::ladder``, out of this ticket's
    scope): rung 5 is FR-97's live rung, no part of getting the CLI, an agent, the gateway and the
    dashboard running, so it is not compared here - the two ladders agree on the rungs they share
    and doctor's is one longer.
    """
    monkeypatch.chdir(tmp_path)
    from_init = init_cmd.ladder("fake")
    from_doctor = doctor_cmd.run_rungs(load())
    assert len(from_init) == 4
    assert [rung.number for rung in from_doctor] == [1, 2, 3, 4, 5]
    assert [(r.number, r.name) for r in from_init] == [(r.number, r.name) for r in from_doctor[:4]]
    assert from_init[3] == from_doctor[3]


# ---------------------------------------------------------------------------
# 2. The gateway rung enumerates routes
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not gateway_installed(), reason="the gateway extra is not installed")
def test_gateway_rung_is_live_because_routes_are_registered(
    clean_env: None, tmp_harnessed_home: Path
) -> None:
    rung = doctor_cmd._rung_gateway(load())
    assert rung.state == "live", rung.detail
    assert "harnessed serve" in rung.detail


def test_gateway_rung_counts_the_routes_the_app_registered(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``live`` follows ``len(app.routes)``: the check reads the app, not a sentence about it."""
    pytest.importorskip("harnessed.gateway")
    import harnessed.gateway as gateway

    seen: list[int] = []
    original = gateway.create_app

    def counted(database_path: Path) -> object:
        built = original(database_path)
        seen.append(len(built.routes))
        return built

    monkeypatch.setattr(gateway, "create_app", counted)
    rung = doctor_cmd._rung_gateway(load())
    assert rung.state == "live"
    assert seen and seen[0] > 0
    assert str(seen[0]) in rung.detail


def test_gateway_rung_is_not_live_when_the_app_registers_zero_routes(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion that makes the check structural: an app with no routes is not a gateway."""
    pytest.importorskip("harnessed.gateway")
    import harnessed.gateway as gateway

    monkeypatch.setattr(gateway, "create_app", lambda database_path: SimpleNamespace(routes=[]))
    rung = doctor_cmd._rung_gateway(load())
    assert rung.state != "live"
    assert rung.state == "broken"
    assert "0 routes" in rung.detail
    evidence(
        "route-enumeration-skip.txt",
        f"create_app replaced by an app whose routes list is empty\n\n{ladder_lines((rung,))}\n",
    )


def test_gateway_rung_is_not_yet_when_the_extra_is_absent(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "harnessed.gateway", None)
    rung = doctor_cmd._rung_gateway(load())
    assert rung.state == "not yet"
    assert GatewayExtraMissing.remedy in rung.detail
    result = split.invoke(app, ["doctor", "--offline"])
    assert result.exit_code == 0, result.stderr
    with EVIDENCE_DIR.joinpath("doctor-ladder.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"\ngateway extra absent (sys.modules)\n\n{ladder_lines((rung,))}\n")


def test_gateway_rung_import_is_lazy() -> None:
    """The extra absent must be a rung state, never an import error at CLI start."""
    tree = ast.parse((CLI_ROOT / "doctor_cmd.py").read_text(encoding="utf-8"))
    top_level = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    assert not any(name.startswith("harnessed.gateway") for name in top_level)


# ---------------------------------------------------------------------------
# 3. doctor still exits 0 either way
# ---------------------------------------------------------------------------


def test_doctor_offline_exits_zero_with_or_without_the_extra(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    present = split.invoke(app, ["doctor", "--offline"])
    assert present.exit_code == 0, present.stderr
    monkeypatch.setitem(sys.modules, "harnessed.gateway", None)
    absent = split.invoke(app, ["doctor", "--offline"])
    assert absent.exit_code == 0, absent.stderr


def test_an_unclimbed_rung_never_fails_doctor(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "harnessed.gateway", None)
    rungs = {rung.number: rung for rung in doctor_cmd.run_rungs(load())}
    assert rungs[2].state == "not yet"
    assert rungs[3].state == "not yet"
    assert not any(rung.broken for rung in rungs.values())
    assert split.invoke(app, ["doctor", "--offline"]).exit_code == 0


def test_rung_two_is_declared_not_live_when_every_executable_resolves(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 088: a PATH hit is not a handshake, so the proof is named rather than claimed."""
    (tmp_path / "agents.toml").write_text(
        f"[agents.probe]\ncommand = {json.dumps(sys.executable)}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    rung = doctor_cmd.run_rungs(load())[1]
    assert rung.state == "declared"
    assert not rung.broken
    assert "harnessed agents test probe" in rung.detail
    result = split.invoke(app, ["doctor", "--offline"])
    assert result.exit_code == 0, result.stderr
    assert "declared" in result.stdout
    evidence_088 = REPO_ROOT / "evidence" / "088"
    evidence_088.mkdir(parents=True, exist_ok=True)
    (evidence_088 / "doctor-rung2.txt").write_text(ladder_lines((rung,)) + "\n", encoding="utf-8")


def test_rung_two_prints_the_jail_kind_beside_each_cli_entry(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 214: the label says which jail, and an acp entry carries none."""
    from harnessed.acp import jail

    monkeypatch.setattr(jail, "detect", lambda: jail.Jail("job_object"))
    (tmp_path / "agents.toml").write_text(
        f'[agents.thin]\ncommand = {json.dumps(sys.executable)}\nkind = "cli"\n'
        f"[agents.rich]\ncommand = {json.dumps(sys.executable)}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    detail = doctor_cmd.run_rungs(load())[1].detail
    assert "thin declared, never proven" in detail
    assert detail.count("[jail: job_object]") == 1
    assert detail.index("[jail: job_object]") < detail.index("rich declared")


@pytest.mark.parametrize(
    ("answer", "suffix"),
    [
        ("No permitted user namespace", "[jail: none - No permitted user namespace]"),
        ("", "[jail: none]"),
    ],
)
def test_rung_two_prints_a_refused_jail_with_its_reason(
    answer: str,
    suffix: str,
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket 217: a host that refuses ``bwrap`` says so, with the reason when there is one."""
    from harnessed.acp import jail

    monkeypatch.setattr(jail, "detect", lambda: jail.Jail("none", reason=answer))
    (tmp_path / "agents.toml").write_text(
        f'[agents.thin]\ncommand = {json.dumps(sys.executable)}\nkind = "cli"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    detail = doctor_cmd.run_rungs(load())[1].detail
    assert detail.count(suffix) == 1


def test_rung_two_is_broken_when_an_executable_is_missing(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agents.toml").write_text(
        '[agents.probe]\ncommand = "a-binary-that-is-not-installed"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert doctor_cmd.run_rungs(load())[1].state == "broken"
    assert split.invoke(app, ["doctor", "--offline"]).exit_code != 0


# ---------------------------------------------------------------------------
# 5. The live rung (ticket 156, FR-97, decision 57)
# ---------------------------------------------------------------------------

EVIDENCE_156 = REPO_ROOT / "evidence" / "156"

#: Placeholder credential (ticket 008's lesson: nothing that looks like a real key).
THROWAWAY_ROUTER_KEY = "not-a-real-key-0000"

#: One answer of each type, matching ``doctor_cmd._PROBE_QUESTIONS``.
_PASSING_BODY = {
    "id": "gen-dec-stub-1",
    "model": "typesafe/jev-1.13-20260917",
    "usage": {"cost": 1e-05},
    "answers": {
        "noul": {"type": "noul", "noul": 0.42},
        "choice": {
            "type": "choice",
            "choice": "a",
            "confidence": 0.9,
            "probabilities": {"a": 0.9, "b": 0.1},
        },
        "score": {
            "type": "score",
            "score": 2,
            "confidence": 0.8,
            "legend": {"0": "not at all", "1": "somewhat", "2": "fully"},
            "probabilities": {},
        },
    },
}

#: A drifted answer: ``choice`` names an option nobody asked for (only ``a`` and ``b`` were).
_REFUSED_BODY = {
    **_PASSING_BODY,
    "answers": {
        **_PASSING_BODY["answers"],
        "choice": {"type": "choice", "choice": "c", "confidence": 0.5},
    },
}


class _StubOpener:
    """Stands in for ``urlopen`` one layer below ``JevDecider.decide``, exactly like
    ``tests/test_jev.py``'s double. Never opens a socket."""

    def __init__(self, *, body: bytes) -> None:
        self._body = body
        self.calls: list[object] = []

    def __call__(self, request: object, timeout: float) -> io.BytesIO:
        self.calls.append(request)
        return io.BytesIO(self._body)


def evidence_156(name: str, text: str) -> None:
    EVIDENCE_156.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_156 / name).write_text(text, encoding="utf-8")


def _router_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", THROWAWAY_ROUTER_KEY)
    return load()


def test_doctor_live_reports_pass_with_latency_and_cost_and_records_one_row(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _router_settings(tmp_path, monkeypatch)
    opener = _StubOpener(body=json.dumps(_PASSING_BODY).encode("utf-8"))
    decider = jev.JevDecider(settings.router_model, settings, opener=opener)

    rung = doctor_cmd._rung_router(settings, live=True, decider=decider)

    assert rung.state == "live"
    assert "pass" in rung.detail
    assert "1e-05" in rung.detail
    assert len(opener.calls) == 1, "the rung never retries and never routes"

    conn = db.connect()
    rows = conn.execute(
        "SELECT model, status, cost FROM router_probes ORDER BY probe_id"
    ).fetchall()
    assert rows == [("typesafe/jev-1.13-20260917", "passed", "1e-05")]
    evidence_156("pass.txt", f"{rung}\nrouter_probes row: {rows[0]}\n")


def test_doctor_live_reports_fail_naming_the_refusal_records_a_failed_row_never_routes(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _router_settings(tmp_path, monkeypatch)
    opener = _StubOpener(body=json.dumps(_REFUSED_BODY).encode("utf-8"))
    decider = jev.JevDecider(settings.router_model, settings, opener=opener)

    rung = doctor_cmd._rung_router(settings, live=True, decider=decider)

    assert rung.state == "broken"
    assert "fail" in rung.detail
    assert "not asked" in rung.detail
    assert len(opener.calls) == 1, "a refusal is reported, never retried into a route"

    conn = db.connect()
    rows = conn.execute(
        "SELECT model, status, cost FROM router_probes ORDER BY probe_id"
    ).fetchall()
    assert rows == [("typesafe/jev-1.13-20260917", "failed", None)]
    evidence_156("fail.txt", f"{rung}\nrouter_probes row: {rows[0]}\n")


def test_doctor_live_wraps_a_transport_failure_the_same_way(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not just a refused answer: an unreachable host is also a ``DecisionError`` and fails
    the rung rather than raising past it."""
    settings = _router_settings(tmp_path, monkeypatch)

    class _Unreachable:
        def __call__(self, request: object, timeout: float) -> io.BytesIO:
            raise urllib.error.URLError("Network is unreachable")

    decider = jev.JevDecider(settings.router_model, settings, opener=_Unreachable())
    rung = doctor_cmd._rung_router(settings, live=True, decider=decider)
    assert rung.state == "broken"
    assert "fail" in rung.detail


def test_doctor_offline_reports_router_probe_as_not_yet_naming_doctor_live(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rungs = {rung.number: rung for rung in doctor_cmd.run_rungs(load())}
    assert rungs[5].name == "router.probe"
    assert rungs[5].state == "not yet"
    assert "harnessed doctor --live" in rungs[5].detail
    assert not rungs[5].broken

    result = split.invoke(app, ["doctor", "--offline"])
    assert result.exit_code == 0, result.stderr
    assert "router.probe" in result.stdout
    assert "not yet" in result.stdout
    evidence_156("offline-not-yet.txt", result.stdout)


def test_offline_and_live_are_mutually_exclusive() -> None:
    result = split.invoke(app, ["doctor", "--offline", "--live"])
    assert result.exit_code != 0

    with pytest.raises(typer.Exit) as excinfo:
        doctor_cmd.doctor(offline=True, live=True, as_json=False)
    assert excinfo.value.exit_code == 2


def test_doctor_live_invoked_through_the_cli_records_and_reports(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path, not just the rung builder: ``doctor --live`` through the CLI, a stub
    swapped in for ``JevDecider`` so no socket opens."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", THROWAWAY_ROUTER_KEY)
    opener = _StubOpener(body=json.dumps(_PASSING_BODY).encode("utf-8"))
    monkeypatch.setattr(
        doctor_cmd,
        "JevDecider",
        lambda model, settings, **_: jev.JevDecider(model, settings, opener=opener),
    )

    result = split.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 0, result.stderr
    assert "router.probe" in result.stdout

    conn = db.connect()
    rows = conn.execute("SELECT status FROM router_probes").fetchall()
    assert rows == [("passed",)]


@pytest.mark.live
def test_doctor_live_reaches_the_real_router_and_records_a_probe(
    tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live half (FR-97): no stub, the operator's own key, the real Decisions API.

    Deselected by default (``-m "not live"``) like every other test that reaches a real
    endpoint (``test_pricing.py``, ``test_live_providers.py``). Skips rather than fails when
    the operator has not configured ``OPENROUTER_API_KEY`` - the same shape
    ``test_prompt_prefix.py``'s live test uses for a provider key it cannot assume - because an
    unconfigured key is not this rung's defect to report.
    """
    settings = load()
    if settings.api_key_for("openrouter") is None:
        pytest.skip("no live key for openrouter")

    result = split.invoke(app, ["doctor", "--live"])
    evidence_156(
        "live-real.txt",
        f"$ harnessed doctor --live   (live, exit {result.exit_code})\n"
        + result.stdout
        + (f"stderr:\n{result.stderr}" if result.stderr else ""),
    )
    assert result.exit_code == 0, result.stderr
    assert "router.probe" in result.stdout

    conn = db.connect()
    rows = conn.execute("SELECT model, status FROM router_probes").fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 6. Registry warnings on a thin entry (ticket 159, decision 62, FR-100)
# ---------------------------------------------------------------------------


def _spec(name: str, **overrides: object) -> acp_config.AgentSpec:
    """A minimal, valid :class:`AgentSpec` with everything but ``name`` overridable."""
    fields: dict[str, object] = {"session_meta": {"settings": {"sources": []}}, **overrides}
    return acp_config.AgentSpec(name=name, command="dummy", **fields)  # type: ignore[arg-type]


def test_registry_warnings_flags_an_entry_with_no_use_for() -> None:
    warnings = doctor_cmd.registry_warnings({"solo": _spec("solo", billing="oauth")})
    assert len(warnings) == 1
    assert "solo" in warnings[0]
    assert "use_for" in warnings[0]


def test_registry_warnings_flags_two_entries_sharing_one_use_for() -> None:
    agents = {
        "alpha": _spec("alpha", use_for="general coding", billing="oauth"),
        "beta": _spec("beta", use_for="general coding", billing="oauth"),
    }
    warnings = doctor_cmd.registry_warnings(agents)
    duplicate = [w for w in warnings if "identical use_for" in w]
    assert len(duplicate) == 1
    assert "alpha" in duplicate[0]
    assert "beta" in duplicate[0]


def test_registry_warnings_flags_an_entry_with_no_billing() -> None:
    warnings = doctor_cmd.registry_warnings({"solo": _spec("solo", use_for="anything")})
    assert len(warnings) == 1
    assert "solo" in warnings[0]
    assert "billing" in warnings[0]


def test_registry_warnings_flags_an_openrouter_entry_with_no_base_url_var() -> None:
    warnings = doctor_cmd.registry_warnings(
        {"solo": _spec("solo", use_for="anything", billing="openrouter")}
    )
    assert len(warnings) == 1
    assert "solo" in warnings[0]
    assert "base_url_var" in warnings[0]


def test_registry_warnings_is_silent_for_a_fully_specified_registry() -> None:
    agents = {
        "alpha": _spec(
            "alpha",
            use_for="general coding",
            billing="openrouter",
            base_url_var="ALPHA_BASE_URL",
        ),
        "beta": _spec("beta", use_for="a different task", billing="oauth"),
    }
    assert doctor_cmd.registry_warnings(agents) == ()


def _shell(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    """Drive ``main`` directly, the way ``test_agents_onboarding.py::shell`` does.

    ``CliRunner(mix_stderr=False)`` reported an empty ``stderr`` for a plain ``print(...,
    file=sys.stderr)`` line here, the exact shape ticket 079 already recorded for
    ``render.failure`` - so a stderr assertion goes through ``main`` and ``capsys`` instead.
    """
    monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_info:
        code = 0 if exit_info.code is None else int(exit_info.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_doctor_prints_a_warning_per_thin_entry_and_still_exits_zero(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The AC: doctor warns, naming the entry, and none of it moves the exit code."""
    (tmp_path / "agents.toml").write_text(
        f"[agents.thin]\ncommand = {json.dumps(sys.executable)}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    code, _, err = _shell(["doctor", "--offline"], monkeypatch, capsys)
    assert code == 0, err
    assert "warning:" in err
    assert "thin" in err
    assert "use_for" in err
    assert "billing" in err
    # Never on stdout, and never inside the --json payload (render.warning's own contract).
    _, out, _ = _shell(["doctor", "--offline", "--json"], monkeypatch, capsys)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "declares no use_for" not in json.dumps(payload)
    assert "declares no billing" not in json.dumps(payload)


def test_doctor_prints_no_registry_warning_when_no_registry_is_declared(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, err = _shell(["doctor", "--offline"], monkeypatch, capsys)
    assert code == 0, err
    assert "warning:" not in err


# ---------------------------------------------------------------------------
# 4. The ratchet ends at 85
# ---------------------------------------------------------------------------


def test_the_coverage_ratchet_reads_eighty_five() -> None:
    assert int(RATCHET_FILE.read_text(encoding="utf-8").strip()) == RATCHET_AT_CLOSE


def test_registry_warnings_names_an_acp_entry_that_runs_with_its_folders_settings() -> None:
    name = next(n for n, t in acp_config.TEMPLATES.items() if t.session_meta)
    bare = acp_config.AgentSpec(name=name, command="dummy", use_for="x", billing="oauth")
    warnings = doctor_cmd.registry_warnings({name: bare})
    lines = acp_config.render_template_fields(acp_config.TEMPLATES[name])
    assert len(warnings) == 1
    assert warnings[0].startswith(f"{name} runs with its folder's settings; paste into")
    assert f"[agents.{name}]: {' and '.join(lines)}" in warnings[0]
    assert f"harnessed agents add {name} --refresh" in warnings[0]
    assert doctor_cmd.settings_word(bare) == "folder - add session_meta"


def test_registry_warnings_is_silent_for_a_template_with_no_session_meta() -> None:
    name = next(
        n for n, t in acp_config.TEMPLATES.items() if not t.session_meta and t.kind == "acp"
    )
    bare = acp_config.AgentSpec(name=name, command="dummy", use_for="x", billing="oauth")
    assert doctor_cmd.registry_warnings({name: bare}) == ()
    assert doctor_cmd.settings_word(bare) == "folder - its adapter documents no override"


def test_settings_word_for_a_harnessed_entry_and_an_unknown_name() -> None:
    assert doctor_cmd.settings_word(_spec("whatever")) == "harnessed"
    unknown = acp_config.AgentSpec(name="nope", command="dummy", use_for="x", billing="oauth")
    assert doctor_cmd.settings_word(unknown) == "folder - no template"
    assert doctor_cmd.registry_warnings({"nope": unknown}) == ()


def test_rung_two_ends_each_entry_with_its_settings_word(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harnessed.acp import jail

    monkeypatch.setattr(jail, "detect", lambda: jail.Jail("job_object"))
    meta = next(n for n, t in acp_config.TEMPLATES.items() if t.session_meta)
    bare = next(
        n for n, t in acp_config.TEMPLATES.items() if not t.session_meta and t.kind == "acp"
    )
    exe = json.dumps(sys.executable)
    (tmp_path / "agents.toml").write_text(
        f"[agents.{meta}]\ncommand = {exe}\n"
        f"[agents.{bare}]\ncommand = {exe}\n"
        f"[agents.mystery]\ncommand = {exe}\n"
        f"[agents.rich]\ncommand = {exe}\nsession_meta = {{ a = 1 }}\n"
        f'[agents.thin]\ncommand = {exe}\nkind = "cli"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    detail = doctor_cmd.run_rungs(load())[1].detail
    assert f"{meta} declared" in detail and "[settings: folder - add session_meta]" in detail
    assert "[settings: folder - its adapter documents no override]" in detail
    assert "[settings: folder - no template]" in detail
    assert detail.count("[settings: harnessed]") == 1
    assert detail.count("[jail: job_object]") == 1
    assert detail.count("[settings:") == 4


def test_registry_warnings_flags_tool_names_with_no_session_meta() -> None:
    spec = acp_config.AgentSpec(
        name="half",
        command="dummy",
        use_for="x",
        billing="oauth",
        tool_names={"execute": ("Run",)},
    )
    warnings = doctor_cmd.registry_warnings({"half": spec})
    assert len(warnings) == 1
    assert "tool_names but no session_meta" in warnings[0]


def test_rung_two_reads_a_passed_cli_probe_without_a_login_word(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 221: `<name> live, proven <date> [jail: <kind>]`, no login word."""
    from harnessed.acp import jail, probes
    from harnessed.memory import db

    monkeypatch.setattr(jail, "detect", lambda: jail.Jail("bwrap"))
    exe = json.dumps(sys.executable)
    target = tmp_path / "agents.toml"
    target.write_text(f'[agents.thin]\ncommand = {exe}\nkind = "cli"\n', encoding="utf-8")
    spec = acp_config.load_agents(target)["thin"]
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        probes.record(
            conn,
            agent="thin",
            fingerprint=probes.fingerprint(spec, shutil.which(spec.command)),
            status=probes.PASSED,
            protocol_version=0,
            detail=probes.with_jail(jail.Jail("bwrap"), "ok"),
        )
    monkeypatch.chdir(tmp_path)
    detail = doctor_cmd.run_rungs(load())[1].detail
    assert "thin live, proven " in detail and "[jail: bwrap]" in detail
    assert "login" not in detail


def test_rung_one_says_routing_also_needs_the_openrouter_key(
    clean_env: None, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 232: the words are ``config.py``'s own, present while the key is unset and gone
    once it is set."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = load()
    notice = settings.routing_key_notice()
    assert notice == "routing also needs OPENROUTER_API_KEY"
    assert notice in doctor_cmd._rung_cli(settings).detail

    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-not-a-key")
    keyed = load()
    assert keyed.routing_key_notice() is None
    assert "routing also needs" not in doctor_cmd._rung_cli(keyed).detail


def test_doctor_line_and_served_fields_agree_for_the_four_confinement_cases(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-131 (G-60): rung 2's per-entry line and `/v1/agents`' fields say the same, four ways."""
    from contextlib import closing

    from harnessed.acp import jail, probes
    from harnessed.memory import db

    bare = next(
        n for n, t in acp_config.TEMPLATES.items() if not t.session_meta and t.kind == "acp"
    )
    exe = json.dumps(sys.executable)
    (tmp_path / "agents.toml").write_text(
        f"[agents.rich]\ncommand = {exe}\nsession_meta = {{ a = 1 }}\n"
        f"[agents.{bare}]\ncommand = {exe}\n"
        f'[agents.thin]\ncommand = {exe}\nkind = "cli"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def agree(found: jail.Jail) -> tuple[str, dict[str, probes.AgentState]]:
        monkeypatch.setattr(jail, "detect", lambda: found)
        detail = doctor_cmd.run_rungs(load())[1].detail
        with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
            rows = {r.agent: r for r in probes.states(conn, tmp_path / "agents.toml")}
        return detail, rows

    detail, rows = agree(jail.Jail(jail.NONE, reason="user namespaces refused"))
    assert "[settings: harnessed]" in detail and rows["rich"].settings == "harnessed"
    assert "[settings: folder - its adapter documents no override]" in detail
    assert (rows[bare].settings, rows[bare].confined_reason) == (
        "folder",
        "its adapter documents no override",
    )
    assert "[jail: none - user namespaces refused]" in detail
    assert (rows["thin"].jail, rows["thin"].confined_reason) == ("none", "user namespaces refused")
    detail, rows = agree(jail.Jail(jail.BWRAP))
    assert "[jail: bwrap]" in detail and rows["thin"].jail == "bwrap"
    detail, rows = agree(jail.Jail(jail.JOB_OBJECT))
    assert "[jail: job_object]" in detail and rows["thin"].jail == "job_object"


@pytest.mark.skipif(sys.platform != "darwin", reason="ci: macos job only")
def test_jail_macos_doctor_and_agents_list_carry_the_same_confined_reason(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harnessed.acp import jail, probes

    exe = json.dumps(sys.executable)
    (tmp_path / "agents.toml").write_text(
        f'[agents.thin]\ncommand = {exe}\nkind = "cli"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    jail.reset()
    detail = doctor_cmd.run_rungs(load())[1].detail
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        (row,) = probes.states(conn, tmp_path / "agents.toml")
    assert row.confined_reason == jail.MACOS_REASON
    assert f"[jail: none - {jail.MACOS_REASON}]" in detail


# ---------------------------------------------------------------------------
# Ticket 322 (FR-180, decision 95): the platform section names every seam's choice
# ---------------------------------------------------------------------------

SEAM_ORDER = ["host", "jail", "process identity", "scheduler", "file mode", "console encoding"]


def test_host_describes_the_os_and_python_through_platform() -> None:
    from harnessed import host

    line = host.describe(system="Linux", release="6.1", python="3.12.4")

    assert line == host.SeamLine("host", "Linux 6.1, Python 3.12.4", None)
    assert host.describe().implementation.endswith(f"Python {sys.version.split()[0]}")


@pytest.mark.parametrize(
    ("platform", "which", "selftest", "implementation", "reason"),
    [
        ("win32", None, None, "job_object (stops no write)", None),
        ("linux", "/usr/bin/bwrap", "bwrap", "bwrap", None),
        ("linux", "/usr/bin/bwrap", "refused", "none", "operation not permitted"),
        ("linux", None, None, "none", "bwrap not on PATH"),
        ("darwin", None, None, "none", "no jail is chosen for macOS: sandbox-exec is deprecated"),
    ],
)
def test_the_jail_line_matches_decision_95_on_each_os(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    which: str | None,
    selftest: str | None,
    implementation: str,
    reason: str | None,
) -> None:
    from harnessed.acp import jail

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(jail.shutil, "which", lambda _name: which)
    monkeypatch.setattr(
        jail,
        "_self_test",
        lambda: (
            jail.Jail(jail.BWRAP)
            if selftest == "bwrap"
            else jail.Jail(jail.NONE, reason="operation not permitted")
        ),
    )
    jail.reset()
    try:
        line = jail.describe()
    finally:
        jail.reset()

    assert line.seam == "jail"
    assert line.implementation == implementation
    assert (line.reason is None) is (reason is None)
    if reason:
        assert line.reason is not None and line.reason.startswith(reason)


@pytest.mark.parametrize(
    ("platform", "implementation", "reason"),
    [
        ("win32", "GetProcessTimes", None),
        ("linux", "/proc/<pid>/stat", None),
        ("darwin", "proc_pidinfo", None),
        ("freebsd14", "pid only", "no process start time"),
    ],
)
def test_the_process_identity_line_matches_decision_95_on_each_os(
    monkeypatch: pytest.MonkeyPatch, platform: str, implementation: str, reason: str | None
) -> None:
    from harnessed import proc

    monkeypatch.setattr(sys, "platform", platform)

    line = proc.describe()

    assert (line.seam, line.implementation) == ("process identity", implementation)
    assert (line.reason is None) is (reason is None)
    if reason:
        assert line.reason is not None and reason in line.reason


@pytest.mark.parametrize(
    ("platform", "systemd", "implementation", "reason"),
    [
        ("win32", True, "TaskSchedulerBackend", None),
        ("linux", True, "SystemdUserBackend", None),
        ("darwin", True, "LaunchdBackend", None),
        ("linux", False, "CrontabBackend", "no user systemd answered"),
    ],
)
def test_the_scheduler_line_matches_decision_95_on_each_os(
    platform: str, systemd: bool, implementation: str, reason: str | None
) -> None:
    from harnessed.tasks import installer

    def runner(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
        return (0, "") if systemd else (1, "Failed to connect to bus")

    line = installer.describe(installer.backend_for(platform, runner=runner))

    assert (line.seam, line.implementation) == ("scheduler", implementation)
    assert (line.reason is None) is (reason is None)
    if reason:
        assert line.reason is not None and line.reason.startswith(reason)


@pytest.mark.parametrize(
    ("platform", "implementation"),
    [
        ("win32", "UTF-8 on a redirected stdout"),
        ("linux", "native"),
        ("darwin", "native"),
    ],
)
def test_the_console_encoding_line_matches_decision_95_on_each_os(
    monkeypatch: pytest.MonkeyPatch, platform: str, implementation: str
) -> None:
    from harnessed.cli import render

    monkeypatch.setattr(sys, "platform", platform)

    line = render.describe()

    assert (line.seam, line.implementation, line.reason) == (
        "console encoding",
        implementation,
        None,
    )


def test_platform_section_calls_the_six_seams_in_order_and_branches_on_nothing() -> None:
    lines = doctor_cmd.platform_section()

    assert [line.seam for line in lines] == SEAM_ORDER
    assert all(line.implementation for line in lines)
    source = ast.parse((CLI_ROOT / "doctor_cmd.py").read_text(encoding="utf-8"))
    func = next(
        n
        for n in ast.walk(source)
        if isinstance(n, ast.FunctionDef) and n.name == "platform_section"
    )
    assert not [n for n in ast.walk(func) if isinstance(n, ast.If | ast.IfExp | ast.Try)]


def test_doctor_prints_the_platform_section_and_serves_it_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSED_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    plain = split.invoke(app, ["doctor", "--offline"])
    served = split.invoke(app, ["doctor", "--offline", "--json"])

    for seam in SEAM_ORDER:
        assert f"platform: {seam}:" in plain.stdout + plain.stderr
    platform_rows = json.loads(served.stdout)["platform"]
    assert [row["seam"] for row in platform_rows] == SEAM_ORDER
    assert all(set(row) == {"seam", "implementation", "reason"} for row in platform_rows)
    assert all(isinstance(row["implementation"], str) for row in platform_rows)
    assert all(row["reason"] is None or isinstance(row["reason"], str) for row in platform_rows)
    evidence_dir = REPO_ROOT / "evidence" / "322"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "platform-section.txt").write_text(
        "".join(f"{r['seam']}: {r['implementation']} ({r['reason']})\n" for r in platform_rows),
        encoding="utf-8",
    )


def test_with_only_a_groq_key_doctor_says_routing_is_off_and_which_key_turns_it_on(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 316 (FR-175): the router rung names the key, the agents rung names the condition."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "placeholder-not-a-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rungs = doctor_cmd.run_rungs(load(), agents_path=tmp_path / "agents.toml")

    assert rungs[4].detail.startswith("routing is off: set OPENROUTER_API_KEY to turn it on")
    assert "agents run only with routing on" in rungs[1].detail


def test_with_the_routing_key_doctor_does_not_say_routing_is_off(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "placeholder-not-a-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-not-a-key")
    rungs = doctor_cmd.run_rungs(load(), agents_path=tmp_path / "agents.toml")

    assert "routing is off" not in rungs[4].detail
    assert "agents run only with routing on" not in rungs[1].detail
