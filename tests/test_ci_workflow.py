"""One CI workflow runs both harnesses and a clean install on three OSes (ticket 325, FR-181).

A build agent cannot run hosted CI, so the workflow is asserted by parsing it. The first green
hosted run is the operator's record (ticket 327). YAML 1.1 reads the ``on:`` key as ``True``,
hence the ``doc.get("on", doc.get(True))`` lookup.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from support.readme_blocks import README, install_blocks, run_block

ROOT = README.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LINKS = ROOT / "scripts" / "check_links.py"
EVIDENCE_DIR = ROOT / "evidence" / "325"
OSES = ("windows", "macos", "linux")

DOC: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
JOBS: dict[str, Any] = DOC["jobs"]
TEXT = README.read_text(encoding="utf-8")


def _steps(job: str) -> list[dict[str, Any]]:
    return JOBS[job]["steps"]


def _run_steps(job: str) -> list[dict[str, Any]]:
    return [s for s in _steps(job) if "run" in s]


def _index(job: str, needle: str) -> int:
    """The index of the first step whose ``run`` contains ``needle``."""
    for i, step in enumerate(_steps(job)):
        if needle in step.get("run", ""):
            return i
    raise AssertionError(f"{job}: no step runs {needle!r}")


def test_triggers_permissions_and_jobs() -> None:
    triggers = DOC.get("on", DOC.get(True))
    assert set(triggers) == {"push", "pull_request", "workflow_call"}
    assert triggers["push"]["branches"] == ["main"]
    assert DOC["permissions"] == {"contents": "read"}
    assert set(JOBS) == {"build", "verify", "clean-install", "secrets", "docs"}
    for name in ("verify", "clean-install"):
        assert JOBS[name]["strategy"]["matrix"]["os"] == list(OSES)
    assert JOBS["clean-install"]["needs"] == "build"
    assert not any("uses" in s and "checkout" in s["uses"] for s in _steps("clean-install"))


def test_no_secrets_every_action_pinned_and_no_live_selection() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert not re.search(r"-m\s+[\"']?live", text)
    uses = [line for line in text.splitlines() if re.match(r"\s*(- )?uses:", line)]
    assert uses
    for line in uses:
        assert re.search(r"uses: [\w./-]+@[0-9a-f]{40}\s+# v\d+(\.\d+)*\s*$", line), line


def test_verify_runs_the_dashboard_harness_then_the_python_harness() -> None:
    for name in ("verify",):
        runs = [s["run"] for s in _run_steps(name)]
        dash = next(i for i, r in enumerate(runs) if "verify-dashboard" in r)
        py = next(i for i, r in enumerate(runs) if re.search(r"scripts/verify\.(sh|ps1)", r))
        assert dash < py
    windows = [s for s in _run_steps("verify") if "verify-dashboard.ps1" in s["run"]]
    assert windows and all(s.get("shell") == "pwsh" for s in windows)
    assert any("verify-dashboard.sh" in s["run"] for s in _run_steps("verify"))
    ps1 = [s for s in _run_steps("verify") if re.search(r"scripts/verify\.ps1", s["run"])]
    assert ps1 and all(s.get("shell") == "pwsh" for s in ps1)


def test_browser_install_uses_the_dashboard_lockfile_before_e2e() -> None:
    steps = _steps("verify")
    install = _index("verify", "npm --prefix dashboard ci")
    browsers = [s for s in steps if s.get("name") == "Install Playwright Chromium"]
    assert len(browsers) == 2
    for step in browsers:
        assert install < steps.index(step) < _index("verify", "verify-dashboard")
        assert step["working-directory"] == "dashboard"
        assert step["run"].startswith("npx --no-install playwright install ")


def test_clean_install_selects_the_supported_python_on_every_os() -> None:
    import shlex
    import tomllib

    command = shlex.split(run_block(TEXT).splitlines()[0])
    assert command[:3] == ["uv", "tool", "install"]
    assert command[command.index("--python") + 1] == "3.12"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["requires-python"] == ">=3.12,<3.13"


@pytest.mark.skipif(os.name == "nt", reason="ci: linux and macos jobs exercise POSIX lib64 aliases")
def test_installed_stamp_accepts_an_alias_but_rejects_a_second_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = next(s["run"] for s in _run_steps("clean-install") if ".stamp" in s["run"])
    monkeypatch.chdir(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "harnessed_runtime-0.7.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("harnessed/dashboard_static/.stamp", b"built stamp")
    tools = tmp_path / "tools"
    environment = tools / "harnessed-runtime"
    relative = Path("python3.12/site-packages/harnessed/dashboard_static/.stamp")
    stamp = environment / "lib" / relative
    stamp.parent.mkdir(parents=True)
    stamp.write_bytes(b"built stamp")
    (environment / "lib64").symlink_to("lib", target_is_directory=True)

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        assert command in (["uv", "tool", "dir"], ["harnessed", "--version"])
        return SimpleNamespace(stdout=str(tools) if command[0] == "uv" else "0.7.0\n")

    monkeypatch.setattr(subprocess, "run", run)
    exec(compile(proof, "ci-installed-stamp", "exec"), {})
    stamp.write_bytes(b"different stamp")
    with pytest.raises(AssertionError, match="not the built wheel"):
        exec(compile(proof, "ci-installed-stamp", "exec"), {})
    stamp.write_bytes(b"built stamp")
    extra = environment / "another-lib" / relative
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"built stamp")
    with pytest.raises(AssertionError):
        exec(compile(proof, "ci-installed-stamp", "exec"), {})


def test_clean_install_hides_node_then_runs_the_readme_blocks_verbatim() -> None:
    blocks = install_blocks(TEXT)
    hide = _index("clean-install", "node")
    assert "which" in _steps("clean-install")[hide]["run"]
    one = {}
    for name in OSES:
        applies = f"matrix.os == '{name}'" if name == "windows" else "matrix.os != 'windows'"
        match = [
            s
            for s in _run_steps("clean-install")
            if s["run"] == blocks[name] and s["if"] == applies
        ]
        assert len(match) == 1, name
        one[name] = match[0]
    two = [s for s in _run_steps("clean-install") if s["run"] == run_block(TEXT)]
    assert len(two) == 1
    assert two[0]["env"]["UV_FIND_LINKS"].endswith("/dist")
    steps = _steps("clean-install")
    first = min(steps.index(s) for s in one.values())
    path = _index("clean-install", "GITHUB_PATH")
    assert hide < first < path < steps.index(two[0])


def test_clean_install_proves_the_wheel_the_dashboard_and_the_scheduler() -> None:
    steps = _steps("clean-install")
    block_two = next(i for i, s in enumerate(steps) if s.get("run") == run_block(TEXT))
    version = next(
        i for i, s in enumerate(steps) if "--version" in s.get("run", "") and i > block_two
    )
    stamp = next(i for i, s in enumerate(steps) if ".stamp" in s.get("run", ""))
    poll = next(i for i, s in enumerate(steps) if "/v1/health" in s.get("run", ""))
    assert "8756" in steps[poll]["run"]
    assert re.search(r"\b60\b", steps[poll]["run"])
    assert version > block_two and stamp > block_two
    install = _index("clean-install", "schedule install")
    doctor = max(
        i for i, s in enumerate(steps) if re.search(r"harnessed doctor\b", s.get("run", ""))
    )
    uninstall = _index("clean-install", "schedule uninstall")
    assert max(poll, version, stamp) < install < doctor < uninstall
    linger = _index("clean-install", "loginctl enable-linger")
    assert linger < install
    assert "linux" in steps[linger]["if"]
    assert "CrontabBackend" in steps[doctor]["run"]


def test_secrets_scan_pins_gitleaks_by_version_and_sha256() -> None:
    secrets = _steps("secrets")
    checkout = next(s for s in secrets if "uses" in s)
    assert checkout["with"]["fetch-depth"] == 0
    text = "\n".join(s.get("run", "") for s in secrets)
    assert re.search(r"gitleaks/gitleaks/releases/download/v8\.\d+\.\d+/", text)
    assert re.search(r"\b[0-9a-f]{64}\b", text)
    assert "sha256sum" in text
    assert "gitleaks git --redact --exit-code 1" in text


def test_docs_runs_the_link_script_and_verify_never_does() -> None:
    assert any("scripts/check_links.py" in s.get("run", "") for s in _steps("docs"))
    for script in ("verify.sh", "verify.ps1", "verify-dashboard.sh", "verify-dashboard.ps1"):
        assert "check_links" not in (ROOT / "scripts" / script).read_text(encoding="utf-8")


def _links() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_links", LINKS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_links_reads_the_readme_external_links() -> None:
    found = _links().external_links(TEXT)
    assert "https://docs.astral.sh/uv/" in found
    assert all(link.startswith("https://") or link.startswith("http://") for link in found)
    assert not any("127.0.0.1" in link or "localhost" in link for link in found)
    assert len(found) == len(set(found))


def test_check_links_reports_each_failure_and_never_opens_a_socket_itself() -> None:
    module = _links()

    def fetch(url: str) -> int:
        return 404 if "gone" in url else 200

    bad = module.broken(["https://a.example/ok", "https://a.example/gone"], fetch)
    assert bad == [("https://a.example/gone", "404")]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "workflow.txt").write_text(
        f"jobs: {sorted(JOBS)}\nverify matrix: {JOBS['verify']['strategy']['matrix']['os']}\n"
        f"clean-install needs: {JOBS['clean-install']['needs']}\n",
        encoding="utf-8",
    )


# ---- 337: a release is a version tag CI builds once ---------------------------------------------

RELEASE = ROOT / ".github" / "workflows" / "release.yml"
RELEASE_DOC: dict[Any, Any] = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
RELEASE_JOBS: dict[str, Any] = RELEASE_DOC["jobs"]


def test_the_build_job_writes_the_notices_after_npm_ci_and_before_the_wheel() -> None:
    ci = _index("build", "npm --prefix dashboard ci")
    notices = _index("build", "scripts/notices.py")
    wheel = _index("build", "uv build")
    assert ci < notices < wheel
    assert "uv run" in _steps("build")[notices]["run"]


def test_a_pushed_version_tag_is_the_only_release_trigger() -> None:
    triggers = RELEASE_DOC.get("on", RELEASE_DOC.get(True))
    assert triggers == {"push": {"tags": ["v*.*.*"]}}
    assert RELEASE_DOC["permissions"] == {"contents": "read"}
    assert list(RELEASE_JOBS) == ["check", "ci", "publish-pypi", "github-release"]


def test_check_compares_the_tag_with_pyprojects_version() -> None:
    run = "\n".join(s.get("run", "") for s in RELEASE_JOBS["check"]["steps"])
    assert "GITHUB_REF_NAME" in run and "pyproject.toml" in run and '"v${version}"' in run


def test_the_whole_ci_matrix_runs_before_anything_is_published() -> None:
    assert RELEASE_JOBS["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert RELEASE_JOBS["ci"]["needs"] == "check"


def test_pypi_is_published_by_trusted_publishing_from_the_ci_wheel() -> None:
    job = RELEASE_JOBS["publish-pypi"]
    assert job["needs"] == "ci"
    assert job["environment"] == "pypi"
    assert job["permissions"] == {"id-token": "write"}
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert any(u.startswith("actions/download-artifact@") for u in uses)
    publish = [u for u in uses if u.startswith("pypa/gh-action-pypi-publish@")]
    assert publish and re.fullmatch(r"pypa/gh-action-pypi-publish@[0-9a-f]{40}", publish[0])
    assert not any("uv build" in s.get("run", "") for s in job["steps"])


def test_the_github_release_carries_the_files_and_their_checksums() -> None:
    job = RELEASE_JOBS["github-release"]
    assert job["needs"] == "publish-pypi"
    assert job["permissions"] == {"contents": "write"}
    run = "\n".join(s.get("run", "") for s in job["steps"])
    assert 'gh release create "${GITHUB_REF_NAME}" dist/* SHA256SUMS --generate-notes' in run


def test_the_release_workflow_names_no_secret_and_pins_every_action() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert "secrets." not in text
    uses = [line for line in text.splitlines() if re.match(r"\s*(- )?uses:", line)]
    for line in uses:
        if "./.github/workflows/" in line:
            continue
        assert re.search(r"uses: [\w./-]+@[0-9a-f]{40}\s+# v\d+(\.\d+)*\s*$", line), line
