-- Redacted model evidence and candidate rejection reasons. Historical batches have no row.
CREATE TABLE consolidation_diagnostics (
    batch_id TEXT PRIMARY KEY REFERENCES consolidation_batches(batch_id),
    payload TEXT NOT NULL
);
