"""FR-35: ``--json`` on the last two commands without it, and the walk that keeps it that way.

``harnessed run`` and the read-shaped ``harnessed memory`` subcommands were the last
decision-producing commands with no ``--json`` flag. This suite pins the flag on each of them, and
pins the rule the flag obeys everywhere else in the CLI: a single JSON object on stdout and nothing
else, in the one canonical spelling ``serialize.canonical`` owns, carrying exactly what the
subsystem callable returned.

The last test walks the Typer command tree rather than checking a list of names, so a command added
later without the flag fails here instead of being forgotten. The commands outside the set are named
with a reason each, and the walk asserts the boundary in both directions: a name in the exemption
map that no longer exists, or an exempt command that has since grown the flag, fails too.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import click
import pytest
import typer
from typer.testing import CliRunner

from harnessed.cli.app import app, main
from harnessed.events.bus import EventBus
from harnessed.loop import runs
from harnessed.memory import db, episodic, procedural, semantic
from harnessed.serialize import canonical

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "067"

PROMPT = "ping"
JSON_FLAG: Final = "--json"

#: The leaves the tree walk does not require to carry ``--json``, each with the reason
#: ``architecture.md`` gives ("FR-35 adds the flag to ``harnessed run`` and the read-shaped
#: ``harnessed memory`` subcommands, the last two without one"). This is an exemption map with a
#: reason per entry, not a list of commands that must have the flag: a command added later is
#: caught by default, and an entry here that stops being true fails the walk.
NOT_DECISION_PRODUCING: Final[dict[str, str]] = {
    "serve": "a server that never completes; per-request output would break the one-object rule",
    "dashboard": "a server that never completes, for the same reason as `serve` (decision 46)",
    "learn": "prints a shipped walkthrough as prose, not a decision (decision 106)",
    "gate": "its decision is the exit code, and the scored run it renders is `eval show --json`'s",
    "memory add": "a write, not a read-shaped subcommand; it prints the id it stored under",
    "memory archive": "a write, not a read-shaped subcommand; it prints the row it retired",
    "prices refresh": "a write that asks the network; `prices list --json` reads what it stored",
    "sessions stream": "the SSE stream, decision 65's first named exception: frames, not an object",
    "permissions answer": "a write, not a read-shaped subcommand; it prints the line it recorded",
    "schedule install": "a write to the OS scheduler; it prints the entry it wrote (decision 79)",
    "schedule uninstall": "a write to the OS scheduler; it prints what it removed (decision 79)",
    "outputs get": (
        "writes bytes to --out, not JSON to stdout; decision 65's second named exception "
        "(the SSE stream is the first)"
    ),
}

runner = CliRunner()
split = CliRunner(mix_stderr=False)


def capture(name: str, text: str) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against the throwaway home, opened the way the CLI opens it."""
    connection = db.connect()
    yield connection
    connection.close()


def one_object(stdout: str) -> dict[str, object]:
    """Exactly one JSON object on one line, and nothing else."""
    assert stdout.endswith("\n"), f"--json did not end its one line:\n{stdout!r}"
    text = stdout[:-1]
    assert "\n" not in text, f"--json wrote more than one line:\n{stdout}"
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


def assert_canonical(stdout: str) -> dict[str, object]:
    """The round-trip rule: ``canonical(json.loads(stdout))`` is the stdout line itself."""
    payload = one_object(stdout)
    assert canonical(payload) == stdout[:-1]
    return payload


def projected(items: tuple[object, ...]) -> list[dict[str, object]]:
    """The dataclass projection the wire carries: field names as keys, nothing renamed."""
    return [dataclasses.asdict(item) for item in items]  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# 1. harnessed run --json
# ---------------------------------------------------------------------------


def test_run_json_is_one_object_on_stdout_and_nothing_on_stderr(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    result = split.invoke(app, ["run", PROMPT, JSON_FLAG])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stderr == ""
    payload = assert_canonical(result.stdout)
    assert payload["key_kind"] == "session"
    assert payload["iterations"] >= 1  # type: ignore[operator]
    capture("run-json.txt", f"$ harnessed run {PROMPT} --json\n{result.stdout}")


def test_run_json_is_the_run_payload_the_journal_holds(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """The command serializes ``loop/runs.py::run_payload`` and adds nothing to it."""
    result = split.invoke(app, ["run", PROMPT, JSON_FLAG])
    assert result.exit_code == 0, result.stderr
    payload = one_object(result.stdout)

    run_key = payload["run_key"]
    assert isinstance(run_key, str)
    assert payload == runs.run_payload(conn, run_key)


def test_run_without_the_flag_renders_as_before(clean_env: None, conn: sqlite3.Connection) -> None:
    """Adding ``--json`` changes nothing an operator without it sees."""
    result = split.invoke(app, ["run", PROMPT])
    assert result.exit_code == 0, result.stderr
    (episode,) = episodic.for_run(conn, _only_run_key(conn))
    assert episode.run_id in result.stdout
    assert "iteration(s)" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def _only_run_key(conn: sqlite3.Connection) -> str:
    (row,) = runs.listing(conn)
    return row.run_key


# ---------------------------------------------------------------------------
# 2. harnessed memory {search, procedures, episodes} --json
# ---------------------------------------------------------------------------


def test_memory_search_json_is_the_facts_search_returned(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    first = semantic.put(conn, "the harness runs offline by default", source="test", tags=("mode",))
    semantic.put(conn, "the harness is offline in CI too", source="test", links=(first.mem_id,))
    semantic.put(conn, "unrelated: the sky is blue", source="test")

    result = split.invoke(app, ["memory", "search", "offline", JSON_FLAG])
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    payload = assert_canonical(result.stdout)

    facts = semantic.search(conn, "offline", limit=10, include_archived=False)
    assert len(facts) == 2
    assert canonical(payload) == canonical({"facts": projected(facts)})
    capture("memory-json.txt", f"$ harnessed memory search offline --json\n{result.stdout}")


def test_memory_search_json_honours_limit_and_archived(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    kept = semantic.put(conn, "offline one", source="test")
    gone = semantic.put(conn, "offline two", source="test")
    semantic.archive(conn, gone.mem_id)

    default = one_object(split.invoke(app, ["memory", "search", "offline", JSON_FLAG]).stdout)
    assert canonical(default) == canonical({"facts": projected((kept,))})

    with_archived = split.invoke(
        app, ["memory", "search", "offline", "--archived", "--limit", "1", JSON_FLAG]
    )
    payload = assert_canonical(with_archived.stdout)
    facts = semantic.search(conn, "offline", limit=1, include_archived=True)
    assert canonical(payload) == canonical({"facts": projected(facts)})


def test_memory_procedures_json_is_the_active_set(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    first = procedural.put(conn, "Answer in one sentence.", source="test")
    procedural.activate(conn, first.proc_id, cand_id=None)
    second = procedural.put(conn, "Answer in two sentences.", source="test")
    procedural.activate(conn, second.proc_id, cand_id=None)

    result = split.invoke(app, ["memory", "procedures", JSON_FLAG])
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    payload = assert_canonical(result.stdout)
    assert canonical(payload) == canonical({"procedures": projected(procedural.active(conn))})
    assert len(payload["procedures"]) == 1  # type: ignore[arg-type]

    every = assert_canonical(split.invoke(app, ["memory", "procedures", "--all", JSON_FLAG]).stdout)
    assert canonical(every) == canonical({"procedures": projected(procedural.all(conn))})
    assert len(every["procedures"]) == 2  # type: ignore[arg-type]
    with open(EVIDENCE_DIR / "memory-json.txt", "a", encoding="utf-8") as handle:
        handle.write(f"$ harnessed memory procedures --json\n{result.stdout}")


def test_memory_episodes_json_is_what_the_run_recorded(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    ran = split.invoke(app, ["run", PROMPT, JSON_FLAG])
    run_key = one_object(ran.stdout)["run_key"]
    assert isinstance(run_key, str)

    result = split.invoke(app, ["memory", "episodes", run_key, JSON_FLAG])
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    payload = assert_canonical(result.stdout)
    assert canonical(payload) == canonical({"episodes": projected(episodic.for_run(conn, run_key))})
    assert len(payload["episodes"]) == 1  # type: ignore[arg-type]
    with open(EVIDENCE_DIR / "memory-json.txt", "a", encoding="utf-8") as handle:
        handle.write(f"$ harnessed memory episodes {run_key} --json\n{result.stdout}")


@pytest.mark.parametrize(
    ("argv", "key"),
    [
        (["memory", "search", "nothing", JSON_FLAG], "facts"),
        (["memory", "procedures", JSON_FLAG], "procedures"),
        (["memory", "procedures", "--all", JSON_FLAG], "procedures"),
    ],
)
def test_an_empty_store_emits_an_empty_collection_not_a_null(
    clean_env: None, conn: sqlite3.Connection, argv: list[str], key: str
) -> None:
    result = split.invoke(app, argv)
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    payload = assert_canonical(result.stdout)
    assert payload == {key: []}


#: A well-formed session id no ``events`` row carries.
UNKNOWN_SESSION: Final = "run_00000000000000000000000000"


def held_session(conn: sqlite3.Connection) -> str:
    """A session that exists: one event published under it, and nothing else written."""
    bus = EventBus(conn=conn)
    bus.publish("run.started", {"provider": "fake"})
    return bus.run_id


def test_memory_episodes_of_a_session_with_no_episodes_is_an_empty_collection(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """Ticket 079: held and empty is ``{"episodes": []}`` and exit 0, the route's 200."""
    session_id = held_session(conn)
    result = split.invoke(app, ["memory", "episodes", session_id, JSON_FLAG])
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    assert assert_canonical(result.stdout) == {"episodes": []}


@pytest.mark.parametrize("flags", [[], [JSON_FLAG]], ids=["prose", "json"])
def test_memory_episodes_of_an_unknown_session_is_refused(
    clean_env: None,
    conn: sqlite3.Connection,
    flags: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ticket 079: an unheld id is absent, not empty, the way `eval show` treats an unknown run.

    Driven through ``main`` rather than ``CliRunner``, as every stderr assertion is (ticket 028).
    """
    monkeypatch.setattr(sys, "argv", ["harnessed", "memory", "episodes", UNKNOWN_SESSION, *flags])
    with pytest.raises(SystemExit) as exited:
        main()
    captured = capsys.readouterr()
    assert exited.value.code == 1
    assert captured.out == ""
    assert f"no session with id {UNKNOWN_SESSION!r}" in captured.err


def test_memory_without_the_flag_renders_as_before(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    fact = semantic.put(conn, "offline fact", source="test")
    procedure = procedural.put(conn, "Be brief.", source="test")
    procedural.activate(conn, procedure.proc_id, cand_id=None)
    episode = episodic.put(conn, held_session(conn), "loop", "prompt: a\nreply: b")

    searched = runner.invoke(app, ["memory", "search", "offline"])
    assert searched.output.startswith("1 fact(s) matching 'offline'")
    assert fact.mem_id in searched.output

    listed = runner.invoke(app, ["memory", "procedures"])
    assert listed.output.startswith("1 procedure(s), active")
    assert procedure.proc_id in listed.output

    read = runner.invoke(app, ["memory", "episodes", episode.run_id])
    assert read.output.startswith(f"1 episode(s) for run {episode.run_id}")
    assert episode.episode_id in read.output

    for result in (searched, listed, read):
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.output)


def test_the_memory_group_itself_takes_no_json_flag(
    clean_env: None, conn: sqlite3.Connection
) -> None:
    """``memory`` is a command group whose callback takes no options: ``memory --json`` is not a
    command, and the parity table must not be written as if it were."""
    result = split.invoke(app, ["memory", JSON_FLAG])
    assert result.exit_code == 2
    assert "No such option: --json" in result.stderr
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# 3. the tree walk: zero decision-producing commands without --json
# ---------------------------------------------------------------------------


def _leaves(command: click.Command, prefix: str = "") -> Iterator[tuple[str, click.Command]]:
    """Every runnable leaf under ``command``, named as it is typed (``memory search``)."""
    if isinstance(command, click.Group):
        for name, child in command.commands.items():
            yield from _leaves(child, f"{prefix}{name} ".strip() + " ")
    else:
        yield prefix.strip(), command


def _has_json_flag(command: click.Command) -> bool:
    return any(
        isinstance(param, click.Option) and JSON_FLAG in param.opts for param in command.params
    )


def test_every_decision_producing_command_has_json() -> None:
    leaves = dict(_leaves(typer.main.get_command(app)))
    assert leaves, "the walk found no commands at all"

    stale = set(NOT_DECISION_PRODUCING) - set(leaves)
    assert not stale, f"exempted commands that no longer exist: {sorted(stale)}"

    outgrown = [name for name in NOT_DECISION_PRODUCING if _has_json_flag(leaves[name])]
    assert not outgrown, f"exempt commands that now carry --json; retire the entry: {outgrown}"

    missing = [
        name
        for name, command in leaves.items()
        if name not in NOT_DECISION_PRODUCING and not _has_json_flag(command)
    ]
    lines = [
        f"{name:<20} {'--json' if _has_json_flag(command) else 'no --json':<10} "
        f"{NOT_DECISION_PRODUCING.get(name, '')}"
        for name, command in sorted(leaves.items())
    ]
    lines.append(f"decision-producing commands lacking --json: {len(missing)}")
    capture("command-tree.txt", "\n".join(lines) + "\n")
    assert len(missing) == 0, f"decision-producing commands lacking --json: {missing}"


# ---- Known defect (c): a redirected stdout under a cp1252 console (ticket 230, FR-159) ----------


def test_json_output_to_a_redirected_cp1252_stdout_is_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run --json`` raised ``UnicodeEncodeError`` on a Windows console redirected to a file.

    A cp1252 stream cannot hold the arrow, so before ``cli/render.py`` reconfigured it the print
    raised; now the bytes are UTF-8 and decode back to the object that was written.
    """
    import io

    from harnessed.cli import render

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    payload = {"answer": "a → b", "café": 1}
    render.json_object(payload)
    stream.flush()
    assert json.loads(raw.getvalue().decode("utf-8")) == payload
    assert stream.encoding == "utf-8"
