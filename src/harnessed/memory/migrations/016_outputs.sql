-- 016_outputs: the delegation output ledger - the agent's text result and its output files,
-- copied in and capped (ticket 181, FR-127, decisions 65, G-28, G-29).
--
-- Committed. Never edited.
--
-- One row per output, written by `acp/outputs.py::record_hop` as a hop settles. `output_id` is
-- `out_` plus 16 hex of a sha256 over `run_id`, `message_seq`, `hop` and `position`, so it is
-- reproducible from the fields that name it rather than random. `position` 0 is always the
-- agent's own text result; a file the entry's `outputs` glob matched and the delegation changed
-- is position 1 and up, at most 8. Copying keeps the gateway a read-only reader of one database:
-- a request never names a path, and `body` is the whole of what `GET /v1/outputs/{output_id}`
-- and its CLI twin serve, read straight off this row. Text bodies are scrubbed on write
-- (`state-and-idempotency.md`, MUST rule 7); binary bodies are not, and are never rendered as
-- text (a standing deviation, decision 65). `too_large` marks a file over `OUTPUT_CAP_BYTES`
-- with a NULL `body`, never a truncated one. `name` and `caption` are NULL for position 0, which
-- names no file.

CREATE TABLE outputs (
    output_id    TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    message_seq  INTEGER,
    hop          INTEGER NOT NULL,
    position     INTEGER NOT NULL,
    task_hash    TEXT,
    output_kind  TEXT NOT NULL,
    media_type   TEXT NOT NULL,
    name         TEXT,
    caption      TEXT,
    size_bytes   INTEGER NOT NULL,
    too_large    INTEGER NOT NULL,
    body         BLOB,
    created_at   TEXT NOT NULL
);

-- The messages read (ticket 182, decision 66) joins outputs onto each hop of a message.
CREATE INDEX outputs_hop_idx ON outputs (run_id, message_seq, hop);
