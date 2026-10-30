"""FR-176, decision 88: the shipped corpus re-hashes to its manifest and ``shipped`` finds it.

``MANIFEST.sha256`` was written from the old paths before the ``git mv``, so a file that matches
here is byte-for-byte the file the proof read before the move (THE CRUX). Newlines are normalized
(decision 9's rule) so a Windows and a Linux checkout agree.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from harnessed import shipped
from harnessed.errors import ConfigError, CorpusMissing

MANIFEST = "MANIFEST.sha256"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _entries(root: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in (root / MANIFEST).read_text(encoding="utf-8").splitlines():
        digest, _, relative = line.partition("  ")
        entries[relative] = digest
    return entries


def _files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST and "__pycache__" not in path.parts
    }


def test_every_corpus_file_rehashes_to_its_manifest_line() -> None:
    root = shipped.corpus_root()
    entries = _entries(root)
    assert entries
    assert {rel: _digest(root / rel) for rel in entries} == entries


def test_every_manifest_line_names_a_present_file_and_every_file_has_a_line() -> None:
    root = shipped.corpus_root()
    assert set(_entries(root)) == _files(root)


def test_the_named_helpers_are_the_only_spellings_of_the_sub_paths() -> None:
    root = shipped.corpus_root()
    assert shipped.suites_dir() == root / "suites"
    assert shipped.routing_tasks() == root / "routing" / "tasks.yaml"
    assert shipped.fake_wire() == root / "wire" / "fake"
    assert shipped.jev_wire() == root / "wire" / "jev"
    assert shipped.walkthroughs_dir() == root / "walkthroughs"
    assert shipped.suites_dir().is_dir()
    assert shipped.routing_tasks().is_file()
    assert shipped.fake_wire().is_dir()
    assert shipped.jev_wire().is_dir()


def test_the_working_directory_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = shipped.corpus_root()
    monkeypatch.chdir(tmp_path)
    assert shipped.corpus_root() == expected


def test_a_missing_directory_raises_corpus_missing_naming_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "corpus"
    monkeypatch.setattr(shipped, "_package_corpus", lambda: missing)
    with pytest.raises(CorpusMissing, match="corpus") as caught:
        shipped.corpus_root()
    assert str(missing) in str(caught.value)
    assert isinstance(caught.value, ConfigError)


def test_a_missing_manifest_raises_corpus_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "corpus").mkdir()
    monkeypatch.setattr(shipped, "_package_corpus", lambda: tmp_path / "corpus")
    with pytest.raises(CorpusMissing, match=MANIFEST):
        shipped.corpus_root()
