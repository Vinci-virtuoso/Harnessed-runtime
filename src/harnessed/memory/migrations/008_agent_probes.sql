-- 008_agent_probes: every `harnessed agents test` result, so doctor can read proof (ticket 089, FR-29).
--
-- Committed. Never edited.
--
-- Append-only: a probe is never updated or replayed, so a newer failure outlives an older pass.
-- `fingerprint` is taken over the entry's launch (command, args, cwd, resolved executable), so a
-- changed entry or a reinstalled program no longer matches the proof it once had. Kept apart from
-- `delegations` on purpose: that ledger answers a known task hash from its row instead of running.

CREATE TABLE agent_probes (
    probe_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent            TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    protocol_version INTEGER NOT NULL,
    detail           TEXT NOT NULL,
    tested_at        TEXT NOT NULL
);

CREATE INDEX agent_probes_by_agent ON agent_probes (agent, probe_id);
