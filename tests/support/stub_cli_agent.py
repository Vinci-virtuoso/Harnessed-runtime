"""A plain, protocol-free CLI agent: no ACP, no JSON-RPC, just argv, stdin and stdout (FR-99).

``acp/cli_runner.py`` spawns a ``kind = "cli"`` agent as an ordinary synchronous subprocess, never
through the Agent Client Protocol, so its stub is an ordinary script rather than
``stub_acp_agent.py``'s hand-rolled wire. Every behaviour a test needs is one environment variable,
read directly - this is a test-support child process, so the single-reader rule governs the
harness package and not the processes it spawns (the same carve-out ``stub_acp_agent.py`` takes).

Every write is flushed immediately, and stdin is always fully drained before anything else runs:
that is what lets the EOF acceptance criterion tell "the runner closed stdin at once" from "the
runner never closes it" - a hang would show up as the test's own timeout, not this script's.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

__all__ = ["main"]


def write_and_commit() -> None:
    """Ticket 244: ``HARNESSED_STUB_WRITE`` names a file written under the cwd, and
    ``HARNESSED_STUB_COMMIT=1`` commits it, so a test can read what the repository became."""
    name = os.environ.get("HARNESSED_STUB_WRITE", "").strip()
    if not name:
        return
    target = Path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("written by the stub\n", encoding="utf-8")
    if os.environ.get("HARNESSED_STUB_COMMIT", "").strip() == "1":
        identity = ["-c", "user.name=stub", "-c", "user.email=stub@example.invalid"]
        subprocess.run(["git", "add", "--", name], check=True, capture_output=True)
        subprocess.run(
            ["git", *identity, "commit", "-q", "-m", "stub change"],
            check=True,
            capture_output=True,
        )


def _argv_task(argv: list[str]) -> str | None:
    """The value after ``--task``, when the harness put the task on argv."""
    if "--task" in argv:
        index = argv.index("--task")
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    # Ticket 214: a grandchild that touches `<path>.beat` every 50 ms, so a test can see whether
    # it is still running without needing its pid to mean anything outside a pid namespace.
    grandchild = os.environ.get("HARNESSED_STUB_GRANDCHILD", "").strip()
    if grandchild:
        code = (
            "import sys, time, pathlib\n"
            "beat = pathlib.Path(sys.argv[1] + '.beat')\n"
            "while True:\n"
            "    beat.write_text(str(time.time()))\n"
            "    time.sleep(0.05)\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", code, grandchild],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        while not Path(grandchild + ".beat").exists():
            time.sleep(0.05)

    # {relative path: text content}, each written under the child's own cwd (ticket 181's
    # outputs coverage: several differently-named files from one delegation, for
    # `acp/outputs.py::kind_for`).
    write_files = os.environ.get("HARNESSED_STUB_WRITE_FILES", "").strip()
    if write_files:
        for relative, content in json.loads(write_files).items():
            target = Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    write_and_commit()

    sleep_s = float(os.environ.get("HARNESSED_STUB_SLEEP_S", "").strip() or "0")
    if sleep_s > 0:
        time.sleep(sleep_s)

    argv_task = _argv_task(sys.argv[1:])
    # Read stdin to EOF whenever the harness might have put the task there, and also when a test
    # asks a stub that received its task on argv to prove the runner still closed stdin at once.
    if argv_task is None or os.environ.get("HARNESSED_STUB_WAIT_STDIN", "").strip() == "1":
        stdin_task = sys.stdin.read()
    else:
        stdin_task = ""

    write_inside = os.environ.get("HARNESSED_STUB_WRITE_INSIDE", "").strip()
    if write_inside:
        Path(write_inside).write_text("written by the stub", encoding="utf-8")

    write_outside = os.environ.get("HARNESSED_STUB_WRITE_OUTSIDE", "").strip()
    if write_outside:
        Path(write_outside).write_text("written by the stub", encoding="utf-8")

    # Ticket 214: try the write and report the OS error as the reply, so a jail that stops it
    # shows up as a successful delegation whose reply is the error, not as a crash.
    report = os.environ.get("HARNESSED_STUB_WRITE_OUTSIDE_REPORT", "").strip() == "1"
    outside_target = os.environ.get("HARNESSED_STUB_OUTSIDE_TARGET", "").strip()
    if report and outside_target:
        try:
            Path(outside_target).write_text("written by the stub", encoding="utf-8")
        except OSError as exc:
            sys.stdout.write(f"error: {exc}")
        else:
            sys.stdout.write("wrote")
        sys.stdout.flush()
        return

    reply_bytes = os.environ.get("HARNESSED_STUB_REPLY_BYTES", "").strip()
    if reply_bytes:
        sys.stdout.write("x" * int(reply_bytes))
    elif os.environ.get("HARNESSED_STUB_DUMP_ENV", "").strip() == "1":
        sys.stdout.write(json.dumps(dict(os.environ)))
    else:
        sys.stdout.write(argv_task if argv_task is not None else stdin_task)
    sys.stdout.flush()

    exit_code = os.environ.get("HARNESSED_STUB_EXIT_CODE", "").strip()
    if exit_code:
        sys.stderr.write("stub failed on purpose")
        sys.exit(int(exit_code))


if __name__ == "__main__":
    main()
