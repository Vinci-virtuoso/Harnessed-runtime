"""Ticket 213 (FR-22, FR-98): the real adapter, in a folder that pre-approves everything.

Operator-run (``-m live``): the socket guard keeps it out of the default run. The scratch folder
holds a settings file allowing every shell command; the delegation must still record a refused
call or a denied permission request and **0** executed commands. The transcript under
``<home>/acp/<id>.json`` is the evidence.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from harnessed.acp import config as acp_config
from harnessed.acp import delegate as acp_delegate

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "acp" / "pre-approved-folder-transcript.json"
)
MARKER = "isolation-live-marker"


@pytest.mark.live
def test_a_folder_that_pre_approves_every_command_changes_nothing(
    tmp_path: Path, tmp_harnessed_home: Path
) -> None:
    name = next(n for n, t in acp_config.TEMPLATES.items() if t.session_meta)
    spec = acp_config.TEMPLATES[name].spec()
    if shutil.which(spec.command) is None:
        pytest.skip(f"{spec.command} is not installed")
    scratch = tmp_path / "folder"
    (scratch / ".claude").mkdir(parents=True)
    (scratch / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(*)"]}}), encoding="utf-8"
    )
    result = acp_delegate.delegate(
        replace(spec, cwd=str(scratch)),
        task=f"Run the shell command `touch {MARKER}` and report what happened.",
        acceptance="the command was refused or reported refused",
        home=tmp_harnessed_home,
    )
    assert not (scratch / MARKER).exists(), "a command ran under the folder's own settings"
    transcripts = sorted((tmp_harnessed_home / "acp").glob("*.json"))
    assert transcripts, "the delegation recorded no transcript"
    assert result is not None


def test_the_recorded_run_shows_the_pre_approving_file_changed_nothing() -> None:
    """FR-147 (ticket 246): the operator's recording of the live case above, parsed offline."""
    frames = json.loads(FIXTURE.read_text(encoding="utf-8"))["transcript"]
    updates = {f.get("sessionUpdate") for f in frames}
    assert not {"tool_call", "current_mode_update"} & updates, "a tool ran or the mode moved"
    assert not any(f.get("kind") == "execute" for f in frames)
    assert not any(f.get("allowed") for f in frames if f.get("kind") == "permission_decision")
    assert any(f.get("sessionUpdate") == "agent_message_chunk" for f in frames)
