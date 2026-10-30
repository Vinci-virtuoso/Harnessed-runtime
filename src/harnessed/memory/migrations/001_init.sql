-- 001_init: the migration ledger and the event log.
--
-- Committed. Never edited: the checksum recorded in schema_migrations is the enforcement
-- (state-and-idempotency.md). A change to the schema is a new numbered file.
--
-- The connection pragmas (journal_mode=WAL, synchronous=NORMAL, foreign_keys=ON,
-- busy_timeout=5000) are applied by memory/db.py::connect() rather than here, because
-- journal_mode cannot be changed inside a transaction and every migration body runs inside
-- one. WAL is persistent in the file header, so the first connect() settles it for good.

CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- FR-21. One row per published event, payload already redacted by the bus. (run_id, seq) is
-- the ordering the sequence assertion reads, and v2's SSE contract replays it.
CREATE TABLE events (
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    name       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE INDEX events_name_idx ON events (name);
