"""The four pillar walkthroughs run from the installed wheel and cannot rot (ticket 329, FR-183).

Each ``harnessed/corpus/walkthroughs/*.md`` holds fenced ``console`` blocks: ``$ harnessed ...``
command lines followed by the output they print. This test runs every command through the installed
interpreter (decision 106) in a temporary home and cwd, and matches the output: figures exactly, the
placeholders ``<name>`` by pattern. A placeholder named in ``BOUND`` must repeat the value it first
matched, and a later command that names it runs with that value. A lone ``...`` line stops the
comparison of the rest of the output.

Per-OS convention (decision 106): a fence written ``console linux|macos|windows`` is run only on
that OS; a bare ``console`` fence is run everywhere.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from harnessed import shipped
from support.wheel import installed_wheel, package_dir, run_installed, scrubbed_env

__all__ = ["installed_wheel"]  # re-exported so pytest finds the session fixture in this module

pytestmark = pytest.mark.slow

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "evidence" / "329"
PILLARS = ("loops", "evals", "memory", "harness")
OS_NAMES = {"linux": "linux", "darwin": "macos", "win32": "windows"}
THIS_OS = OS_NAMES.get(sys.platform, sys.platform)
BOUND = frozenset({"cand_id", "run_id"})
#: Commands that take no ``--provider`` option; they read ``HARNESSED_PROVIDER`` instead.
NO_PROVIDER_OPTION = frozenset({"doctor", "schedule", "agents", "runs", "memory"})

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FENCE = re.compile(r"^```console(?:[ \t]+(\w+))?[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
_PLACEHOLDER = re.compile(r"<(\w+)>")
_SOURCE = re.compile(r"`(harnessed/[\w./-]+)`")


@dataclass(frozen=True)
class Step:
    argv: list[str]
    expected: list[str]
    line: str


def _blocks(text: str) -> list[tuple[str | None, list[Step]]]:
    """Every console block as ``(os marker, steps)``."""
    blocks = []
    for match in _FENCE.finditer(text):
        steps: list[Step] = []
        for raw in match.group(2).splitlines():
            if raw.startswith("$ "):
                words = shlex.split(raw[2:])
                assert (
                    words[0] == "harnessed"
                ), f"a walkthrough command starts with harnessed: {raw}"
                steps.append(Step(words[1:], [], raw))
            else:
                assert steps, f"output before any command: {raw!r}"
                steps[-1].expected.append(raw)
        blocks.append((match.group(1), steps))
    return blocks


def _clean(output: str) -> list[str]:
    return _ANSI.sub("", output).replace("\r\n", "\n").rstrip("\n").split("\n")


def _pattern(expected: str, seen: dict[str, str]) -> re.Pattern[str]:
    parts: list[str] = []
    named: set[str] = set()
    position = 0
    for found in _PLACEHOLDER.finditer(expected):
        parts.append(re.escape(expected[position : found.start()]))
        name = found.group(1)
        if name in seen:
            parts.append(re.escape(seen[name]))
        elif name in named:
            parts.append(f"(?P={name})")
        else:
            if name in BOUND:
                parts.append(f"(?P<{name}>.+?)")
                named.add(name)
            else:
                parts.append(".*?")
        position = found.end()
    parts.append(re.escape(expected[position:]))
    return re.compile("".join(parts))


def _compare(step: Step, actual: list[str], seen: dict[str, str]) -> None:
    expected = step.expected
    elided = bool(expected) and expected[-1] == "..."
    if elided:
        expected = expected[:-1]
    ok = len(actual) >= len(expected) if elided else len(actual) == len(expected)
    mismatches = [] if ok else [f"expected {len(expected)} lines, got {len(actual)}"]
    for number, (want, got) in enumerate(zip(expected, actual, strict=False), start=1):
        matched = _pattern(want, seen).fullmatch(got)
        if matched is None:
            mismatches.append(f"line {number}:\n  want {want}\n  got  {got}")
        else:
            seen.update({k: v for k, v in matched.groupdict().items() if k in BOUND})
    assert not mismatches, (
        f"{step.line}\n" + "\n".join(mismatches) + "\n--- actual ---\n" + "\n".join(actual)
    )


def _run_walkthrough(text: str, interpreter: Path, root: Path, *, name: str) -> list[str]:
    """Run every command of the blocks for this OS; return the transcript."""
    cwd = root / "cwd"
    cwd.mkdir(parents=True)
    home = cwd / ".harnessed"  # where a scheduled process would resolve it
    user = root / "user"
    user.mkdir()
    env = scrubbed_env(
        home,
        {
            "HARNESSED_PROVIDER": "fake",
            "HOME": str(user),
            "USERPROFILE": str(user),
            "XDG_CONFIG_HOME": str(user / ".config"),
        },
    )
    seen: dict[str, str] = {}
    transcript: list[str] = []
    ran = 0
    for marker, steps in _blocks(text):
        if marker is not None and marker != THIS_OS:
            continue
        for step in steps:
            argv = [_PLACEHOLDER.sub(lambda m: seen[m.group(1)], word) for word in step.argv]
            if argv[0] not in NO_PROVIDER_OPTION:
                assert argv[-2:] == [
                    "--provider",
                    "fake",
                ], f"{name}: no --provider fake: {step.line}"
            done = run_installed(interpreter, argv, cwd=cwd, env=env)
            assert (
                done.returncode == 0
            ), f"{step.line} exited {done.returncode}\n{done.stdout}\n{done.stderr}"
            actual = _clean(done.stdout)
            _compare(step, actual, seen)
            transcript += [f"$ harnessed {' '.join(argv)}", *actual]
            ran += 1
    assert ran, f"{name}: no command ran on {THIS_OS}"
    return transcript


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "walk"


@pytest.mark.parametrize("pillar", PILLARS)
def test_every_command_in_a_walkthrough_prints_what_is_shown(
    pillar: str, installed_wheel: Path, root: Path, clean_env: None
) -> None:
    text = (package_dir(installed_wheel) / "corpus" / "walkthroughs" / f"{pillar}.md").read_text(
        encoding="utf-8"
    )
    transcript = _run_walkthrough(text, installed_wheel, root, name=pillar)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{pillar}.txt").write_text(
        f"329 - the {pillar} walkthrough on {THIS_OS}\n\n" + "\n".join(transcript) + "\n",
        encoding="utf-8",
    )


def test_an_edited_figure_in_a_seeded_copy_fails(
    installed_wheel: Path, root: Path, clean_env: None
) -> None:
    text = (package_dir(installed_wheel) / "corpus" / "walkthroughs" / "evals.md").read_text(
        encoding="utf-8"
    )
    assert "score 0.8  4/5 passed" in text
    seeded = text.replace("score 0.8  4/5 passed", "score 0.9  4/5 passed", 1)
    with pytest.raises(AssertionError, match="score 0.9"):
        _run_walkthrough(seeded, installed_wheel, root, name="seeded")


@pytest.mark.parametrize("pillar", PILLARS)
def test_each_walkthrough_names_sources_that_exist(pillar: str, installed_wheel: Path) -> None:
    package = package_dir(installed_wheel)
    text = (package / "corpus" / "walkthroughs" / f"{pillar}.md").read_text(encoding="utf-8")
    named = _SOURCE.findall(text)
    assert named, f"{pillar}.md names no source by package path"
    for path in named:
        assert (package.parent / path).is_file(), f"{pillar}.md names {path}, which is not shipped"


def test_the_walkthroughs_are_found_from_the_install_and_the_checkout() -> None:
    found = {path.stem for path in shipped.walkthroughs_dir().glob("*.md")}
    assert found == set(PILLARS)


def test_the_blocks_are_marked_per_os_and_use_placeholders() -> None:
    text = (shipped.walkthroughs_dir() / "harness.md").read_text(encoding="utf-8")
    markers = {marker for marker, _ in _blocks(text) if marker}
    assert markers == {"linux", "macos", "windows"}
    for pillar in PILLARS:
        body = (shipped.walkthroughs_dir() / f"{pillar}.md").read_text(encoding="utf-8")
        assert not re.search(r"\b(cand_|run_0|eval_0|imp_0|bat_|mem_0|epi_0)\w{6,}", body)
