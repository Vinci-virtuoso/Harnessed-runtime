# Recorded ACP transcripts (ticket 171, FR-112)

Real delegations recorded by the operator on 2026-09-13 from `.harnessed/acp/` (the file
`acp/delegate.py::_write_transcript` writes), supplied in `.spec/operator-inputs/171/` and copied
here on 2026-09-27. Reviewed code, never generated (MUST rule 8): a re-record replaces a file
whole, from a new real delegation.

| File | Agent | `usage_update` frames | Cost |
|---|---|---|---|
| `reports-cost.json` | Claude Code (`billing = "oauth"`) | 4, the last with `cost {amount, currency}` | API-equivalent, `0.063306 USD` |
| `not-metered.json` | Grok (`billing = "oauth"`) | 0 | not metered |

The files are named by what they show, not by agent, so `tests/test_agent_turns.py` never names a
specific agent CLI (`test_acp.py`'s scan).

**Scrubbed:** every home-folder path (`C:\Users\<operator>`) in the Grok transcript's
`availableCommands` metadata was replaced with `<home>`; nothing else was changed. Both files were
re-serialised with sorted keys, compact separators and a trailing newline, the same shape
`_write_transcript` uses. Hand-reviewed for keys, bearer tokens, email addresses and home paths:
none remain.

## Ticket 246 (FR-147): the isolation, the approval and a repository delegation

Recorded by the operator on 2026-10-04 (`record.ps1`), supplied in `.spec/operator-inputs/246/`, copied
verbatim except where noted. Never generated (MUST rule 8).

| File | Shows |
|---|---|
| `pre-approved-folder-transcript.json` | the live isolation run: a folder pre-approving `Bash(*)`, the agent still has no shell tool and runs nothing |
| `approved-execute-transcript.json` | one `execute` permission request, answered `allow-once` |
| `approved-execute-permission.json` | the console's record of that request: `approve`, rule `operator`, `answered_via: console` |
| `repository-delegation-messages.json` | a task delegated into a scratch repository; the hop carries a `repository` record |

**Scrubbed:** in `pre-approved-folder-transcript.json` the operator's home-folder name was replaced with
`<operator>`. **Recorded as they happened:** the approval was sent through the console's own route by the
script (the console showed no Approve card); the repository record reads `changed_count: "0"` with an
empty diff stat (the edit was left uncommitted on a clean head).
