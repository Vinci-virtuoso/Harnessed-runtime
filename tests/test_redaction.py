"""FR-13: every store write and every event publish is redacted first.

Two kinds of assertion live here. The behavioural ones prove ``redact.scrub()`` replaces each
declared pattern with a stable token, deterministically, through arbitrarily nested payloads.
The structural one enumerates the write entry points and fails naming any that bypasses
``scrub()`` - including ``events/bus.py``, which ticket 009 builds, so the bus inherits a
failing assertion the moment it lands without the call.

Every sample below is assembled from fragments rather than written out whole: a literal that
looks like a credential is what the repository's own write guard exists to reject, and a test
for redaction is not an exception to that rule.
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessed import config
from harnessed.memory import db, episodic, procedural, redact, semantic

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "harnessed"

# Every write entry point FR-13 governs, as (module path, function name). A ledger write is a
# store write: ``loop/journal.py`` puts a whole transcript on disk, which is the payload most
# likely to be carrying a key the model was handed.
ENTRY_POINTS = (
    ("memory/semantic.py", "put"),
    ("memory/episodic.py", "put"),
    ("memory/procedural.py", "put"),
    ("events/bus.py", "publish"),
    ("loop/journal.py", "record"),
    ("improve/steps.py", "put"),
)

# The entry points that must already exist. Without this the enumeration above could pass
# vacuously by having nothing left to enumerate.
PRESENT_ENTRY_POINTS = ENTRY_POINTS[:3]

_BODY = "AAAABBBBCCCCDDDDEEEEFFFF"
_DASHES = "-" * 5

ANTHROPIC_SAMPLE = "sk-" + "ant-api03-" + _BODY
OPENAI_SAMPLE = "sk-" + "proj-" + _BODY
GROQ_SAMPLE = "gsk" + "_" + _BODY
BEARER_SAMPLE = "abc123def456ghi789"
AWS_SAMPLE = "AKIA" + "IOSFODNN7EXAMPLE"
PEM_SAMPLE = _DASHES + "BEGIN RSA PRIVATE " + "KEY" + _DASHES

# One sample per declared pattern: a payload fragment and the secret inside it.
SAMPLES = (
    (f"key={ANTHROPIC_SAMPLE} end", ANTHROPIC_SAMPLE),
    (f"key={OPENAI_SAMPLE} end", OPENAI_SAMPLE),
    (f"key={GROQ_SAMPLE} end", GROQ_SAMPLE),
    (f"Authorization: Bearer {BEARER_SAMPLE} end", BEARER_SAMPLE),
    (f"id={AWS_SAMPLE} end", AWS_SAMPLE),
    (f"{PEM_SAMPLE}\nMIIEowIBAAKCAQEA\n", PEM_SAMPLE),
)


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database file under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


def redacted_tokens(text: str) -> list[str]:
    """The kind of every ``[redacted:<kind>]`` token in ``text``, in order."""
    return [piece.split("]", 1)[0] for piece in text.split("[redacted:")[1:] if "]" in piece]


# ---- the declared patterns ---------------------------------------------------------------------


@pytest.mark.parametrize(("payload", "secret"), SAMPLES)
def test_each_declared_pattern_is_replaced_by_a_stable_token(
    clean_env: None, payload: str, secret: str
) -> None:
    scrubbed = redact.scrub(payload)
    assert secret not in scrubbed
    assert redacted_tokens(scrubbed), scrubbed


def test_each_pattern_gets_its_own_kind(clean_env: None) -> None:
    """One token per pattern, and no two patterns collapse into the same word."""
    kinds = [redacted_tokens(redact.scrub(payload))[0] for payload, _ in SAMPLES]
    assert len(set(kinds)) == len(SAMPLES), kinds


def test_a_payload_with_no_secret_is_returned_unchanged(clean_env: None) -> None:
    assert redact.scrub("the capital of France is Paris") == "the capital of France is Paris"


def test_a_set_secret_env_value_is_replaced_verbatim(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "not-a-recognisable-shape-9182")
    assert "GROQ_API_KEY" in config.SECRET_VARS
    scrubbed = redact.scrub("the value is not-a-recognisable-shape-9182 today")
    assert "not-a-recognisable-shape-9182" not in scrubbed
    assert redacted_tokens(scrubbed)


def test_an_unset_secret_env_var_redacts_nothing(clean_env: None) -> None:
    plain = "the value is not-a-recognisable-shape-9182 today"
    assert redact.scrub(plain) == plain


def test_scrubbing_is_deterministic_and_idempotent(clean_env: None) -> None:
    payload = " ".join(sample for sample, _ in SAMPLES)
    once = redact.scrub(payload)
    assert once == redact.scrub(payload)
    assert redact.scrub(once) == once


def test_nested_payloads_are_scrubbed_through_every_container(clean_env: None) -> None:
    payload: dict[str, object] = {
        "outer": [f"key={OPENAI_SAMPLE}", (GROQ_SAMPLE,)],
        ANTHROPIC_SAMPLE: {"inner": f"id={AWS_SAMPLE}", "deeper": [PEM_SAMPLE]},
        "as_set": {GROQ_SAMPLE},
        "as_frozenset": frozenset({OPENAI_SAMPLE}),
        "count": 3,
        "flag": True,
        "missing": None,
    }
    scrubbed = redact.scrub(payload)
    flat = repr(scrubbed)
    for _, secret in SAMPLES:
        if secret != BEARER_SAMPLE:
            assert secret not in flat
    assert scrubbed["count"] == 3
    assert scrubbed["flag"] is True
    assert scrubbed["missing"] is None
    assert isinstance(scrubbed["outer"], list)
    assert isinstance(scrubbed["as_set"], set)
    assert isinstance(scrubbed["as_frozenset"], frozenset)


# ---- the three stores ---------------------------------------------------------------------------


def test_a_semantic_write_stores_the_redacted_text(conn: sqlite3.Connection) -> None:
    fact = semantic.put(conn, f"the key is {OPENAI_SAMPLE}", "test")
    assert OPENAI_SAMPLE not in fact.text
    stored = semantic.get(conn, fact.mem_id)
    assert stored is not None
    assert OPENAI_SAMPLE not in stored.text
    row = conn.execute("SELECT text FROM mem_semantic_fts WHERE mem_id = ?", (fact.mem_id,))
    assert OPENAI_SAMPLE not in str(row.fetchone()[0])


def test_an_episodic_write_stores_the_redacted_text(conn: sqlite3.Connection) -> None:
    episode = episodic.put(conn, "run_1", "loop", f"id={AWS_SAMPLE}")
    assert AWS_SAMPLE not in episode.text
    stored = episodic.get(conn, episode.episode_id)
    assert stored is not None
    assert AWS_SAMPLE not in stored.text


def test_a_procedural_write_stores_the_redacted_text(conn: sqlite3.Connection) -> None:
    procedure = procedural.put(conn, f"use {GROQ_SAMPLE}", "test")
    assert GROQ_SAMPLE not in procedure.text
    stored = procedural.get(conn, procedure.proc_id)
    assert stored is not None
    assert GROQ_SAMPLE not in stored.text


def test_a_procedure_is_content_addressed_by_its_redacted_text(conn: sqlite3.Connection) -> None:
    """Hashing the raw text would store two rows for what reads back as one identical text."""
    first = procedural.put(conn, f"use {GROQ_SAMPLE}", "test")
    second = procedural.put(conn, "use " + "gsk" + "_ZZZZYYYYXXXXWWWWVVVVUUUU", "test")
    assert first.proc_id == second.proc_id
    assert len(procedural.all(conn)) == 1


# ---- the enumeration (FR-13's structural assertion) ---------------------------------------------


def routing(relative: str, function: str) -> str:
    """``scrub``, ``bypass``, or ``absent`` for one write entry point.

    ``absent`` is a sentinel rather than a skip so the claim reads identically before and after
    ticket 009 lands ``events/bus.py``: a file that does not exist cannot bypass anything, and
    one that exists must call ``redact.scrub()``.
    """
    path = PACKAGE_ROOT / relative
    if not path.exists():
        return "absent"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
    ]
    if not definitions:
        return "bypass"
    for definition in definitions:
        for node in ast.walk(definition):
            target = node.func if isinstance(node, ast.Call) else None
            if isinstance(target, ast.Attribute) and target.attr == "scrub":
                return "scrub"
    return "bypass"


def test_no_write_entry_point_bypasses_scrub() -> None:
    bypassing = [
        f"{relative}::{function}"
        for relative, function in ENTRY_POINTS
        if routing(relative, function) == "bypass"
    ]
    assert bypassing == []


def test_every_store_write_entry_point_exists_and_routes_through_scrub() -> None:
    assert {
        f"{relative}::{function}": routing(relative, function)
        for relative, function in PRESENT_ENTRY_POINTS
    } == {
        "memory/semantic.py::put": "scrub",
        "memory/episodic.py::put": "scrub",
        "memory/procedural.py::put": "scrub",
    }


# ---- the key that came from the file (ticket 039) -----------------------------------------------

#: A value with no credential shape whatsoever: nothing in the pattern table can match it, so
#: only ``config.secret_values()`` knowing the process's own credentials can remove it. That is
#: exactly the arm ticket 039 widens, so it is the arm these tests pin.
DOTENV_KEY = "not-a-recognisable-shape-from-a-file-2718"

#: The provider whose key var these tests write, and that variable. Quoted from the table.
KEYED_PROVIDER = "groq"
KEY_VAR = config.PROVIDER_KEY_VARS[KEYED_PROVIDER]


@pytest.fixture
def dotenv_only(clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A working directory whose only configuration is a `.env` holding a provider and its key.

    The chdir is what makes this honest on a machine that has its own `.env`: the file arm reads
    the working directory, so without it these tests would be reading a developer's real keys.
    """
    (tmp_path / config.ENV_FILENAME).write_text(
        f"HARNESSED_PROVIDER={KEYED_PROVIDER}\n{KEY_VAR}={DOTENV_KEY}\n", encoding="utf-8"
    )
    monkeypatch.delenv("HARNESSED_PROVIDER", raising=False)
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def test_a_key_read_only_from_the_file_is_a_secret_value(dotenv_only: Path) -> None:
    """The criterion the ticket exists for. A key that reaches an adapter but not the redactor
    would land unscrubbed in every store, which is strictly worse than needing an export."""
    assert DOTENV_KEY in config.secret_values()


def test_a_key_read_only_from_the_file_is_scrubbed_verbatim(dotenv_only: Path) -> None:
    scrubbed = redact.scrub(f"the header carried {DOTENV_KEY} today")
    assert DOTENV_KEY not in scrubbed
    assert redacted_tokens(scrubbed)


def test_both_the_exported_and_the_file_value_are_scrubbed(
    dotenv_only: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exported value wins for *use*; for *redaction* both are known credentials and both go.

    Scrubbing the value the process did not choose costs nothing and removes the case where an
    operator rotates a key, exports the new one, and the old one leaks out of a cached payload.
    """
    exported = "not-a-recognisable-shape-from-a-shell-3141"
    monkeypatch.setenv(str(KEY_VAR), exported)
    scrubbed = redact.scrub(f"{DOTENV_KEY} and {exported}")
    assert DOTENV_KEY not in scrubbed
    assert exported not in scrubbed


def test_a_line_in_the_file_that_is_not_a_declared_secret_is_not_redacted(
    dotenv_only: Path,
) -> None:
    """Only ``SECRET_VARS`` names are taken from the file. Redacting every value an operator
    happens to keep in `.env` would scrub ordinary words out of every payload."""
    (dotenv_only / config.ENV_FILENAME).write_text(
        f"HARNESSED_PROVIDER={KEYED_PROVIDER}\nHARNESSED_MODEL=a-model-name-8628\n",
        encoding="utf-8",
    )
    assert redact.scrub("the model is a-model-name-8628") == "the model is a-model-name-8628"


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda path: path.mkdir(), id="unreadable"),
        pytest.param(lambda path: path.write_bytes(b"\xff\xfe\x00 not utf-8"), id="undecodable"),
        pytest.param(
            lambda path: path.write_text("not an assignment at all\n=\n", encoding="utf-8"),
            id="malformed",
        ),
    ],
)
def test_a_broken_config_file_can_never_turn_a_redaction_into_an_error(
    make: object, clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guarantee the old `secret_values()` docstring made, kept across the widening: failing
    to scrub is the worse failure, so this arm returns what it can and never raises."""
    monkeypatch.chdir(tmp_path)
    make(tmp_path / config.ENV_FILENAME)  # type: ignore[operator]
    assert config.secret_values() == ()
    assert redact.scrub("nothing to see") == "nothing to see"


# ---- the same key, out the far end of a real run (ticket 039) -----------------------------------


def stub_adapter(secret: str) -> object:
    """A provider that proves the credential reached it and hands it straight back.

    Both halves matter. ``require_api_key`` is asked so the test fails if the key never got out
    of the file, and the value is echoed into the completion the way a live endpoint echoes a
    header back inside a 401 body - which is the shape that puts a credential into the journal
    and the event stream in the first place.
    """

    class Echoes:
        def __init__(self, settings: config.Settings) -> None:
            assert settings.require_api_key(KEYED_PROVIDER) == secret

        def complete(self, request: object) -> object:
            from harnessed.providers.types import ChatResponse

            return ChatResponse(
                response_id="resp_stub",
                model="stub-model",
                content=f"the endpoint replied with {secret} in the body",
            )

    return Echoes


def test_a_key_supplied_only_by_the_file_never_lands_in_the_events_or_turns_tables(
    dotenv_only: Path, tmp_harnessed_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this ticket is most likely to ship by accident, asserted at the tables.

    A naive widening would load the file's keys into ``Settings._keys`` alone, hand one to a live
    adapter, and leave it in ``events`` and ``turns`` unscrubbed - a credential leak wearing a
    convenience feature's clothes. The key here is a shape no pattern in the table can match, so
    ``secret_values()`` reading the same file is the only thing that can keep it out.

    The session is opened the way ``harnessed run`` opens it - nothing about the configuration is
    stubbed, only the endpoint - and a :class:`Journal` is bound so the ``turns`` half of the
    claim is written rather than vacuously empty: ``harnessed run`` itself journals nothing.
    """
    from harnessed.cli.run_cmd import open_session
    from harnessed.loop.engine import run_loop
    from harnessed.loop.journal import Journal
    from harnessed.providers import registry
    from harnessed.providers.translate import tool_call_payload
    from harnessed.providers.types import ChatRequest, Message

    monkeypatch.setitem(registry.OFFLINE_FACTORIES, KEYED_PROVIDER, stub_adapter(DOTENV_KEY))
    session = open_session()
    journal = Journal(session.conn, "run_039", serialize_call=tool_call_payload, provider="fake")
    request = ChatRequest(
        model=session.model,
        system=session.system(),
        messages=(Message(role="user", content="ping"),),
    )
    run_loop(
        session.provider,
        session.tools,
        request,
        limit=session.settings.max_iterations,
        bus=session.bus,
        journal=journal,
    )

    stored = "\n".join(
        str(row[0])
        for query in ("SELECT payload FROM events", "SELECT request || response FROM turns")
        for row in session.conn.execute(query).fetchall()
    )
    session.conn.close()

    assert stored, "the run wrote no events and no turns, so this asserts nothing"
    assert DOTENV_KEY not in stored
    assert redact.token("env_secret") in stored, "the key never reached a store at all"

    directory = REPO_ROOT / "evidence" / "039"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "redaction-covers-dotenv-key.txt").write_text(stored, encoding="utf-8")
