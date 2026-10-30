"""
FR-63: the dashboard is a static export shipped inside the wheel, and staleness is a test.

Ticket 123's acceptance criteria in executable form (decisions 43, 44; MUST rules 22, 23):

- ``next.config.ts`` exports statically and declares no production rewrite; the one rewrite it
  holds is the development loop's, guarded by ``NODE_ENV``.
- The v3 transport is gone: no proxy handler, no origin module, no ``HARNESSED_GATEWAY_URL``, no
  ``x-harnessed-transport`` header, no ``/gw`` path; every read is the relative ``/v1/...``.
- No page and no layout is ``force-dynamic``, because an export has no server to be dynamic on.
- ``next build`` wrote one ``.html`` per page and no route handler into ``dashboard/out/``.
- The export stage staged that directory into the package with a ``.stamp``, and the stamp is a
  sha256 over the sorted relative paths and contents of the dashboard's sources.
- An edited source with no rebuild fails here, **naming the first file that differs**.
- ``uv build`` ships the export's ``index.html`` inside the wheel, and the build backend shells
  out to node **0** times: node is a build-time dependency of the dashboard, never of the wheel.
- The dashboard harness has seven stages with ``export`` between ``build`` and ``e2e``, and the
  Python harness is still six.

The stamp format is shared with ``dashboard/scripts/stage-export.mjs``, which writes it. Both
sides walk the same trees, sort the same relative paths and hash the same bytes; the test below
that runs the node stager over a synthetic tree is what keeps the two from drifting apart.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "dashboard"
PACKAGE = REPO_ROOT / "src" / "harnessed"
EXPORT = PACKAGE / "dashboard_static"
OUT = DASHBOARD / "out"
STAGER = DASHBOARD / "scripts" / "stage-export.mjs"
NEXT_CONFIG = DASHBOARD / "next.config.ts"
VERIFY_DASHBOARD_PS1 = REPO_ROOT / "scripts" / "verify-dashboard.ps1"
VERIFY_DASHBOARD_SH = REPO_ROOT / "scripts" / "verify-dashboard.sh"
VERIFY_PS1 = REPO_ROOT / "scripts" / "verify.ps1"

#: What the stamp covers, relative to ``dashboard/``: four trees and three files (decision 44).
STAMPED_TREES = ("app", "components", "lib", "public")
STAMPED_FILES = ("package-lock.json", "next.config.ts", "tsconfig.json")

#: The stamp's name inside the staged export.
STAMP_NAME = ".stamp"

#: The areas decision 28's scan judges, and the ones the transport may not survive in.
SOURCE_AREAS = ("app", "components", "lib")

#: The dashboard harness's stages, in order (decision 30, seven from v4).
DASHBOARD_STAGES = ("install", "lint", "typecheck", "test", "build", "export", "e2e")

#: The command the stale-export message names, so an operator knows what to run.
BUILD_COMMAND = "npm --prefix dashboard run build"
EXPORT_COMMAND = "npm --prefix dashboard run export"


# --------------------------------------------------------------------------------------------
# The stamp: one digest over sorted paths and contents, and one line per file so a difference
# can be named
# --------------------------------------------------------------------------------------------


def stamped_files(dashboard: Path) -> list[tuple[str, Path]]:
    """Every file the stamp covers, as ``(relative posix path, path)``, sorted by the former."""
    found: list[tuple[str, Path]] = []
    for tree in STAMPED_TREES:
        base = dashboard / tree
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                found.append((path.relative_to(dashboard).as_posix(), path))
    for name in STAMPED_FILES:
        path = dashboard / name
        if path.is_file():
            found.append((name, path))
    return sorted(found, key=lambda entry: entry[0])


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_stamp(dashboard: Path) -> tuple[str, dict[str, str]]:
    """The overall digest and the per-file digests, from the tree as it is now."""
    overall = hashlib.sha256()
    per_file: dict[str, str] = {}
    for relative, path in stamped_files(dashboard):
        data = path.read_bytes()
        overall.update(relative.encode("utf-8"))
        overall.update(b"\0")
        overall.update(str(len(data)).encode("ascii"))
        overall.update(b"\0")
        overall.update(data)
        per_file[relative] = file_digest(data)
    return overall.hexdigest(), per_file


def render_stamp(overall: str, per_file: dict[str, str]) -> str:
    lines = [f"sha256:{overall}"]
    lines.extend(f"{digest}  {relative}" for relative, digest in sorted(per_file.items()))
    return "\n".join(lines) + "\n"


def parse_stamp(text: str) -> tuple[str, dict[str, str]]:
    lines = text.splitlines()
    if not lines or not lines[0].startswith("sha256:"):
        raise ValueError("the stamp's first line is not 'sha256:<digest>'")
    per_file: dict[str, str] = {}
    for line in lines[1:]:
        digest, separator, relative = line.partition("  ")
        if not separator or not relative:
            raise ValueError(f"the stamp holds a line that is not '<digest>  <path>': {line!r}")
        per_file[relative] = digest
    return lines[0].removeprefix("sha256:"), per_file


def staleness(dashboard: Path, stamp_text: str) -> str | None:
    """``None`` when the stamp matches the tree; else one sentence naming the first difference.

    "First" is first in the sorted relative-path order both writers use, so the file named is
    deterministic and is the same file the stager would hash first.
    """
    stamped_overall, stamped_files_ = parse_stamp(stamp_text)
    overall, per_file = compute_stamp(dashboard)
    if overall == stamped_overall and per_file == stamped_files_:
        return None
    for relative in sorted(set(per_file) | set(stamped_files_)):
        if relative not in stamped_files_:
            return f"{relative} was added since the export was staged; run {BUILD_COMMAND}"
        if relative not in per_file:
            return f"{relative} was removed since the export was staged; run {BUILD_COMMAND}"
        if per_file[relative] != stamped_files_[relative]:
            return f"{relative} changed since the export was staged; run {BUILD_COMMAND}"
    return f"the stamp's digest does not match the tree; run {BUILD_COMMAND}"


# --------------------------------------------------------------------------------------------
# Helpers over the real tree
# --------------------------------------------------------------------------------------------


def source_files(dashboard: Path, areas: tuple[str, ...] = SOURCE_AREAS) -> list[Path]:
    found: list[Path] = []
    for area in areas:
        base = dashboard / area
        if base.is_dir():
            found.extend(p for p in base.rglob("*") if p.suffix in {".ts", ".tsx"})
    return sorted(found)


def page_files(dashboard: Path) -> list[Path]:
    return sorted((dashboard / "app").rglob("page.tsx"))


def expected_html(dashboard: Path) -> set[str]:
    """One ``.html`` per page, as export mode names them, plus Next's own not-found pair."""
    names: set[str] = set()
    for page in page_files(dashboard):
        route = page.parent.relative_to(dashboard / "app").as_posix()
        names.add("index.html" if route == "." else f"{route}.html")
    # Next 16 writes the not-found page twice: at its route and as the `404.html` a static host
    # serves for a path no file matches (decision 43's `StaticFiles` does the same).
    names.update({"404.html", "_not-found.html"})
    return names


def occurrences(needle: str, files: list[Path]) -> list[str]:
    return [
        f"{path.relative_to(DASHBOARD).as_posix()}"
        for path in files
        if needle in path.read_text(encoding="utf-8")
    ]


_LINE_COMMENT = re.compile(r"^[ 	]*//.*$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def code_of(path: Path) -> str:
    """The file with its comments stripped: what a scan of *fetches* judges, not of prose."""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", path.read_text(encoding="utf-8")))


def code_occurrences(needle: str, files: list[Path]) -> list[str]:
    return [
        f"{path.relative_to(DASHBOARD).as_posix()}" for path in files if needle in code_of(path)
    ]


# --------------------------------------------------------------------------------------------
# next.config.ts: static export, and no rewrite outside development
# --------------------------------------------------------------------------------------------


def test_the_config_exports_statically() -> None:
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    assert re.search(r"""output:\s*["']export["']""", text), "next.config.ts lacks output: 'export'"


def test_the_only_rewrite_is_the_development_loop_and_it_is_guarded() -> None:
    """Decision 44: `/v1/*` and `/control/v1/*` go to 127.0.0.1:8756 under `next dev` alone.

    The behaviour - `rewrites()` answering `[]` outside development - is pinned by
    `dashboard/tests/unit/export-config.test.ts`, which imports the config and calls it. This
    side pins the shape: the guard is spelled as decision 44 spells it, the origin appears once,
    and nothing else in the config names an origin.
    """
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    assert 'process.env.NODE_ENV === "development"' in text
    assert text.count("http://127.0.0.1:8756") == 1
    assert len(re.findall(r"https?://", text)) == 1
    assert "/v1/:path*" in text and "/control/v1/:path*" in text


# --------------------------------------------------------------------------------------------
# The transport is gone
# --------------------------------------------------------------------------------------------


def test_the_proxy_handler_and_the_origin_module_are_gone() -> None:
    assert not (DASHBOARD / "app" / "gw").exists(), "app/gw/** still exists"
    assert not (DASHBOARD / "lib" / "gateway-origin.ts").exists(), "lib/gateway-origin.ts exists"


def test_the_gateway_url_variable_is_read_zero_times() -> None:
    files = [*source_files(DASHBOARD, (*SOURCE_AREAS, "tests/unit")), NEXT_CONFIG]
    assert occurrences("HARNESSED_GATEWAY_URL", files) == []


def test_the_transport_header_and_its_test_are_gone() -> None:
    files = source_files(DASHBOARD, (*SOURCE_AREAS, "tests"))
    assert occurrences("x-harnessed-transport", files) == []
    assert not (DASHBOARD / "tests" / "e2e" / "transport.spec.ts").exists()


def test_no_source_names_the_proxy_path() -> None:
    """0 `/gw` in code under app/, components/ and lib/. Comments are prose, not fetches: the one
    that survives, in `components/ProvenanceDrawer.tsx`, is a scope_out file ticket 124 rewrites."""
    assert code_occurrences("/gw", source_files(DASHBOARD)) == []


def test_both_fetchers_read_the_relative_v1_path() -> None:
    fetch_panel = (DASHBOARD / "lib" / "fetch-panel.ts").read_text(encoding="utf-8")
    stream = (DASHBOARD / "lib" / "stream.ts").read_text(encoding="utf-8")
    assert 'export const GATEWAY_ORIGIN = "/v1";' in fetch_panel
    assert re.search(r"fetch\(route,", fetch_panel), "fetch-panel.ts does not fetch the route"
    assert re.search(r"fetch\(route,", stream), "stream.ts does not fetch the route"
    assert "GATEWAY_PROXY_PREFIX" not in fetch_panel and "GATEWAY_PROXY_PREFIX" not in stream


def test_the_origin_context_defaults_to_the_relative_origin_and_the_shell_provides_it() -> None:
    panel_frame = (DASHBOARD / "components" / "PanelFrame.tsx").read_text(encoding="utf-8")
    app_shell = (DASHBOARD / "components" / "AppShell.tsx").read_text(encoding="utf-8")
    layout = (DASHBOARD / "app" / "layout.tsx").read_text(encoding="utf-8")
    assert "createContext<string>(GATEWAY_ORIGIN)" in panel_frame
    assert "<GatewayOriginContext.Provider value={GATEWAY_ORIGIN}>" in app_shell
    assert "gatewayUrl" not in layout and "gateway-origin" not in layout


# --------------------------------------------------------------------------------------------
# No page is dynamic
# --------------------------------------------------------------------------------------------


def test_there_are_thirteen_pages_and_none_is_force_dynamic() -> None:
    # Nine after FR-64; `/console` (FR-81, ticket 139) the tenth; `/models` (FR-89, ticket 147)
    # the eleventh; `/traces` (ticket 206) the twelfth, `/runs` and `/sessions`
    # staying as forwarders; `/memory` (ticket 208) the thirteenth.
    pages = page_files(DASHBOARD)
    assert len(pages) == 13, [p.relative_to(DASHBOARD).as_posix() for p in pages]
    dynamic = occurrences("force-dynamic", [*pages, DASHBOARD / "app" / "layout.tsx"])
    assert dynamic == []
    segments = [p for p in (DASHBOARD / "app").rglob("*") if "[" in p.name]
    assert segments == [], "a bracketed segment cannot be exported (decision 45)"


# --------------------------------------------------------------------------------------------
# The build wrote out/, the export stage staged it, and the stamp matches the tree
# --------------------------------------------------------------------------------------------


def test_the_build_wrote_one_html_per_page_and_no_route_handler() -> None:
    assert (OUT / "index.html").is_file(), f"dashboard/out/ is not built; run {BUILD_COMMAND}"
    files = [p for p in OUT.rglob("*") if p.is_file() and "_next" not in p.parts]
    html = {p.relative_to(OUT).as_posix() for p in files if p.suffix == ".html"}
    assert html == expected_html(DASHBOARD)
    # A route handler exports as a file with no extension at its path; a page as `.html` beside
    # its `.txt` RSC payload. Nothing else may be in the export outside `_next/` but a file the
    # dashboard ships verbatim from `public/` (251's `theme-init.js`).
    public = {
        p.relative_to(DASHBOARD / "public").as_posix() for p in (DASHBOARD / "public").rglob("*")
    }
    extra = sorted(
        p.relative_to(OUT).as_posix()
        for p in files
        if p.suffix not in {".html", ".txt"} and p.relative_to(OUT).as_posix() not in public
    )
    assert not extra, extra


def test_the_export_is_staged_inside_the_package_with_its_stamp() -> None:
    assert (
        EXPORT / "index.html"
    ).is_file(), (
        f"src/harnessed/dashboard_static/ is not staged; run {BUILD_COMMAND} then {EXPORT_COMMAND}"
    )
    assert (EXPORT / STAMP_NAME).is_file(), f"the export has no {STAMP_NAME}; run {EXPORT_COMMAND}"
    staged = {p.relative_to(EXPORT).as_posix() for p in EXPORT.rglob("*") if p.is_file()}
    built = {p.relative_to(OUT).as_posix() for p in OUT.rglob("*") if p.is_file()}
    assert staged == built | {STAMP_NAME}


def test_the_shipped_export_is_not_stale() -> None:
    """The staleness test itself: an edited source with no rebuild fails here, naming the file."""
    stamp = EXPORT / STAMP_NAME
    assert stamp.is_file(), f"no export stamp; run {BUILD_COMMAND} then {EXPORT_COMMAND}"
    assert staleness(DASHBOARD, stamp.read_text(encoding="utf-8")) is None


def test_the_stamp_covers_the_four_trees_and_the_three_files() -> None:
    _, per_file = parse_stamp((EXPORT / STAMP_NAME).read_text(encoding="utf-8"))
    assert set(per_file) == {relative for relative, _ in stamped_files(DASHBOARD)}
    assert "app/layout.tsx" in per_file
    assert "lib/fetch-panel.ts" in per_file
    assert "components/PanelFrame.tsx" in per_file
    for name in STAMPED_FILES:
        assert name in per_file, name


# --------------------------------------------------------------------------------------------
# The staleness check names the first file that differs
# --------------------------------------------------------------------------------------------


def _synthetic_dashboard(root: Path) -> Path:
    dashboard = root / "dashboard"
    (dashboard / "app" / "runs").mkdir(parents=True)
    (dashboard / "components").mkdir()
    (dashboard / "lib").mkdir()
    (dashboard / "app" / "layout.tsx").write_text("export default () => null;\n", encoding="utf-8")
    (dashboard / "app" / "runs" / "page.tsx").write_text(
        "export default () => 1;\n", encoding="utf-8"
    )
    (dashboard / "components" / "A.tsx").write_text("export const A = 1;\n", encoding="utf-8")
    (dashboard / "lib" / "b.ts").write_text("export const b = 2;\n", encoding="utf-8")
    (dashboard / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (dashboard / "next.config.ts").write_text("export default {};\n", encoding="utf-8")
    (dashboard / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    # Not stamped: the tests, the build output, the dependencies.
    (dashboard / "tests").mkdir()
    (dashboard / "tests" / "x.test.ts").write_text("it();\n", encoding="utf-8")
    (dashboard / "out").mkdir()
    (dashboard / "out" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    return dashboard


def test_a_fresh_stamp_matches(tmp_path: Path) -> None:
    dashboard = _synthetic_dashboard(tmp_path)
    assert staleness(dashboard, render_stamp(*compute_stamp(dashboard))) is None


def test_an_edited_source_fails_naming_the_file(tmp_path: Path) -> None:
    dashboard = _synthetic_dashboard(tmp_path)
    stamp = render_stamp(*compute_stamp(dashboard))
    (dashboard / "lib" / "b.ts").write_text("export const b = 3;\n", encoding="utf-8")
    message = staleness(dashboard, stamp)
    assert message is not None
    assert message.startswith("lib/b.ts changed"), message
    assert BUILD_COMMAND in message


def test_the_first_file_in_sorted_order_is_the_one_named(tmp_path: Path) -> None:
    dashboard = _synthetic_dashboard(tmp_path)
    stamp = render_stamp(*compute_stamp(dashboard))
    (dashboard / "lib" / "b.ts").write_text("export const b = 3;\n", encoding="utf-8")
    (dashboard / "app" / "runs" / "page.tsx").write_text(
        "export default () => 2;\n", encoding="utf-8"
    )
    message = staleness(dashboard, stamp)
    assert message is not None
    assert message.startswith("app/runs/page.tsx changed"), message


def test_an_added_and_a_removed_file_are_each_named(tmp_path: Path) -> None:
    dashboard = _synthetic_dashboard(tmp_path)
    stamp = render_stamp(*compute_stamp(dashboard))
    (dashboard / "components" / "B.tsx").write_text("export const B = 1;\n", encoding="utf-8")
    added = staleness(dashboard, stamp)
    assert added is not None and added.startswith("components/B.tsx was added"), added
    (dashboard / "components" / "B.tsx").unlink()
    (dashboard / "components" / "A.tsx").unlink()
    removed = staleness(dashboard, stamp)
    assert removed is not None and removed.startswith("components/A.tsx was removed"), removed


def test_an_edit_outside_the_stamped_set_is_not_staleness(tmp_path: Path) -> None:
    dashboard = _synthetic_dashboard(tmp_path)
    stamp = render_stamp(*compute_stamp(dashboard))
    (dashboard / "tests" / "x.test.ts").write_text("it(); it();\n", encoding="utf-8")
    (dashboard / "out" / "index.html").write_text("<html>x</html>\n", encoding="utf-8")
    assert staleness(dashboard, stamp) is None


def test_the_stamped_set_is_the_four_trees_and_the_three_files(tmp_path: Path) -> None:
    dashboard = _synthetic_dashboard(tmp_path)
    assert [relative for relative, _ in stamped_files(dashboard)] == [
        "app/layout.tsx",
        "app/runs/page.tsx",
        "components/A.tsx",
        "lib/b.ts",
        "next.config.ts",
        "package-lock.json",
        "tsconfig.json",
    ]


# --------------------------------------------------------------------------------------------
# The node stager writes the stamp this file reads, byte for byte
# --------------------------------------------------------------------------------------------

node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node is a build-time dependency and is not on PATH")
def test_the_stager_and_the_checker_agree_on_a_synthetic_tree(tmp_path: Path) -> None:
    dashboard = _synthetic_dashboard(tmp_path)
    target = tmp_path / "staged"
    completed = subprocess.run(
        [str(node), str(STAGER), str(dashboard), str(dashboard / "out"), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert (target / "index.html").read_text(encoding="utf-8") == "<html></html>\n"
    written = (target / STAMP_NAME).read_text(encoding="utf-8")
    assert written == render_stamp(*compute_stamp(dashboard))
    assert staleness(dashboard, written) is None


@pytest.mark.skipif(node is None, reason="node is a build-time dependency and is not on PATH")
def test_the_stager_refuses_to_stage_a_missing_build(tmp_path: Path) -> None:
    dashboard = _synthetic_dashboard(tmp_path)
    shutil.rmtree(dashboard / "out")
    completed = subprocess.run(
        [str(node), str(STAGER), str(dashboard), str(dashboard / "out"), str(tmp_path / "s")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert BUILD_COMMAND in completed.stderr
    assert not (tmp_path / "s").exists()


@pytest.mark.skipif(node is None, reason="node is a build-time dependency and is not on PATH")
def test_the_stager_replaces_a_previous_staging_whole(tmp_path: Path) -> None:
    dashboard = _synthetic_dashboard(tmp_path)
    target = tmp_path / "staged"
    target.mkdir()
    (target / "old.html").write_text("stale\n", encoding="utf-8")
    completed = subprocess.run(
        [str(node), str(STAGER), str(dashboard), str(dashboard / "out"), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (target / "old.html").exists()


# --------------------------------------------------------------------------------------------
# The wheel ships the export, and the build backend never calls node
# --------------------------------------------------------------------------------------------

uv = shutil.which("uv")


def _node_shim(directory: Path, marker: Path) -> None:
    """A `node` first on PATH that records being called. The backend must never reach it."""
    directory.mkdir()
    if sys.platform == "win32":
        (directory / "node.cmd").write_text(f'@echo called > "{marker}"\r\n@exit /b 1\r\n')
        (directory / "node.exe.cmd").write_text(f'@echo called > "{marker}"\r\n@exit /b 1\r\n')
    else:
        shim = directory / "node"
        shim.write_text(f'#!/bin/sh\necho called > "{marker}"\nexit 1\n')
        shim.chmod(0o755)


@pytest.mark.skipif(uv is None, reason="uv is not on PATH")
def test_the_wheel_holds_the_export_and_the_backend_shells_out_to_node_zero_times(
    tmp_path: Path,
) -> None:
    assert (EXPORT / "index.html").is_file(), f"no staged export; run {EXPORT_COMMAND}"
    marker = tmp_path / "node-was-called"
    _node_shim(tmp_path / "shim", marker)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(tmp_path / "shim"), env.get("PATH", "")])
    dist = tmp_path / "dist"
    completed = subprocess.run(
        [str(uv), "build", "--wheel", "--out-dir", str(dist)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert not marker.exists(), "the build backend shelled out to node"
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
    assert "harnessed/dashboard_static/index.html" in names
    assert f"harnessed/dashboard_static/{STAMP_NAME}" in names


def test_the_backend_declares_no_build_hook_and_ships_the_export_as_an_artifact() -> None:
    """A hook is the one place a backend could run node; there is none (decision 44).

    The export rides as a hatchling `artifact` (a VCS-ignored file the wheel carries) rather than
    a `force-include`: the latter raises from every editable build while the export is not
    staged, so `uv sync` on a clean checkout would need node first, which is what FR-63 forbids.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.hatch.build.hooks" not in pyproject
    assert "[tool.hatch.build.targets.wheel.force-include]" not in pyproject
    assert 'artifacts = ["src/harnessed/dashboard_static/**"]' in pyproject


@pytest.mark.skipif(uv is None, reason="uv is not on PATH")
def test_an_editable_build_succeeds_with_no_export_staged(tmp_path: Path) -> None:
    """`uv sync` must never need node: the package installs before the export exists."""
    marker = tmp_path / "node-was-called"
    _node_shim(tmp_path / "shim", marker)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(tmp_path / "shim"), env.get("PATH", "")])
    # A copy of the packaging inputs with no export: the real tree may have one staged.
    source = tmp_path / "checkout"
    (source / "src").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copy(REPO_ROOT / "README.md", source / "README.md")
    shutil.copytree(
        PACKAGE, source / "src" / "harnessed", ignore=shutil.ignore_patterns("dashboard_static")
    )
    assert not (source / "src" / "harnessed" / "dashboard_static").exists()
    completed = subprocess.run(
        [str(uv), "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()


def test_the_staged_export_is_gitignored() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "src/harnessed/dashboard_static/" in ignored
    assert "dashboard/out/" in ignored


# --------------------------------------------------------------------------------------------
# The dashboard harness has seven stages; the Python harness still has six
# --------------------------------------------------------------------------------------------


def _ps1_stages(script: Path) -> list[tuple[str, tuple[str, ...]]]:
    stages: list[tuple[str, tuple[str, ...]]] = []
    for line in script.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^Invoke-Stage\s+'(\w+)'\s+@\((.*)\)\s*$", line.strip())
        if match is None:
            continue
        parts = tuple(p.strip().strip("'\"") for p in match.group(2).split(","))
        stages.append((match.group(1), parts))
    return stages


def _sh_stages(script: Path) -> list[tuple[str, tuple[str, ...]]]:
    stages: list[tuple[str, tuple[str, ...]]] = []
    for line in script.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^stage\s+(\w+)\s+(.*)$", line.strip())
        if match is None:
            continue
        stages.append((match.group(1), tuple(shlex.split(match.group(2)))))
    return stages


def test_the_dashboard_harness_runs_seven_stages_with_export_between_build_and_e2e() -> None:
    stages = _ps1_stages(VERIFY_DASHBOARD_PS1)
    assert [name for name, _ in stages] == list(DASHBOARD_STAGES)
    by_name = dict(stages)
    assert by_name["build"] == ("npm", "--prefix", "dashboard", "run", "build")
    assert by_name["export"] == ("npm", "--prefix", "dashboard", "run", "export")
    assert by_name["e2e"] == ("npm", "--prefix", "dashboard", "run", "e2e")


def test_the_posix_dashboard_harness_is_at_parity() -> None:
    assert _sh_stages(VERIFY_DASHBOARD_SH) == _ps1_stages(VERIFY_DASHBOARD_PS1)


def test_the_export_stage_is_the_stager_and_never_uv_build() -> None:
    package = (DASHBOARD / "package.json").read_text(encoding="utf-8")
    assert '"export": "node scripts/stage-export.mjs"' in package
    for script in (VERIFY_DASHBOARD_PS1, VERIFY_DASHBOARD_SH):
        assert "uv build" not in script.read_text(encoding="utf-8"), script.name


def test_the_python_harness_still_has_six_stages() -> None:
    assert len(_ps1_stages(VERIFY_PS1)) == 6
