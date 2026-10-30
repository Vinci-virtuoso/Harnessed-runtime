"""The publication audit (ticket 334, FR-185; decision 107, MUST rule 45).

Every tracked file is read as the public repository would publish it. A user path with a name, an
address nobody published, a key shape, a session link or the private mirror's name fails the run,
and so does a tracked path the operator keeps unpublished. Seeded samples prove each check fires;
the samples are assembled from parts so this file never matches its own patterns.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from harnessed.memory import redact

REPO_ROOT = Path(__file__).resolve().parents[1]

# Operator ruling Q3: kept as files and in the private mirror, never in the index.
UNPUBLISHED = (
    ".spec/operator-inputs/",
    ".spec/afk-runs/",
    ".spec/loop-state.md",
    ".spec/launch/",
    ".lavish/",
    ".deepeval/",
    "evidence/",
    ".harnessed/",
    ".env",
    "agents.toml",
)

# The one address the operator named at the gate (ticket 334, 2026-10-13).
PUBLISHED_CONTACTS = frozenset({"5eun3isiyktv@gmail.com"})

# Exact values that match a key shape and are not keys: the suite's fake keys, and the tail of a
# `task-...` identifier, which the unanchored `sk-` pattern reads as one.
NOT_KEYS = frozenset(
    "sk" + tail
    for tail in (
        "-ant-abcdefgh12345678",
        "-not-a-real-key",
        "-SECRET-VALUE",
        "-ticket-213-never-in-a-child",
        "-finished",
        "-pending-permission",
        "-run-due-command",
        "-run-command",
        "-pause-line",
        "-show-command",
        "-add-list",
        "-authored",
        "-truncation",
    )
)

USER_PATHS = (
    re.compile(r"""[A-Za-z]:\\Users\\[^\\\s.<>`'"]+"""),
    re.compile(r"/home/[a-z_][a-z0-9_-]*/"),
    re.compile(r"/Users/[A-Za-z][^/\s]*/"),
)
SESSION_LINK = re.compile(r"claude\.ai/code/session_[A-Za-z0-9]{6,}")
MIRROR_NAMES = ("Harnessed" + "-mirror", "Harnessed" + "-backup")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_DOMAIN = re.compile(r"(?:^|\.)example\.[a-z]+$|\.invalid$")


def _published_address(address: str) -> bool:
    local, _, domain = address.rpartition("@")
    return (
        address in PUBLISHED_CONTACTS
        or local.startswith("noreply")
        or ALLOWED_DOMAIN.search(domain.lower()) is not None
    )


def findings(text: str) -> list[tuple[str, str]]:
    """Every match in ``text`` the public repository must not carry, as ``(check, match)``."""
    found = [("user path", m.group()) for p in USER_PATHS for m in p.finditer(text)]
    found += [("session link", m.group()) for m in SESSION_LINK.finditer(text)]
    found += [("mirror name", name) for name in MIRROR_NAMES if name in text]
    found += [
        ("email", m.group()) for m in EMAIL.finditer(text) if not _published_address(m.group())
    ]
    for kind, pattern in redact._PATTERNS:
        found += [(kind, m.group()) for m in pattern.finditer(text) if m.group() not in NOT_KEYS]
    return found


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def _unpublished(path: str) -> bool:
    return any(
        path == u.rstrip("/") or (u.endswith("/") and path.startswith(u)) for u in UNPUBLISHED
    )


def _text(path: str) -> str | None:
    try:
        return (REPO_ROOT / path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return None


def test_no_unpublished_path_is_tracked() -> None:
    assert [p for p in _tracked() if _unpublished(p)] == []


#: Generated third-party licence text (ticket 336) reproduces authors' copyright lines verbatim,
#: emails included, as the licences require. Only the email check is lifted, and only here.
THIRD_PARTY_TEXT = frozenset({"THIRD_PARTY_NOTICES.md"})


def publishable_findings(path: str, text: str) -> list[tuple[str, str]]:
    found = findings(text)
    if path in THIRD_PARTY_TEXT:
        found = [(check, match) for check, match in found if check != "email"]
    return found


def test_every_tracked_file_is_publishable() -> None:
    offenders = {}
    for path in _tracked():
        text = _text(path)
        if text is not None and (found := publishable_findings(path, text)):
            offenders[path] = found
    assert offenders == {}


def test_the_notices_exemption_lifts_only_the_email_check_and_only_there() -> None:
    address = "dana" + "@" + "gmail.com"
    key = "sk" + "-ant-" + "Q7rT9vW2xY4zA6bC"
    assert publishable_findings("THIRD_PARTY_NOTICES.md", f"(c) Dana <{address}>") == []
    assert publishable_findings("THIRD_PARTY_NOTICES.md", key) != []
    assert publishable_findings("README.md", f"(c) Dana <{address}>") != []


def test_gitignore_holds_a_line_for_every_unpublished_path() -> None:
    lines = set((REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert [u for u in UNPUBLISHED if u not in lines] == []


SEEDED = {
    "windows user path": "C:" + "\\Users\\" + "dana\\proj",
    "posix home": "/home/" + "dana/proj",
    "mac home": "/Users/" + "Dana/proj",
    "session link": "https://claude.ai/code/" + "session_01AbCdEfGh",
    "mirror name": "cloned from " + MIRROR_NAMES[0] + ".git",
    "email": "write to " + "dana" + "@" + "gmail.com",
    "anthropic key": "key=" + "sk" + "-ant-" + "Q7rT9vW2xY4zA6bC",
    "openai key": "key=" + "sk" + "-" + "Q7rT9vW2xY4zA6bC",
    "groq key": "key=" + "gsk" + "_" + "Q7rT9vW2xY4zA6bC",
    "bearer token": "Authorization: " + "Bearer " + "Q7rT9vW2xY4zA6bC",
    "aws key id": "AKIA" + "Q7RT9VW2XY4ZA6BC",
    "private key": "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5,
}

ADMITTED = {
    "elided windows path": "C:" + "\\Users\\" + "...\\proj",
    "quoted prefix": "`C:" + "\\Users\\`, `/home/`",
    "session prefix only": "a `claude.ai/code/" + "session_` URL",
    "published contact": "mail " + next(iter(PUBLISHED_CONTACTS)),
    "noreply": "noreply" + "@" + "github.com",
    "example domain": "help" + "@" + "acme.example.com",
    "invalid domain": "stub" + "@" + "example.invalid",
    "fake key": "key=" + "sk" + "-not-a-real-key",
    "task identifier": "task" + "-finished",
}


@pytest.mark.parametrize("sample", list(SEEDED.values()), ids=list(SEEDED))
def test_a_seeded_file_fails_the_audit(tmp_path: Path, sample: str) -> None:
    seeded = tmp_path / "seeded.md"
    seeded.write_text(f"# notes\n\n{sample}\n", encoding="utf-8")
    assert findings(seeded.read_text(encoding="utf-8")) != []


@pytest.mark.parametrize("sample", list(ADMITTED.values()), ids=list(ADMITTED))
def test_what_the_audit_admits_passes(sample: str) -> None:
    assert findings(sample) == []


def test_a_seeded_unpublished_path_is_caught() -> None:
    assert _unpublished(".spec/loop-state.md")
    assert _unpublished(".spec/operator-inputs/307/record.json")
    assert _unpublished("agents.toml")
    assert not _unpublished("agents.toml.example")
    assert not _unpublished(".env.example")


def _ledger_fixtures() -> list[str]:
    return [
        p
        for p in _tracked()
        if p.endswith((".json", ".jsonl")) and ("fixtures/" in p or "/wire/" in p)
    ]


def test_every_ledger_shaped_fixture_is_a_fixed_point_of_scrub() -> None:
    fixtures = _ledger_fixtures()
    assert fixtures
    moved = []
    for path in fixtures:
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        rows = (
            [json.loads(line) for line in text.splitlines() if line.strip()]
            if path.endswith(".jsonl")
            else [json.loads(text)]
        )
        if any(redact.scrub(row) != row for row in rows):
            moved.append(path)
    assert moved == []


def test_the_published_contacts_are_the_operators_ruling() -> None:
    assert {"5eun3isiyktv@gmail.com"} == PUBLISHED_CONTACTS
