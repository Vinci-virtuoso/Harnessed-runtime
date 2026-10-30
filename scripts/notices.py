"""Write THIRD_PARTY_NOTICES.md from the two lockfiles (ticket 336, FR-186, decision 108).

    uv run python scripts/notices.py

Python: the runtime closure of `harnessed-runtime[gateway]` in `uv.lock` (the main dependencies
and the
`gateway` extra, never `dev` or `eval`), each licence read from the installed distribution through
`importlib.metadata`. Dashboard: every `dashboard/package-lock.json` entry not marked `dev`, minus
`BUILD_ONLY`, each with its `license` field and licence text from `dashboard/node_modules`
or the checked-in notice when a platform-optional package is absent locally.
Standard library only; it adds no package.
"""

from __future__ import annotations

import fnmatch
import importlib.metadata as metadata
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "THIRD_PARTY_NOTICES.md"
LOCK = ROOT / "uv.lock"
NPM_LOCK = ROOT / "dashboard" / "package-lock.json"
NODE_MODULES = ROOT / "dashboard" / "node_modules"

#: Reviewed (decision 108): these build the dashboard export and are not in it. `sharp` covers
#: everything nested under it in the lockfile.
BUILD_ONLY = ("@img/*", "sharp", "@next/swc-*", "caniuse-lite")

#: Reviewed licences for runtime packages a platform marker keeps off the generating machine, so
#: `importlib.metadata` cannot read them here. A new marker-gated package fails until it is added.
MARKER_GATED = {"colorama": "BSD-3-Clause"}

#: Free-text licence fields and classifiers, normalized to SPDX.
SPDX = {
    "Apache License, Version 2.0": "Apache-2.0",
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD",
    "ISC License": "ISC",
    "ISC License (ISCL)": "ISC",
    "MIT License": "MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "SIL OPEN FONT LICENSE": "OFL-1.1",
}
LICENCE_FILE = re.compile(r"^(licen[cs]e|copying|notice)(\.(md|txt|rst))?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    version: str
    licence: str
    texts: tuple[str, ...]
    note: str = ""


def _spdx(value: str) -> str:
    value = value.strip()
    return SPDX.get(value, value)


def _identify(text: str) -> str | None:
    head = " ".join(text.split()[:12])
    if head.startswith("Apache License Version 2.0"):
        return "Apache-2.0"
    return None


def runtime_closure() -> dict[str, tuple[str, str | None]]:
    """Name -> (locked version, marker) for `harnessed` plus its `gateway` extra, transitively."""
    packages = {p["name"]: p for p in tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]}
    root = packages["harnessed-runtime"]
    todo = [*root["dependencies"], *root["optional-dependencies"]["gateway"]]
    seen: dict[str, tuple[str, str | None]] = {}
    while todo:
        edge = todo.pop()
        if edge["name"] in seen:
            continue
        package = packages[edge["name"]]
        seen[edge["name"]] = (package["version"], edge.get("marker"))
        todo.extend(package.get("dependencies", []))
    return dict(sorted(seen.items()))


def python_entry(name: str, version: str, marker: str | None) -> Entry:
    if name in MARKER_GATED:
        # The reviewed entry wins on every OS, installed or not, so the notices are the same bytes
        # wherever they are generated (colorama is installed on Windows only).
        note = f"installed only where `{marker}`; licence reviewed, text not reproduced"
        return Entry(name, version, MARKER_GATED[name], (), note)
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError as missing:
        message = f"{name} is not installed and has no reviewed MARKER_GATED licence"
        raise SystemExit(message) from missing
    if dist.version != version:
        raise SystemExit(
            f"{name} is installed at {dist.version}, the lock says {version}: uv sync first"
        )
    files = [f for f in dist.files or () if LICENCE_FILE.match(f.name)]
    texts = tuple(f.read_text(encoding="utf-8") for f in files)
    licence = dist.metadata.get("License-Expression") or ""
    if not licence:
        field = (dist.metadata.get("License") or "").strip()
        licence = _spdx(field) if field and "\n" not in field and len(field) < 60 else ""
    if not licence:
        classifiers = [
            c.rsplit(" :: ", 1)[1]
            for c in dist.metadata.get_all("Classifier") or []
            if c.startswith("License :: OSI Approved :: ")
        ]
        licence = " OR ".join(_spdx(c) for c in classifiers)
    if not licence:
        licence = next((found for t in texts if (found := _identify(t))), "")
    if not licence:
        raise SystemExit(f"{name}: no licence in its metadata or its licence files")
    return Entry(name, version, _spdx(licence), texts)


def build_only(path: str) -> bool:
    name = path.split("node_modules/", 1)[1]
    return path.startswith("node_modules/sharp/") or any(
        fnmatch.fnmatchcase(name, pattern) for pattern in BUILD_ONLY
    )


def bundled() -> list[tuple[str, dict]]:
    """Every non-dev lockfile entry not in `BUILD_ONLY`, as (path, entry), in lockfile order."""
    packages = json.loads(NPM_LOCK.read_text(encoding="utf-8"))["packages"]
    return [
        (path, e)
        for path, e in packages.items()
        if path and not e.get("dev") and not build_only(path)
    ]


def _previous_npm_entry(name: str, version: str) -> tuple[tuple[str, ...], str] | None:
    """Reuse the checked-in canonical licence when npm omits a platform-optional package."""
    if not OUT.is_file():
        return None
    contents = OUT.read_text(encoding="utf-8")
    heading = f"### {name} {version}\n"
    start = contents.find(heading)
    if start < 0:
        return None
    start += len(heading)
    ends = [
        position
        for marker in ("\n### ", "\n## ")
        if (position := contents.find(marker, start)) >= 0
    ]
    end = min(ends, default=len(contents))
    section = contents[start:end]
    notes = re.findall(r"^Note: (.*)\.$", section, re.MULTILINE)
    texts = tuple(re.findall(r"```text\n(.*?)\n```", section, re.DOTALL))
    if not notes and not texts:
        return None
    return texts, notes[0] if notes else ""


def npm_entry(path: str, locked: dict) -> Entry:
    name = path.rsplit("node_modules/", 1)[1]
    directory = ROOT / "dashboard" / path
    if not directory.is_dir():
        previous = _previous_npm_entry(name, locked["version"])
        if previous is not None:
            texts, note = previous
            return Entry(name, locked["version"], _spdx(locked["license"]), texts, note)
        note = "optional, not installed on this machine; licence from the lockfile"
        return Entry(name, locked["version"], _spdx(locked["license"]), (), note)
    files = sorted(f for f in directory.iterdir() if f.is_file() and LICENCE_FILE.match(f.name))
    texts = tuple(f.read_text(encoding="utf-8") for f in files)
    note = "" if texts else "the package ships no licence file; licence from its package.json"
    return Entry(name, locked["version"], _spdx(locked["license"]), texts, note)


def render(python: list[Entry], npm: list[Entry]) -> str:
    def section(entries: list[Entry]) -> list[str]:
        lines: list[str] = []
        for e in entries:
            lines += [f"### {e.name} {e.version}", "", f"Licence: `{e.licence}`", ""]
            if e.note:
                lines += [f"Note: {e.note}.", ""]
            for text in e.texts:
                lines += ["```text", text.rstrip("\n"), "```", ""]
        return lines

    lines = [
        "# Third-party notices",
        "",
        "Generated by `uv run python scripts/notices.py` from `uv.lock` and"
        " `dashboard/package-lock.json`;",
        "do not edit by hand. Harnessed itself is licensed under Apache-2.0"
        " (see `LICENSE` and `NOTICE`).",
        "",
        "## Summary",
        "",
        "| package | version | licence | where |",
        "|---|---|---|---|",
    ]
    lines += [f"| {e.name} | {e.version} | {e.licence} | python |" for e in python]
    lines += [f"| {e.name} | {e.version} | {e.licence} | dashboard |" for e in npm]
    lines += [
        "",
        "Used to build the dashboard export and not distributed in it (reviewed, decision 108): "
        + ", ".join(f"`{p}`" for p in BUILD_ONLY)
        + ".",
        "",
        "## Python runtime dependencies (`harnessed-runtime[gateway]`)",
        "",
        *section(python),
        "## Bundled in the dashboard export",
        "",
        *section(npm),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> int:
    python = [python_entry(name, *locked) for name, locked in runtime_closure().items()]
    npm = [npm_entry(path, locked) for path, locked in bundled()]
    OUT.write_text(render(python, npm), encoding="utf-8", newline="\n")
    print(f"{OUT.name}: {len(python)} Python packages, {len(npm)} bundled npm packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
