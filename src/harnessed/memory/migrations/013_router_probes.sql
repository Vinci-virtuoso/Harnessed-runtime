-- 013_router_probes: what `harnessed doctor --live` proved about the pinned router model
-- (ticket 156, FR-97, decision 57's live rung).
--
-- Committed. Never edited.
--
-- One row per invocation of the live rung, passing or failed, never replayed or updated -
-- `agent_probes` (008) is the same append-only shape for the same reason: a newer failure must
-- not be shadowed by an older pass. `latency_seconds` and `cost` are TEXT, the exact text this
-- ticket's transport reports, never rounded and never a REAL column (decision 57's cost
-- discipline, restated for the probe's own timing).

CREATE TABLE router_probes (
    probe_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    model           TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    latency_seconds TEXT NOT NULL,
    cost            TEXT,
    detail          TEXT NOT NULL,
    probed_at       TEXT NOT NULL
);
