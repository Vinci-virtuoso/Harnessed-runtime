"""Validate-then-write: the one composition behind ``harnessed init`` and the control app.

FR-87, MUST rule 16. ``init`` is this function's first caller and the control app's providers route
is its second (ticket 146), so a credential set from a terminal and one set from the browser take
the same path to disk. Three rules shape it, moved here verbatim from ``init_cmd``.

* **Merge, never rewrite.** ``.env`` belongs to the operator. The parse-and-merge is
  ``config.merge_env_text``; this module only decides which variables it owns.
* **Nothing is written until the credential is proven.** Validation runs before the first byte
  reaches disk, so a refusal leaves the file exactly as it was.
* **What the operator typed is never printed or returned.** The return is ``init --json``'s
  payload, which names the variable and never its value, and the refusal carries a status, not an
  SDK's message, because an SDK is free to echo a credential back inside one.

One fix ``init`` lacked rides along: on POSIX the written file is ``chmod 0600``.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from harnessed import config, providers
from harnessed.cli import doctor_cmd
from harnessed.cli.doctor_cmd import Rung
from harnessed.errors import ConfigError
from harnessed.host import SeamLine

__all__ = [
    "MODE_SKIPPED",
    "POSIX",
    "configure_provider",
    "ladder",
    "restrict",
]

#: Whether the owner-only mode bit means anything on this host. A module flag rather than an
#: inline test so both branches can be asserted on either host.
POSIX: Final = os.name == "posix"

#: Why the mode call is skipped off POSIX. ``os.chmod`` on Windows only toggles the read-only
#: flag, so ``0o600`` would silently do nothing; access there is the user profile's ACL.
MODE_SKIPPED: Final = (
    "not restricted on Windows: os.chmod sets only the read-only flag there, and the file is "
    "already limited to the user profile's ACL"
)


def ladder(provider: str) -> tuple[Rung, ...]:
    """The four rungs. Rung 1 is live because, by the time this is called, it is.

    This has been through both failure modes. It was a fixed string reading ``live now`` while
    the credential init had just written sat in a ``.env`` ``config.py`` read no key out of, so
    the very next command failed (ticket 038). Ticket 038 added an honest not-yet branch telling
    the operator to export the variable; ticket 039 made the file readable and deleted that
    branch rather than leaving it dormant, because a branch nobody can reach is a lie waiting to
    be re-enabled. The claim is still *checked* rather than assumed - the caller resolves the
    credential back out of the file it just wrote and reports that as ``credential_live``.

    Rungs 3 and 4 are ``doctor``'s own builders, so ``init`` and ``doctor`` print one ladder:
    both upper rungs are read off their apps' routes here too (FR-42, FR-66), never off a
    second string.
    """
    settings = config.load()
    return (
        Rung(1, "CLI", "live", f"provider {provider}, harnessed run / eval run / memory"),
        Rung(2, "Coding agent", "not yet", "harnessed agents add"),
        doctor_cmd._rung_gateway(settings),
        doctor_cmd._rung_dashboard(settings),
    )


def describe() -> SeamLine:
    """The file mode line of ``doctor``'s platform section."""
    if POSIX:
        return SeamLine("file mode", "chmod 0600", None)
    return SeamLine("file mode", "skipped", MODE_SKIPPED)


def restrict(env_path: Path) -> str | None:
    """Make ``env_path`` owner-only on POSIX. Off POSIX, skip the call and return why."""
    if not POSIX:
        return MODE_SKIPPED
    os.chmod(env_path, 0o600)
    return None


def _existing(env_path: Path, example_path: Path | None) -> tuple[str, bool]:
    """The text to merge into, and whether ``.env`` already existed.

    A fresh clone has no ``.env`` and is seeded from ``.env.example``, so the comments that
    document every variable survive into the operator's own file (FR-26). An existing ``.env``
    is the base for its own merge and the example file is not consulted at all.
    """
    if env_path.exists():
        return env_path.read_bytes().decode("utf-8"), True
    if example_path is not None and example_path.exists():
        return example_path.read_bytes().decode("utf-8"), False
    return "", False


def configure_provider(
    provider: str,
    credential: str | None,
    *,
    validate: bool,
    env_path: Path,
    example_path: Path | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Prove ``credential`` for ``provider``, merge it into ``env_path``, and report what now works.

    Returns ``init --json``'s payload. Raises ``ConfigError`` for an unknown provider or a keyed
    provider given no credential, and ``ProviderError`` for a refusal - in every case before a
    byte is written. Prints nothing: rendering is the caller's.
    """
    settings = config.load()
    # Raises ConfigError naming the known providers before anything is read or written.
    key_var = settings.key_var_for(provider)
    value = None if key_var is None or credential is None else credential.strip()
    if key_var is not None and not value:
        raise ConfigError(f"no value was supplied for {key_var}. Nothing was written.")

    if validate:
        keys = {} if key_var is None or value is None else {key_var: value}
        providers.validate_credential(
            replace(settings, provider=provider, model=model or settings.model, _keys=keys),
            provider,
        )

    before, existed = _existing(env_path, example_path)
    updates = {"HARNESSED_PROVIDER": provider}
    if model is not None:
        updates["HARNESSED_MODEL"] = model
    if key_var is not None and value is not None:
        updates[key_var] = value
    after = config.merge_env_text(before, updates)

    unchanged = existed and after == before
    if not unchanged:
        env_path.write_bytes(after.encode("utf-8"))
        restrict(env_path)

    # The claim is computed from the state the *next* command will start from, which is why the
    # settings are resolved again here rather than reused: the ones above were built before the
    # write, and ``.env`` is now one of the places a key is read from. A provider needing no key
    # is live because there is nothing to supply.
    credential_live = key_var is None or config.load().api_key_for(provider) is not None
    return {
        "credential_live": credential_live,
        "credential_var": key_var,
        "env_file": env_path.name,
        "provider": provider,
        "rungs": [
            {"number": r.number, "name": r.name, "state": r.state, "detail": r.detail}
            for r in ladder(provider)
        ],
        "unchanged": unchanged,
    }
