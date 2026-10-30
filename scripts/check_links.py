"""Check the README's external links (ticket 325, FR-181). Run by the CI ``docs`` job only.

This is the one networked step in the project's checks; ``verify`` never calls it, because the
offline gate must stay offline. Standard library only.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
TIMEOUT_SECONDS = 20
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")

_LINK = re.compile(r"https?://[^\s)>\]\"'`]+")


def external_links(text: str) -> list[str]:
    """Every distinct http(s) link in ``text``, in order, loopback addresses left out."""
    seen: dict[str, None] = {}
    for match in _LINK.finditer(text):
        link = match.group(0).rstrip(".,;:")
        if not any(host in link for host in LOOPBACK):
            seen.setdefault(link)
    return list(seen)


def fetch_status(url: str) -> int:
    """The HTTP status of ``url``, trying HEAD first and GET where HEAD is refused."""
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method, headers={"User-Agent": "harnessed"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            if method == "GET" or error.code not in (403, 405, 501):
                return error.code
    raise AssertionError("unreachable")


def broken(links: list[str], fetch: Callable[[str], int] = fetch_status) -> list[tuple[str, str]]:
    """The links that did not answer below 400, each with its status or its error."""
    bad: list[tuple[str, str]] = []
    for link in links:
        try:
            status = fetch(link)
        except (OSError, ValueError) as error:
            bad.append((link, type(error).__name__))
            continue
        if status >= 400:
            bad.append((link, str(status)))
    return bad


def main() -> int:
    links = external_links(README.read_text(encoding="utf-8"))
    bad = broken(links)
    for link, why in bad:
        print(f"broken: {link} ({why})")
    print(f"{len(links) - len(bad)}/{len(links)} external links answered")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
