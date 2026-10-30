"""The README installs a newcomer in two blocks per OS, PowerShell first (ticket 324, FR-184).

Block 1 installs uv and is the only text that differs per OS; block 2 is the same bytes everywhere
(decision 91). The blocks are extracted by marker so the CI clean-install job can run them verbatim.
"""

from __future__ import annotations

import json
import re

import pytest

from support.readme_blocks import OSES, README, install_blocks, run_block
from test_e2e_flywheel import Proof, conn, proved, spy

__all__ = ["conn", "proved", "spy"]  # re-exported so pytest finds the fixtures here

ROOT = README.parent
EVIDENCE_DIR = ROOT / "evidence" / "324"

#: The public repository, in the exact case the operator creates (gate input, 2026-10-13).
REPO = "Vinci-virtuoso/Harnessed-runtime"

#: Decision 91's block 2 with the candidate id left to be proved against the in-process run.
RUN_LINES = [
    'uv tool install "harnessed-runtime[gateway]"',
    "harnessed --version",
    "mkdir harnessed-demo",
    "cd harnessed-demo",
    "harnessed doctor --offline",
    "harnessed eval run smoke --fresh --provider fake",
    "harnessed improve run --dry-run --provider fake",
    "harnessed improve promote {cand} --provider fake",
    "harnessed eval run smoke --fresh --provider fake",
    "harnessed dashboard",
]

TEXT = README.read_text(encoding="utf-8")


def test_install_blocks_per_os_with_windows_first() -> None:
    blocks = install_blocks(TEXT)
    assert blocks["windows"].strip() == "irm https://astral.sh/uv/install.ps1 | iex"
    for name in ("macos", "linux"):
        assert blocks[name].strip() == "curl -LsSf https://astral.sh/uv/install.sh | sh"
    order = [TEXT.index(f"<!-- install:{name} -->") for name in OSES]
    assert order[0] == min(order)
    assert TEXT.index("<!-- run -->") > max(order)
    between = TEXT[max(order) : TEXT.index("<!-- run -->")]
    assert "open a new terminal" in between.lower()


def test_block_two_is_shared_and_matches_the_proof(proved: Proof) -> None:
    assert TEXT.count("<!-- run -->") == 1
    cand = proved.cand_id
    expected = "\n".join(line.format(cand=cand) for line in RUN_LINES) + "\n"
    assert run_block(TEXT) == expected
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    blocks = install_blocks(TEXT)
    (EVIDENCE_DIR / "blocks.txt").write_text(
        "".join(f"[{name}]\n{blocks[name]}\n" for name in OSES) + f"[run]\n{run_block(TEXT)}",
        encoding="utf-8",
    )


def test_home_rule_is_stated_once_before_block_two() -> None:
    rule = "Harnessed keeps its ledger in the folder you run it from"
    assert TEXT.count(rule) == 1
    assert TEXT.index(rule) < TEXT.index("<!-- run -->")


def test_init_section_follows_with_groq_then_openrouter() -> None:
    after = TEXT[TEXT.index("<!-- run -->") :]
    assert "harnessed init" in after
    assert after.index("GROQ_API_KEY") < after.index("OPENROUTER_API_KEY")
    assert "routing" in after[after.index("OPENROUTER_API_KEY") :].lower()


def test_clone_url_pins_the_repository_spelling() -> None:
    urls = re.findall(
        r"github\.com/(Vinci-virtuoso/[\w.-]+?)(?:\.git)?(?=[\s/)\"'#]|$)", TEXT, re.I
    )
    assert urls
    assert set(urls) == {REPO}


def test_relative_links_resolve() -> None:
    for target in re.findall(r"\]\((?!https?:|mailto:|#)([^)#\s]+)", TEXT):
        assert (ROOT / target).exists(), target


@pytest.mark.parametrize(
    "pattern",
    [
        r"C:\\Users\\[^\\\s<]+",
        r"/home/(?!user\b)[^/\s<]+/",
        r"/Users/[^/\s<]+/",
        r"Harnessed-(mirror|backup)",
        "operator" + "-inputs",  # spelled in two parts so the records scan stays clean
    ],
)
def test_no_private_path_mirror_or_operator_link(pattern: str) -> None:
    assert re.search(pattern, TEXT) is None


def test_no_sentence_promises_a_dashboard_from_uv_sync_alone() -> None:
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", TEXT))
    offenders = [s for s in sentences if "uv sync" in s and "dashboard" in s.lower()]
    assert offenders == []


def test_the_ci_clean_install_runs_the_readme_blocks_byte_for_byte() -> None:
    import yaml

    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    runs = [s["run"] for s in doc["jobs"]["clean-install"]["steps"] if "run" in s]
    blocks = install_blocks(TEXT)
    for name in OSES:
        assert blocks[name] in runs, name
    assert run_block(TEXT) in runs


PILLARS = ("loops", "evals", "memory", "harness")
WALKTHROUGHS = [f"src/harnessed/corpus/walkthroughs/{p}.md" for p in PILLARS]


def test_readme_links_the_four_walkthroughs_and_harnessed_learn() -> None:
    links = set(re.findall(r"\]\((?!https?:|mailto:|#)([^)#\s]+)", TEXT))
    for path in WALKTHROUGHS:
        assert path in links, path
        assert (ROOT / path).is_file(), path
    assert "harnessed learn" in TEXT


def test_contributor_section_lists_the_checkout_steps_in_order() -> None:
    assert "## Contributing" in TEXT
    section = TEXT[TEXT.index("## Contributing") :]
    section = section.split("\n---\n", 1)[0]
    package = (ROOT / "dashboard" / "package.json").read_text(encoding="utf-8")
    assert json.loads(package)["engines"]["node"] in section
    steps = [
        "git clone",
        "uv sync",
        "npm --prefix dashboard ci",
        "npm --prefix dashboard run build",
        "npm --prefix dashboard run export",
    ]
    positions = [section.index(step) for step in steps]
    assert positions == sorted(positions)


def test_one_section_states_offline_groq_and_openrouter() -> None:
    assert TEXT.count("## What needs a key") == 1
    section = TEXT[TEXT.index("## What needs a key") :].split("\n---\n", 1)[0]
    lowered = section.lower()
    assert "offline" in lowered
    assert section.index("GROQ_API_KEY") < section.index("OPENROUTER_API_KEY")
    assert "routing" in lowered
