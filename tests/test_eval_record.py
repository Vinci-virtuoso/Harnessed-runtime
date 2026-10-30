"""Ticket 235 (FR-136, decision 83): a work suite recorded from a real session, a verdict per case.

Sessions are built the way ``test_session_messages`` builds them (a FakeProvider session behind a
stub decider), and every suite and fixture tree is written under ``tmp_path``: nothing here writes
the committed ``corpus/suites/`` or the committed corpus (MUST rule 8).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from harnessed import config, shipped
from harnessed.cli.app import app
from harnessed.cli.run_cmd import continue_session, solve_with_memory
from harnessed.errors import SuiteLoadError
from harnessed.evals import record
from harnessed.evals import suite as suites
from harnessed.memory import db
from harnessed.providers.fake import DEFAULT_CORPUS_ROOT as DEFAULT_CORPUS
from harnessed.providers.fake import FakeProvider
from test_session_messages import _groq_session

pytestmark = pytest.mark.usefixtures("frozen_clock", "seeded_ids")

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence" / "235"


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def roots(
    tmp_path: Path, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """The home's suites and fixtures roots, which is where a recording lands by default.

    The shipped corpus is stood in by an empty directory so ``work-email`` is free to record.
    """
    empty = tmp_path / "shipped-suites"
    empty.mkdir()
    monkeypatch.setattr(shipped, "suites_dir", lambda: empty)
    return tmp_harnessed_home / "suites", tmp_harnessed_home / "fixtures" / "fake"


def _seeded(conn: sqlite3.Connection, *texts: str) -> str:
    session = _groq_session(conn)
    first = solve_with_memory(session, texts[0])
    for text in texts[1:]:
        continue_session(session, first.run_id, text)
    conn.commit()
    return first.run_id


def _cases(suites_root: Path, name: str = "work-email") -> list[dict[str, object]]:
    return yaml.safe_load((suites_root / f"{name}.yaml").read_text(encoding="utf-8"))["cases"]


def test_a_session_is_recorded_as_a_work_suite_with_a_pending_case_per_message(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    suites_root, fixtures_root = roots
    run_id = _seeded(conn, "ping", "second turn")

    done = record.record(conn, "work-email", run_id)

    loaded = suites.load_named_suite("work-email", suites_root)
    assert loaded.kind == suites.KIND_WORK
    assert [case.id for case in loaded.cases] == list(done.added)
    first, second = loaded.cases
    assert first.id == "work-email-001-ping"
    assert second.id == "work-email-002-second-turn"
    assert first.prompt == "ping"
    assert first.handler == "groq_chat"
    assert first.reference != ""
    assert {case.verdict for case in loaded.cases} == {"pending"}
    for case in loaded.cases:
        scope = fixtures_root / case.id
        assert (scope / "_index.yaml").is_file()
        assert (scope / "base.json").is_file()
    shipped = json.loads((fixtures_root / first.id / "base.json").read_text(encoding="utf-8"))
    assert shipped["content"] == first.reference


def test_seq_records_only_the_named_messages(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    run_id = _seeded(conn, "ping", "second turn")
    from harnessed.loop import messages

    second = messages.for_session(conn, run_id)[1]

    record.record(conn, "work-email", run_id, seqs=[second.message_seq])

    assert [case["input"] for case in _cases(roots[0])] == ["second turn"]
    with pytest.raises(SuiteLoadError, match="seq 999"):
        record.record(conn, "work-email", run_id, seqs=[999])


def test_re_recording_appends_new_cases_and_rewrites_no_verdict_or_fixture(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    suites_root, fixtures_root = roots
    session = _groq_session(conn)
    first = solve_with_memory(session, "ping")
    conn.commit()
    record.record(conn, "work-email", first.run_id)
    record.set_verdict("work-email", "work-email-001-ping", verdict="accept")
    fixture = fixtures_root / "work-email-001-ping" / "base.json"
    fixture.write_text(fixture.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    before = (suites_root / "work-email.yaml").read_text(encoding="utf-8")
    fixture_before = fixture.read_bytes()

    continue_session(session, first.run_id, "second turn")
    conn.commit()
    again = record.record(conn, "work-email", first.run_id)

    assert again.added == ("work-email-002-second-turn",)
    assert again.kept == ("work-email-001-ping",)
    cases = _cases(suites_root)
    assert [case["verdict"] for case in cases] == ["accept", "pending"]
    assert fixture.read_bytes() == fixture_before
    after = (suites_root / "work-email.yaml").read_text(encoding="utf-8")
    assert after.startswith(
        before.rstrip("\n").rsplit("\n  - id:", 1)[0].rstrip()
    ), "the recorded part of the file is untouched"
    assert record.record(conn, "work-email", first.run_id).added == ()


def test_a_secret_in_a_message_reaches_neither_the_suite_nor_the_fixture(
    conn: sqlite3.Connection, roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    suites_root, fixtures_root = roots
    var = sorted(config.SECRET_VARS)[0]
    secret = "zq-not-a-real-secret-9f8e7d6c5b4a"
    monkeypatch.setenv(var, secret)
    run_id = _seeded(conn, "ping")
    # The ledger scrubs on write; the recorder must scrub on read too, so plant the raw value.
    conn.execute("UPDATE decisions SET task = ? WHERE run_id = ?", (f"use {secret} now", run_id))
    conn.execute("UPDATE turns SET response = ? WHERE run_id = ?", (f"key {secret}", run_id))
    conn.commit()

    record.record(conn, "work-email", run_id)

    written = [suites_root / "work-email.yaml", *fixtures_root.rglob("*")]
    for path in written:
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8"), path
    (case,) = _cases(suites_root)
    assert "[redacted:env_secret]" in str(case["input"])
    assert "[redacted:env_secret]" in str(case["reference"])
    assert secret not in str(case["id"])


def test_verdict_sets_accept_reject_and_repeatable_traits(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    run_id = _seeded(conn, "ping", "second turn")
    record.record(conn, "work-email", run_id)
    runner = CliRunner()

    ok = runner.invoke(app, ["eval", "verdict", "work-email", "work-email-001-ping", "accept"])
    assert ok.exit_code == 0, ok.stdout + ok.output
    traited = runner.invoke(
        app,
        [
            "eval",
            "verdict",
            "work-email",
            "work-email-002-second-turn",
            "--trait",
            "polite",
            "--trait",
            "short",
        ],
    )
    assert traited.exit_code == 0, traited.stdout + traited.output

    loaded = suites.load_named_suite("work-email", roots[0])
    assert [case.verdict for case in loaded.cases] == ["accept", "traits"]
    assert loaded.cases[1].traits == ("polite", "short")
    runner.invoke(app, ["eval", "verdict", "work-email", "work-email-002-second-turn", "reject"])
    assert suites.load_named_suite("work-email", roots[0]).cases[1].traits == ()


def test_verdict_on_an_unknown_suite_or_case_exits_1_naming_it(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    record.record(conn, "work-email", _seeded(conn, "ping"))
    runner = CliRunner()

    no_suite = runner.invoke(app, ["eval", "verdict", "nope-suite", "x", "accept"])
    no_case = runner.invoke(app, ["eval", "verdict", "work-email", "nope-case", "accept"])

    assert no_suite.exit_code == 1 and "nope-suite" in no_suite.output
    assert no_case.exit_code == 1 and "nope-case" in no_case.output


def test_pending_lists_each_case_in_a_block_and_as_json_and_says_so_when_none(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    run_id = _seeded(conn, "ping", "second turn")
    record.record(conn, "work-email", run_id)
    runner = CliRunner()
    record.set_verdict("work-email", "work-email-001-ping", verdict="reject")

    text = runner.invoke(app, ["eval", "verdict", "work-email", "--pending"])
    as_json = runner.invoke(app, ["eval", "verdict", "work-email", "--pending", "--json"])

    assert text.exit_code == 0, text.output
    assert "work-email-002-second-turn" in text.stdout
    assert "input: second turn" in text.stdout and "reference:" in text.stdout
    assert "work-email-001-ping" not in text.stdout
    payload = json.loads(as_json.stdout)
    assert [row["case_id"] for row in payload["pending"]] == ["work-email-002-second-turn"]
    assert set(payload["pending"][0]) == {"case_id", "input", "reference"}

    record.set_verdict("work-email", "work-email-002-second-turn", verdict="accept")
    none = runner.invoke(app, ["eval", "verdict", "work-email", "--pending"])
    assert none.exit_code == 0 and "no case awaits a verdict" in none.stdout

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "pending.json").write_text(as_json.stdout, encoding="utf-8")
    (EVIDENCE / "pending.txt").write_text(text.stdout, encoding="utf-8")
    (EVIDENCE / "suite.yaml").write_text(
        (roots[0] / "work-email.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (EVIDENCE / "fixtures.txt").write_text(
        "\n".join(sorted(str(p.relative_to(roots[1])) for p in roots[1].rglob("*") if p.is_file()))
        + "\n",
        encoding="utf-8",
    )


def test_the_recorded_fixture_replays_through_the_fake_provider(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    record.record(conn, "work-email", _seeded(conn, "ping"))
    provider = FakeProvider(config.load(provider="fake"), roots[1])
    assert "work-email-001-ping" in provider._scopes  # type: ignore[attr-defined]


# ---- ticket 293 (FR-172, decisions 77, 88, 102): the operator's words only, and --remove ------


def _as_task(conn: sqlite3.Connection, run_id: str, kinds: dict[int, str]) -> None:
    for seq, kind in kinds.items():
        conn.execute(
            "INSERT INTO task_messages (run_id, message_seq, task_id, kind) "
            "VALUES (?, ?, 'task_x', ?)",
            (run_id, seq, kind),
        )
    conn.commit()


def _long_session(conn: sqlite3.Connection, tmp_path: Path, *texts: str) -> str:
    """Like ``_seeded`` over a copy of the corpus with turns 3 and 4 of every scope added."""
    corpus = tmp_path / "corpus"
    shutil.copytree(DEFAULT_CORPUS, corpus)
    for second in corpus.glob("*/base.2.json"):
        for extra in (3, 4):
            shutil.copy(second, second.with_name(f"base.{extra}.json"))
    session = _groq_session(conn)
    session = replace(session, provider=FakeProvider(session.settings, root=corpus))
    first = solve_with_memory(session, texts[0])
    for text in texts[1:]:
        continue_session(session, first.run_id, text)
    conn.commit()
    return first.run_id


def test_a_task_session_records_only_the_served_operator_message(
    conn: sqlite3.Connection, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    from harnessed.loop import messages

    run_id = _long_session(conn, tmp_path, "ping", "second turn", "second turn", "ping")
    served = messages.for_session(conn, run_id)
    _as_task(
        conn,
        run_id,
        {
            served[0].message_seq: "first",
            served[1].message_seq: "continuation",
            served[2].message_seq: "continuation",
        },
    )

    done = record.record(conn, "work-email", run_id)

    cases = _cases(roots[0])
    assert [case["input"] for case in cases] == ["ping"]
    assert len(done.added) == 1
    assert [case["seq"] for case in cases] == [served[3].message_seq]
    assert sorted(path.name for path in roots[1].iterdir()) == list(done.added)


def test_remove_deletes_the_suite_and_its_fixtures_and_nothing_else(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    suites_root, fixtures_root = roots
    run_id = _seeded(conn, "ping", "second turn")
    done = record.record(conn, "work-mine", run_id)
    other = fixtures_root / "someone-else"
    other.mkdir()
    (other / "base.json").write_text("{}\n", encoding="utf-8", newline="\n")
    sibling = suites_root / "work-other.yaml"
    sibling.write_text("suite: work-other\n", encoding="utf-8", newline="\n")

    gone = record.remove("work-mine")

    assert not (suites_root / "work-mine.yaml").exists()
    assert [path.name for path in fixtures_root.iterdir()] == ["someone-else"]
    assert set(gone.fixtures) == set(done.added)
    assert (other / "base.json").read_bytes() == b"{}\n"
    assert sibling.read_text(encoding="utf-8") == "suite: work-other\n"


@pytest.mark.parametrize("shipped", ["smoke", "holdout", "memory", "work-email"])
def test_remove_refuses_a_shipped_suite_naming_it(shipped: str, roots: tuple[Path, Path]) -> None:
    roots[0].mkdir()
    (roots[0] / f"{shipped}.yaml").write_text("suite: x\n", encoding="utf-8")
    with pytest.raises(SuiteLoadError, match=shipped):
        record.remove(shipped)
    assert (roots[0] / f"{shipped}.yaml").exists()


def test_the_remove_flag_is_listed_and_removes_through_the_cli(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    runner = CliRunner()
    helped = runner.invoke(app, ["eval", "record", "--help"])
    assert "--remove" in helped.output
    run_id = _seeded(conn, "ping")
    record.record(conn, "work-mine", run_id)

    result = runner.invoke(app, ["eval", "record", "work-mine", "--remove"])

    assert result.exit_code == 0, result.output
    assert "work-mine" in result.output
    assert not (roots[0] / "work-mine.yaml").exists()
    refused = runner.invoke(app, ["eval", "record", "smoke", "--remove"])
    assert refused.exit_code == 1
    assert "smoke" in refused.output


# ---- ticket 313: the home, --to, and never shadowing a shipped suite -----------------------


def test_a_recording_lands_in_the_home_by_default(
    conn: sqlite3.Connection, roots: tuple[Path, Path], tmp_harnessed_home: Path
) -> None:
    run_id = _seeded(conn, "ping")

    done = record.record(conn, "work-email", run_id)

    assert done.path == tmp_harnessed_home / "suites" / "work-email.yaml"
    assert done.path.is_file()
    assert (tmp_harnessed_home / "fixtures" / "fake" / done.added[0] / "_index.yaml").is_file()


def test_to_writes_a_corpus_directory_and_a_verdict_and_remove_follow_it(
    conn: sqlite3.Connection, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    run_id = _seeded(conn, "ping")
    corpus = tmp_path / "corpus"
    runner = CliRunner()

    recorded = runner.invoke(
        app, ["eval", "record", "work-mine", "--session", run_id, "--to", str(corpus)]
    )
    assert recorded.exit_code == 0, recorded.output
    case_id = "work-mine-001-ping"
    assert (corpus / "suites" / "work-mine.yaml").is_file()
    assert (corpus / "wire" / "fake" / case_id / "base.json").is_file()
    assert not roots[0].exists(), "nothing reached the home"

    judged = runner.invoke(
        app, ["eval", "verdict", "work-mine", case_id, "accept", "--to", str(corpus)]
    )
    assert judged.exit_code == 0, judged.output
    loaded = suites.load_named_suite("work-mine", corpus / "suites")
    assert loaded.cases[0].verdict == "accept"

    removed = runner.invoke(app, ["eval", "record", "work-mine", "--remove", "--to", str(corpus)])
    assert removed.exit_code == 0, removed.output
    assert not (corpus / "suites" / "work-mine.yaml").exists()
    assert list((corpus / "wire" / "fake").iterdir()) == [], "0 fixture directories remain"


def test_to_refuses_a_directory_it_cannot_write_naming_it(
    conn: sqlite3.Connection, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    run_id = _seeded(conn, "ping")
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")

    result = CliRunner().invoke(
        app, ["eval", "record", "work-mine", "--session", run_id, "--to", str(blocker)]
    )

    assert result.exit_code == 1
    assert str(blocker) in result.output


def test_a_recording_never_shadows_a_shipped_suite(
    conn: sqlite3.Connection,
    roots: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = tmp_path / "corpus"
    run_id = _seeded(conn, "ping", "second turn")
    record.record(conn, "work-email", run_id, seqs=[1], suites_root=corpus / "suites")
    monkeypatch.setattr(shipped, "suites_dir", lambda: corpus / "suites")

    with pytest.raises(SuiteLoadError, match="would shadow a shipped one"):
        record.record(conn, "work-email", run_id)
    # The same name in the home and the shipped corpus is refused at load, too.
    roots[0].mkdir(parents=True, exist_ok=True)
    shutil.copy(corpus / "suites" / "work-email.yaml", roots[0] / "work-email.yaml")
    with pytest.raises(SuiteLoadError, match="the recorded suite work-email would shadow"):
        suites.load_named_suite("work-email")
    # Naming the corpus is how a contributor lands a case in it.
    done = record.record(conn, "work-email", run_id, suites_root=corpus / "suites")
    assert len(done.added) == 1


def test_a_verdict_on_a_shipped_suite_without_to_is_refused_not_written_into_the_package(
    conn: sqlite3.Connection,
    roots: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg = tmp_path / "pkg"
    done = record.record(conn, "work-email", _seeded(conn, "ping"), suites_root=pkg)
    monkeypatch.setattr(shipped, "suites_dir", lambda: pkg)
    before = done.path.read_bytes()

    with pytest.raises(SuiteLoadError, match="ships with harnessed"):
        record.set_verdict("work-email", done.added[0], verdict="accept")
    assert done.path.read_bytes() == before


def test_the_listing_includes_a_home_suite(
    conn: sqlite3.Connection, roots: tuple[Path, Path]
) -> None:
    record.record(conn, "work-mine", _seeded(conn, "ping"))

    assert [row.suite for row in suites.listing(conn)] == ["work-mine"]
