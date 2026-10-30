"""FR-29 / FR-42: rung 2 reads ``live`` only when a recorded ``agents test`` proves it (ticket 089).

``harnessed agents test`` records every result, passing and failing, in ``agent_probes``, and
doctor reads the newest row per declared agent. A pass counts only while it matches the entry's
current fingerprint, so editing the entry or reinstalling the program withdraws the proof, and
a newer failure outlives an older pass.

Probes are recorded *beside* ``delegate()`` and the delegation ledger, never through them: the
ledger is idempotent by design, and a probe that replayed a recorded outcome would report on the
ledger rather than on the agent.

No agent is named here. Every probe runs the stub agent through this interpreter, so the suite
stays offline.
"""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
from typer.testing import CliRunner

from harnessed.acp import PROTOCOL_VERSION, probes
from harnessed.acp import config as acp_config
from harnessed.cli import agents_cmd, doctor_cmd
from harnessed.cli.app import app, main
from harnessed.config import load
from harnessed.errors import AcpVersionMismatch, DelegationError, DelegationTimeout
from harnessed.memory import db

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"
STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_acp_agent.py"
EVIDENCE_DIR = REPO_ROOT / "evidence" / "089"
EVIDENCE_180 = REPO_ROOT / "evidence" / "180"

split = CliRunner(mix_stderr=False)


def stub_entry(name: str = "probe", *extra_args: str) -> str:
    args = [str(STUB_PATH), *extra_args]
    return (
        f"[agents.{name}]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps(args)}\n"
        "timeout_s = 20.0\n"
    )


CLI_STUB_PATH = REPO_ROOT / "tests" / "support" / "stub_cli_agent.py"


def cli_entry(name: str = "thin") -> str:
    """A ``kind = "cli"`` entry on the plain cli stub (ticket 221): no ACP handshake to answer."""
    return (
        f"[agents.{name}]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps([str(CLI_STUB_PATH), '--task', '{task}'])}\n"
        'kind = "cli"\ntimeout_s = 20.0\n'
    )


def write_agents(directory: Path, *entries: str) -> Path:
    target = directory / "agents.toml"
    target.write_text("\n".join(entries), encoding="utf-8")
    return target


def rows(home: Path) -> list[tuple[str, str, str, int, str, str]]:
    path = home / db.DATABASE_FILENAME
    with closing(sqlite3.connect(path)) as conn:
        return [
            (str(r[0]), str(r[1]), str(r[2]), int(r[3]), str(r[4]), str(r[5]))
            for r in conn.execute(
                "SELECT agent, fingerprint, status, protocol_version, detail, tested_at "
                "FROM agent_probes ORDER BY probe_id"
            )
        ]


def current_fingerprint(path: Path, name: str) -> str:
    spec = acp_config.load_agents(path)[name]
    return probes.fingerprint(spec, shutil.which(spec.command))


def seed(home: Path, agent: str, fingerprint: str, status: str, detail: str) -> probes.Probe:
    with closing(db.connect(home / db.DATABASE_FILENAME)) as conn:
        return probes.record(
            conn,
            agent=agent,
            fingerprint=fingerprint,
            status=status,
            protocol_version=PROTOCOL_VERSION,
            detail=detail,
        )


def shell(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    """Drive argv through ``main`` so a ``HarnessError`` is rendered to stderr (ticket 028)."""
    monkeypatch.setattr(sys, "argv", ["harnessed", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_info:
        code = 0 if exit_info.code is None else int(exit_info.code)
    return code, capsys.readouterr().err


def rung_two() -> doctor_cmd.Rung:
    return doctor_cmd.run_rungs(load())[1]


# ---------------------------------------------------------------------------
# 1. agents test records every result
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_passing_probe_records_a_passed_row(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    target = write_agents(tmp_path, stub_entry())
    result = split.invoke(app, ["agents", "test", "probe", "--agents", str(target)])
    assert result.exit_code == 0, result.stderr

    [row] = rows(tmp_harnessed_home)
    agent, fingerprint, status, version, detail, tested_at = row
    assert (agent, status, version) == ("probe", probes.PASSED, PROTOCOL_VERSION)
    assert fingerprint == current_fingerprint(target, "probe")
    assert detail and "\n" not in detail
    assert tested_at


@pytest.mark.slow
def test_a_dying_agent_records_a_failed_row_and_still_exits_non_zero(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = "import sys\nprint('the adapter could not start', file=sys.stderr)\nsys.exit(1)\n"
    target = tmp_path / "agents.toml"
    target.write_text(
        "[agents.probe]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps(['-c', script])}\n"
        "timeout_s = 20.0\n",
        encoding="utf-8",
    )
    code, err = shell(["agents", "test", "probe", "--agents", str(target)], monkeypatch, capsys)
    assert code != 0
    assert "the adapter could not start" in err

    [row] = rows(tmp_harnessed_home)
    assert row[2] == probes.FAILED
    assert "\n" not in row[4]
    assert row[4] and row[4] in err


@pytest.mark.parametrize("error", [DelegationError, AcpVersionMismatch, DelegationTimeout])
def test_each_delegation_failure_is_recorded_with_its_first_line_scrubbed(
    error: type[DelegationError],
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-" + "ant-api03-" + "A" * 40
    message = f"it broke holding {secret}\nsecond line of detail"

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise error(message)

    monkeypatch.setattr(agents_cmd, "delegate", refuse)
    target = write_agents(tmp_path, stub_entry())
    code, err = shell(["agents", "test", "probe", "--agents", str(target)], monkeypatch, capsys)
    assert code != 0
    assert "it broke holding" in err

    [row] = rows(tmp_harnessed_home)
    assert row[2] == probes.FAILED
    assert row[4].startswith("it broke holding")
    assert secret not in row[4]
    assert "second line" not in row[4]


@pytest.mark.slow
def test_a_proven_agent_is_launched_again_and_recorded_again(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = agents_cmd.delegate

    def counting(spec: acp_config.AgentSpec, *args: object, **kwargs: object) -> object:
        calls.append(spec.name)
        return real(spec, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(agents_cmd, "delegate", counting)
    target = write_agents(tmp_path, stub_entry())
    for _ in range(2):
        result = split.invoke(app, ["agents", "test", "probe", "--agents", str(target)])
        assert result.exit_code == 0, result.stderr
    assert calls == ["probe", "probe"]
    assert [row[2] for row in rows(tmp_harnessed_home)] == [probes.PASSED, probes.PASSED]


def test_agents_test_hands_delegate_no_ledger_store() -> None:
    """Probes are recorded beside the ledger: ``delegate`` gets no ``store`` from the probe."""
    tree = ast.parse((PACKAGE_ROOT / "cli" / "agents_cmd.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "delegate"
    ]
    assert calls
    assert all("store" not in {kw.arg for kw in call.keywords} for call in calls)
    body = (PACKAGE_ROOT / "acp" / "probes.py").read_text(encoding="utf-8")
    assert "delegations" not in body
    assert "ledger" not in {
        alias.name
        for node in ast.walk(ast.parse(body))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


# ---------------------------------------------------------------------------
# 2. The fingerprint
# ---------------------------------------------------------------------------


def test_the_fingerprint_follows_the_launch_and_ignores_policy() -> None:
    spec = acp_config.AgentSpec(name="probe", command="agent-bin", args=("--acp",))
    base = probes.fingerprint(spec, "/usr/bin/agent-bin")
    assert base == probes.fingerprint(spec, "/usr/bin/agent-bin")
    assert base == probes.fingerprint(
        replace(spec, mode="deny", allow=("read",), deny=("write",)), "/usr/bin/agent-bin"
    )
    assert base != probes.fingerprint(replace(spec, command="other-bin"), "/usr/bin/agent-bin")
    assert base != probes.fingerprint(replace(spec, args=()), "/usr/bin/agent-bin")
    assert base != probes.fingerprint(replace(spec, cwd="/elsewhere"), "/usr/bin/agent-bin")
    assert base != probes.fingerprint(spec, "/opt/bin/agent-bin")


def test_the_fingerprint_follows_session_meta_and_tool_names_so_a_stale_probe_is_withdrawn() -> (
    None
):
    spec = acp_config.AgentSpec(name="probe", command="agent-bin", args=("--acp",))
    meta = replace(spec, session_meta={"vendor": {"options": {"sources": []}}})
    named = replace(meta, tool_names={"execute": ("Run",)})
    exe = "/usr/bin/agent-bin"
    prints = {probes.fingerprint(s, exe) for s in (spec, meta, named)}
    assert len(prints) == 3


def test_latest_is_the_newest_row_even_on_a_tied_clock(
    tmp_harnessed_home: Path, frozen_clock: object
) -> None:
    seed(tmp_harnessed_home, "probe", "fp", probes.PASSED, "fine")
    seed(tmp_harnessed_home, "probe", "fp", probes.FAILED, "broke")
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        newest = probes.latest(conn, "probe")
        assert probes.latest(conn, "nobody") is None
    assert newest is not None
    assert (newest.status, newest.detail) == (probes.FAILED, "broke")


# ---------------------------------------------------------------------------
# 3. Doctor's rung 2
# ---------------------------------------------------------------------------


def test_rung_two_is_live_when_every_agent_holds_a_matching_pass(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write_agents(tmp_path, stub_entry("probe"), stub_entry("other"))
    monkeypatch.chdir(tmp_path)
    for name in ("probe", "other"):
        proof = seed(
            tmp_harnessed_home, name, current_fingerprint(target, name), probes.PASSED, "ok"
        )
    rung = rung_two()
    assert rung.state == "live", rung.detail
    assert "probe" in rung.detail and "other" in rung.detail
    assert proof.tested_at[:10] in rung.detail
    assert split.invoke(app, ["doctor", "--offline"]).exit_code == 0


def test_an_entry_changed_since_its_proof_is_declared_and_names_the_test(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write_agents(tmp_path, stub_entry())
    monkeypatch.chdir(tmp_path)
    seed(tmp_harnessed_home, "probe", current_fingerprint(target, "probe"), probes.PASSED, "ok")
    write_agents(tmp_path, stub_entry("probe", "--changed"))
    rung = rung_two()
    assert rung.state == "declared"
    assert "changed since" in rung.detail
    assert "harnessed agents test probe" in rung.detail


def test_a_newest_matching_failure_is_broken_and_fails_doctor(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write_agents(tmp_path, stub_entry())
    monkeypatch.chdir(tmp_path)
    fingerprint = current_fingerprint(target, "probe")
    seed(tmp_harnessed_home, "probe", fingerprint, probes.PASSED, "ok")
    failure = seed(tmp_harnessed_home, "probe", fingerprint, probes.FAILED, "handshake refused")
    rung = rung_two()
    assert rung.state == "broken"
    assert "handshake refused" in rung.detail
    assert failure.tested_at[:10] in rung.detail
    assert split.invoke(app, ["doctor", "--offline"]).exit_code != 0


def test_the_rung_takes_the_weakest_state_and_lists_each_agent(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write_agents(tmp_path, stub_entry("alpha"), stub_entry("beta"), stub_entry("gamma"))
    monkeypatch.chdir(tmp_path)
    seed(tmp_harnessed_home, "alpha", current_fingerprint(target, "alpha"), probes.PASSED, "ok")
    mixed = rung_two()
    assert mixed.state == "declared"
    for name in ("alpha", "beta", "gamma"):
        assert name in mixed.detail
    assert "live" in mixed.detail

    fingerprint = current_fingerprint(target, "beta")
    seed(tmp_harnessed_home, "beta", fingerprint, probes.FAILED, "beta fell over")
    worst = rung_two()
    assert worst.state == "broken"
    assert "beta fell over" in worst.detail
    assert "alpha" in worst.detail and "gamma" in worst.detail


def test_no_database_means_no_proof_and_none_is_created(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agents(tmp_path, stub_entry())
    monkeypatch.chdir(tmp_path)
    assert rung_two().state == "declared"
    assert not (tmp_harnessed_home / db.DATABASE_FILENAME).exists()


def test_a_database_behind_migration_eight_reads_declared_and_is_left_alone(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = tmp_path / "migrations"
    older.mkdir()
    for migration in db.load_migrations():
        if migration.version < probes.SCHEMA_VERSION:
            source = Path(db.__file__).parent / "migrations" / migration.filename
            shutil.copy(source, older / source.name)
    path = tmp_harnessed_home / db.DATABASE_FILENAME
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        db.migrate(conn, older)
        assert db.schema_version(conn) == probes.SCHEMA_VERSION - 1

    write_agents(tmp_path, stub_entry())
    monkeypatch.chdir(tmp_path)
    assert rung_two().state == "declared"
    with closing(sqlite3.connect(path)) as conn:
        assert db.schema_version(conn) == probes.SCHEMA_VERSION - 1


# ---------------------------------------------------------------------------
# 4. Evidence: a pass, a failure, a changed entry, and rung 2 for each
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_evidence_transcript(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = write_agents(tmp_path, stub_entry())
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    def snapshot(label: str) -> None:
        rung = rung_two()
        newest = rows(tmp_harnessed_home)[-1] if rows(tmp_harnessed_home) else None
        lines.append(f"## {label}")
        if newest is not None:
            lines.append(f"newest row: agent={newest[0]} status={newest[2]} detail={newest[4]}")
        lines.append(f"rung 2  {rung.state:<8} {rung.detail}")
        lines.append("")

    code, _ = shell(["agents", "test", "probe", "--agents", str(target)], monkeypatch, capsys)
    assert code == 0
    snapshot("a passing probe")
    assert rung_two().state == "live"

    real = agents_cmd.delegate

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise AcpVersionMismatch("the agent answered initialize with protocol version 99")

    monkeypatch.setattr(agents_cmd, "delegate", refuse)
    code, _ = shell(["agents", "test", "probe", "--agents", str(target)], monkeypatch, capsys)
    assert code != 0
    snapshot("a failing probe")
    assert rung_two().state == "broken"

    monkeypatch.setattr(agents_cmd, "delegate", real)
    shell(["agents", "test", "probe", "--agents", str(target)], monkeypatch, capsys)
    write_agents(tmp_path, stub_entry("probe", "--changed"))
    snapshot("the entry changed after its proof")
    assert rung_two().state == "declared"

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "probes.txt").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 4. GET /v1/agents and `agents list --json` (FR-90, ticket 148)
# ---------------------------------------------------------------------------

EVIDENCE_148 = REPO_ROOT / "evidence" / "148"

#: The keys every row carries, probed or not: nulls where no probe exists, never absent keys.
#: `kind` through `confined` are the registry fields (decision 62, FR-120, ticket 180): read
#: regardless of whether the entry was ever probed.
AGENT_ROW_KEYS = {
    "agent",
    "status",
    "protocol_version",
    "detail",
    "tested_at",
    "fingerprint_matches",
    "probed",
    "kind",
    "use_for",
    "not_for",
    "billing",
    "produces",
    "confined",
    "settings",
    "jail",
    "confined_reason",
}


def four_states(home: Path, directory: Path) -> Path:
    """A registry of four agents, one per card word: live, stale, failed, unproven.

    ``stale`` is a pass taken against the entry before its args changed, and ``failed`` a newest
    failure against the entry as it stands, the two ticket 089 rules doctor already reads.
    """
    target = write_agents(directory, stub_entry("live"), stub_entry("stale", "--before"))
    seed(home, "stale", current_fingerprint(target, "stale"), probes.PASSED, "ok before")
    target = write_agents(
        directory,
        stub_entry("live"),
        stub_entry("stale", "--after"),
        stub_entry("failed"),
        stub_entry("unproven"),
    )
    seed(home, "live", current_fingerprint(target, "live"), probes.PASSED, "handshake done")
    seed(home, "failed", current_fingerprint(target, "failed"), probes.PASSED, "an older pass")
    seed(home, "failed", current_fingerprint(target, "failed"), probes.FAILED, "handshake refused")
    return target


def agents_body(home: Path) -> bytes:
    from fastapi.testclient import TestClient

    from harnessed.gateway import create_app

    with TestClient(create_app(home / db.DATABASE_FILENAME)) as client:
        response = client.get("/v1/agents")
    assert response.status_code == 200, response.text
    return response.content


def test_states_serves_one_row_per_declared_agent_in_the_registry_order(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    target = four_states(tmp_harnessed_home, tmp_path)
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        found = probes.states(conn, target)
    assert [row.agent for row in found] == ["live", "stale", "failed", "unproven"]
    by_name = {row.agent: row for row in found}
    assert (by_name["live"].status, by_name["live"].fingerprint_matches) == (probes.PASSED, True)
    assert (by_name["stale"].status, by_name["stale"].fingerprint_matches) == (
        probes.PASSED,
        False,
    )
    assert by_name["failed"].status == probes.FAILED, "the newest probe, not the older pass"
    assert by_name["failed"].detail == "handshake refused"
    assert by_name["failed"].fingerprint_matches is True
    assert by_name["live"].protocol_version == PROTOCOL_VERSION
    unproven = by_name["unproven"]
    assert unproven.probed is False
    assert (
        unproven.status,
        unproven.protocol_version,
        unproven.detail,
        unproven.tested_at,
        unproven.fingerprint_matches,
    ) == (None, None, None, None, None)
    assert all(row.probed for row in found if row.agent != "unproven")
    payload = probes.json_payload(found)
    assert list(payload) == ["agents"]
    for row in payload["agents"]:
        assert set(row) == AGENT_ROW_KEYS, row


def test_a_cli_entry_carries_the_registry_fields_and_is_unconfined(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 (FR-120, decision 62): a ``kind = "cli"`` entry serves the four registry fields,
    ``produces`` and ``confined: false`` - stated rather than implied, never inferred from the
    absence of an ACP handshake. On a host with no jail (ticket 214 pins that case)."""
    from harnessed.acp import jail

    monkeypatch.setattr(jail, "detect", lambda: jail.Jail(jail.NONE))
    entry = (
        stub_entry("thin")
        + 'kind = "cli"\n'
        + 'use_for = "quick text edits"\n'
        + 'not_for = "anything needing a diagram"\n'
        + 'billing = "oauth"\n'
        + 'produces = "text"\n'
    )
    target = write_agents(tmp_path, entry)
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        (row,) = probes.states(conn, target)
    assert (row.kind, row.use_for, row.not_for, row.billing, row.produces, row.confined) == (
        "cli",
        "quick text edits",
        "anything needing a diagram",
        "oauth",
        "text",
        False,
    )
    payload = probes.json_payload((row,))["agents"][0]
    assert (payload["kind"], payload["confined"]) == ("cli", False)
    EVIDENCE_180.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_180 / "agents-body.txt").write_text(
        'ticket 180 - GET /v1/agents: a `kind = "cli"` entry\'s registry fields\n\n'
        + json.dumps(probes.json_payload((row,)), indent=2)
        + "\n",
        encoding="utf-8",
    )


def test_states_is_empty_without_a_registry(clean_env: None, tmp_harnessed_home: Path) -> None:
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        assert probes.states(conn, tmp_harnessed_home / "agents.toml") == ()
    assert probes.json_payload(()) == {"agents": []}


def test_the_route_and_agents_list_json_are_the_same_bytes(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway reads ``agents.toml`` where ``agents test`` and doctor do: the working dir."""
    four_states(tmp_harnessed_home, tmp_path)
    monkeypatch.chdir(tmp_path)
    body = agents_body(tmp_harnessed_home)
    result = split.invoke(app, ["agents", "list", "--json"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.encode("utf-8") == body + b"\n"
    rows_ = json.loads(body)["agents"]
    assert [row["agent"] for row in rows_] == ["live", "stale", "failed", "unproven"]
    assert rows_[3]["probed"] is False and rows_[3]["status"] is None

    prose = split.invoke(app, ["agents", "list"])
    assert prose.exit_code == 0, prose.stderr
    for word in ("live", "stale", "failed", "unproven"):
        assert word in prose.stdout
    assert "harnessed agents test unproven" in prose.stdout

    monkeypatch.chdir(tmp_harnessed_home)  # no registry here
    empty = agents_body(tmp_harnessed_home)
    assert empty == b'{"agents":[]}'
    listed = split.invoke(app, ["agents", "list", "--json"])
    assert listed.stdout.encode("utf-8") == empty + b"\n"

    identical = result.stdout.encode("utf-8") == body + b"\n"
    EVIDENCE_148.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_148 / "agents-payload.txt").write_text(
        f"GET /v1/agents (four declared agents, one never probed)\n{body.decode()}\n\n"
        f"harnessed agents list --json\n{result.stdout}"
        f"byte-identical: {identical}\n\n"
        f"harnessed agents list\n{prose.stdout}\n"
        f"GET /v1/agents with no agents.toml\n{empty.decode()}\n",
        encoding="utf-8",
    )


#: What an OAuth flow would have to name somewhere: a library, a grant, a token, a callback route.
OAUTH_MARKERS = (
    "oauth",
    "authlib",
    "access_token",
    "refresh_token",
    "authorization_code",
    "client_secret",
    "redirect_uri",
)

#: The modules allowed to spell the bare word "oauth", and only that one marker: decision 62/63
#: (ticket 157) give an ACP entry a `billing = "oauth"` kind, meaning its vendor login is never
#: proxied - a billing label, not an authentication flow. Every other marker (a library, a grant,
#: a token, a callback route) is still banned in these files too - the same named carve-out shape
#: `test_agents_onboarding.py`'s ``TEMPLATE_MODULE`` uses for the agent-name scan.
#:
#: The two dashboard files render that served label and nothing more: `lib/human-words.ts` holds
#: the `billing:oauth` row `tests/test_dashboard_vocabulary.py` requires for every word the ledger
#: can emit (ticket 184), and `components/CostLine.tsx` reads the served word to show an
#: API-equivalent rather than a cost (FR-115, ticket 189). Both are named here, never a folder.
OAUTH_WORD_EXEMPT: Final = frozenset(
    {
        PACKAGE_ROOT / "acp" / "config.py",
        REPO_ROOT / "dashboard" / "lib" / "human-words.ts",
        REPO_ROOT / "dashboard" / "components" / "CostLine.tsx",
    }
)


def test_there_are_zero_oauth_routes_callbacks_tokens_or_libraries(
    tmp_harnessed_home: Path,
) -> None:
    """FR-90: OAuth is rejected, and the dependency list is what MUST rule 9 pins."""
    from harnessed.gateway import create_app

    scanned = [
        *sorted(PACKAGE_ROOT.rglob("*.py")),
        *sorted(
            path
            for folder in ("app", "components", "lib")
            for path in (REPO_ROOT / "dashboard" / folder).rglob("*")
            if path.suffix in {".ts", ".tsx"}
        ),
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "dashboard" / "package.json",
    ]
    hits = [
        f"{path.relative_to(REPO_ROOT)}: {marker}"
        for path in scanned
        for marker in OAUTH_MARKERS
        if marker in path.read_text(encoding="utf-8").lower()
        if not (marker == "oauth" and path in OAUTH_WORD_EXEMPT)
    ]
    assert hits == []
    routes = [getattr(route, "path", "") for route in create_app(tmp_harnessed_home / "x").routes]
    assert [p for p in routes if any(w in p for w in ("oauth", "callback", "token", "auth"))] == []
    EVIDENCE_148.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_148 / "oauth-scan.txt").write_text(
        f"scanned {len(scanned)} files (src/harnessed, dashboard app/components/lib, "
        f"pyproject.toml, dashboard/package.json) for {', '.join(OAUTH_MARKERS)}\n"
        f"hits: {len(hits)}\n"
        f"gateway routes: {', '.join(routes)}\n"
        f"routes naming oauth/callback/token/auth: 0\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 5. The login word (FR-116, ticket 175)
# ---------------------------------------------------------------------------

EVIDENCE_175 = REPO_ROOT / "evidence" / "175"

#: The probe table's columns: the login word rides in ``detail``, so no migration adds one.
PROBE_COLUMNS = ["probe_id", "agent", "fingerprint", "status", "protocol_version", "detail"]


def subscription_entry(name: str, *extra_args: str) -> str:
    """A stub entry billed as a subscription, the kind whose login a probe reports."""
    return stub_entry(name, *extra_args) + f'billing = "{acp_config.BILLING_SUBSCRIPTION}"\n'


@pytest.mark.slow
@pytest.mark.parametrize(
    ("extra", "word"),
    [(("--auth-methods",), probes.LOGIN_LIVE), ((), probes.LOGIN_UNKNOWN)],
    ids=["handshake-reports-a-login", "handshake-says-nothing"],
)
def test_a_subscription_probe_records_its_login_word_and_doctor_prints_it(
    extra: tuple[str, ...],
    word: str,
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = write_agents(tmp_path, subscription_entry("probe", *extra))
    monkeypatch.chdir(tmp_path)
    result = split.invoke(app, ["agents", "test", "probe", "--agents", str(target)])
    assert result.exit_code == 0, result.stderr
    assert word in result.stdout

    [row] = rows(tmp_harnessed_home)
    assert row[2] == probes.PASSED
    assert row[4].startswith(f"{word}: ")
    assert probes.login_word(row[4]) == word

    rung = rung_two()
    assert rung.state == "live"
    assert f"probe live, proven {row[5][:10]}, {word}" in rung.detail
    assert word in split.invoke(app, ["doctor", "--offline"]).stdout


@pytest.mark.slow
def test_an_entry_not_billed_as_a_subscription_carries_no_login_word(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write_agents(tmp_path, stub_entry("probe", "--auth-methods"))
    monkeypatch.chdir(tmp_path)
    result = split.invoke(app, ["agents", "test", "probe", "--agents", str(target)])
    assert result.exit_code == 0, result.stderr
    [row] = rows(tmp_harnessed_home)
    assert probes.login_word(row[4]) is None
    assert "login" not in rung_two().detail


def test_the_login_word_adds_no_column_and_no_migration(tmp_harnessed_home: Path) -> None:
    assert probes.SCHEMA_VERSION == 8
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        columns = [str(r[1]) for r in conn.execute("PRAGMA table_info(agent_probes)")]
    assert columns[: len(PROBE_COLUMNS)] == PROBE_COLUMNS
    assert columns == [*PROBE_COLUMNS, "tested_at"]


def test_a_failed_probe_and_a_bare_detail_read_no_login_word() -> None:
    assert probes.login_word("the adapter could not start") is None
    assert probes.login_word(f"{probes.LOGIN_LIVE}: ok") == probes.LOGIN_LIVE
    assert probes.with_login(None, "ok") == f"{probes.LOGIN_UNKNOWN}: ok"
    assert probes.with_login(True, "ok") == f"{probes.LOGIN_LIVE}: ok"


@pytest.mark.slow
def test_evidence_login_word(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write_agents(
        tmp_path,
        subscription_entry("live", "--auth-methods"),
        subscription_entry("silent"),
        stub_entry("unbilled"),
    )
    monkeypatch.chdir(tmp_path)
    lines = ["## harnessed agents test <name>, one per entry", ""]
    for name in ("live", "silent", "unbilled"):
        result = split.invoke(app, ["agents", "test", name, "--agents", str(target)])
        assert result.exit_code == 0, result.stderr
        lines.append(f"$ harnessed agents test {name}")
        lines.extend(result.stdout.splitlines())
        lines.append("")
    rung = rung_two()
    assert rung.state == "live"
    lines.append("## doctor rung 2")
    lines.append(f"rung 2  {rung.state:<8} {rung.detail}")
    EVIDENCE_175.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_175 / "login.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("kind", "expected"), [("bwrap", True), ("job_object", False), ("none", False)]
)
def test_a_cli_entry_is_confined_only_where_detect_says_bwrap(
    kind: str,
    expected: bool,
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticket 214: measured per host, and the served boolean stays a bool."""
    from harnessed.acp import jail

    monkeypatch.setattr(jail, "detect", lambda: jail.Jail(kind))
    target = write_agents(tmp_path, stub_entry("thin") + 'kind = "cli"\n')
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        (row,) = probes.states(conn, target)
    assert row.confined is expected


def test_an_acp_entry_is_confined_only_with_a_session_meta(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    """Ticket 213: ``confined`` means launched under Harnessed's settings, and stays a bool."""
    bare = stub_entry("bare")
    meta = stub_entry("meta") + 'session_meta = {"vendor" = {"options" = {"sources" = []}}}\n'
    target = write_agents(tmp_path, bare, meta)
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        found = {row.agent: row.confined for row in probes.states(conn, target)}
    assert found == {"bare": False, "meta": True}
    assert all(type(value) is bool for value in found.values())


def test_a_probe_taken_before_session_meta_changed_reads_as_stale(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    target = write_agents(tmp_path, stub_entry("probe") + 'session_meta = {"v" = {"o" = 1}}\n')
    spec = acp_config.load_agents(target)["probe"]
    exe = shutil.which(spec.command)
    old = probes.fingerprint(replace(spec, session_meta={}), exe)
    seed(tmp_harnessed_home, "probe", old, probes.PASSED, "fine")
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        (row,) = probes.states(conn, target)
    assert row.fingerprint_matches is False


# ---------------------------------------------------------------------------
# ticket 217: the probe row carries the jail kind and the self-test's reason
# ---------------------------------------------------------------------------


def test_with_jail_leads_the_detail_with_the_kind_and_a_scrubbed_reason() -> None:
    from harnessed.acp import jail

    assert probes.with_jail(jail.Jail("bwrap"), "ok") == "jail=bwrap: ok"
    refused = jail.Jail("none", reason="No permitted user namespace")
    assert probes.with_jail(refused, "ok") == "jail=none (No permitted user namespace): ok"
    secret = "sk-or-v1-" + "a1b2c3d4" * 6
    assert secret not in probes.with_jail(jail.Jail("none", reason=secret), "ok")
    assert probes.login_word("jail=bwrap: ok") is None


@pytest.mark.parametrize(
    ("found", "prefix"),
    [
        ("bare", "jail=none: "),
        ("refused", "jail=none (user namespaces are disabled): "),
    ],
)
@pytest.mark.usefixtures("no_child_coverage")
def test_agents_test_records_the_jail_on_a_cli_entry_probe(
    found: str,
    prefix: str,
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harnessed.acp import jail

    answer = (
        jail.Jail(jail.NONE, reason="user namespaces are disabled")
        if found == "refused"
        else jail.Jail(jail.NONE)
    )
    monkeypatch.setattr(jail, "detect", lambda: answer)
    target = write_agents(tmp_path, cli_entry("thin"))
    result = split.invoke(app, ["agents", "test", "thin", "--agents", str(target)])
    assert result.exit_code == 0, result.stderr
    [row] = rows(tmp_harnessed_home)
    assert row[4].startswith(prefix)


# ticket 221: `agents test` runs a cli entry as a cli entry, not an ACP handshake


def _spy_delegate(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def refuse(*args: object, **kwargs: object) -> object:
        calls.append(args)
        raise AssertionError("a cli entry must not open an ACP connection")

    monkeypatch.setattr(agents_cmd, "delegate", refuse)
    return calls


@pytest.mark.usefixtures("no_child_coverage")
def test_a_cli_probe_runs_the_cli_runner_and_opens_no_acp_connection(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harnessed.acp import jail

    detected = jail.detect()
    kind = detected.kind
    calls = _spy_delegate(monkeypatch)
    target = write_agents(tmp_path, cli_entry())
    result = split.invoke(app, ["agents", "test", "thin", "--agents", str(target)])
    assert result.exit_code == 0, result.stderr
    assert calls == []
    assert "agent thin: succeeded" in result.stdout
    assert f"jail {kind}" in result.stdout
    assert "Reply with one short sentence" in result.stdout
    lowered = result.stdout.lower()
    assert "protocol version" not in lowered
    assert "login" not in lowered
    [row] = rows(tmp_harnessed_home)
    assert (row[2], row[3]) == ("passed", 0)
    assert row[4] == probes.with_jail(
        detected,
        "Reply with one short sentence confirming you received this prompt. Change no files.",
    )


@pytest.mark.usefixtures("no_child_coverage")
def test_a_cli_probe_json_carries_kind_and_a_null_protocol_version(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spy_delegate(monkeypatch)
    target = write_agents(tmp_path, cli_entry(), stub_entry("rich"))
    result = split.invoke(app, ["agents", "test", "thin", "--json", "--agents", str(target)])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["agent"] == "thin" and payload["kind"] == "cli"
    assert payload["protocol_version"] is None and payload["status"] == "succeeded"
    monkeypatch.undo()
    # `undo` also drops the test's own home and cwd; put them back or the probe reads the real ones.
    monkeypatch.setenv("HARNESSED_HOME", str(tmp_harnessed_home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESSED_PROVIDER", "fake")
    acp = split.invoke(app, ["agents", "test", "rich", "--json", "--agents", str(target)])
    assert acp.exit_code == 0, acp.stderr
    assert json.loads(acp.stdout)["protocol_version"] == PROTOCOL_VERSION
    assert json.loads(acp.stdout)["kind"] == "acp"


@pytest.mark.usefixtures("no_child_coverage")
def test_states_serves_null_for_a_probed_cli_entry(
    clean_env: None, tmp_harnessed_home: Path, tmp_path: Path
) -> None:
    target = write_agents(tmp_path, cli_entry())
    result = split.invoke(app, ["agents", "test", "thin", "--agents", str(target)])
    assert result.exit_code == 0, result.stderr
    with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
        [state] = probes.states(conn, target)
    assert state.probed and state.status == "passed" and state.protocol_version is None


@pytest.mark.parametrize(("env", "reason"), [("HARNESSED_STUB_EXIT_CODE", "exit_code")])
@pytest.mark.usefixtures("no_child_coverage")
def test_a_failing_cli_probe_is_recorded_failed_with_the_cli_reason(
    env: str,
    reason: str,
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(env, "3")
    target = write_agents(tmp_path, cli_entry())
    code, err = shell(["agents", "test", "thin", "--agents", str(target)], monkeypatch, capsys)
    assert code != 0 and reason in err
    [row] = rows(tmp_harnessed_home)
    assert row[2] == "failed" and reason in row[4]


@pytest.mark.usefixtures("no_child_coverage")
def test_a_cli_probe_that_times_out_is_recorded_failed_as_a_timeout(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HARNESSED_STUB_SLEEP_S", "30")
    entry = cli_entry().replace("timeout_s = 20.0", "timeout_s = 1.0")
    target = write_agents(tmp_path, entry)
    code, err = shell(["agents", "test", "thin", "--agents", str(target)], monkeypatch, capsys)
    assert code != 0 and "timeout" in err
    [row] = rows(tmp_harnessed_home)
    assert row[2] == "failed" and "timeout" in row[4]


def test_each_confinement_case_serves_settings_jail_and_reason(
    clean_env: None,
    tmp_harnessed_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-131 (G-60): the registry serves settings, jail and confined_reason per entry."""
    from harnessed.acp import jail

    bare_acp = next(
        n for n, t in acp_config.TEMPLATES.items() if not t.session_meta and t.kind == "acp"
    )
    meta = stub_entry("meta") + 'session_meta = {"vendor" = {"options" = {"sources" = []}}}\n'
    cli = stub_entry("thin") + 'kind = "cli"\n'
    target = write_agents(tmp_path, meta, stub_entry(bare_acp), cli)

    def served(found: jail.Jail) -> dict[str, tuple[object, ...]]:
        monkeypatch.setattr(jail, "detect", lambda: found)
        with closing(db.connect(tmp_harnessed_home / db.DATABASE_FILENAME)) as conn:
            rows = probes.states(conn, target)
        return {r.agent: (r.settings, r.jail, r.confined_reason) for r in rows}

    refused = served(jail.Jail(jail.NONE, reason="user namespaces refused"))
    assert refused["meta"] == ("harnessed", None, None)
    assert refused[bare_acp] == ("folder", None, "its adapter documents no override")
    assert refused["thin"] == (None, "none", "user namespaces refused")
    assert served(jail.Jail(jail.BWRAP))["thin"] == (None, "bwrap", None)
    assert served(jail.Jail(jail.JOB_OBJECT))["thin"] == (None, "job_object", None)
