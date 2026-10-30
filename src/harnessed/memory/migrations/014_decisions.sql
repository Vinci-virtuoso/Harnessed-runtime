-- 014_decisions: one row per routed decision, written before its handler starts (ticket 161,
-- FR-108, decision 61).
--
-- Committed. Never edited.
--
-- `decisions` is keyed (run_id, idx): `idx` counts every decision this run has ever written,
-- across every message and every chain hop (a hop's own ticket is 163+), so a later hop's row
-- never collides with the message that started it. `hop = 0` is the message's own decision;
-- `task` is only ever populated there. Per-option probabilities are rows, not a JSON column, in
-- `decision_options` (research.md; no new standing deviation over decision 47's rounding rule).
-- `turns.message_seq` is nullable because every turn recorded before this migration has none; a
-- turn recorded from here on always carries it, through a routed message or not.
--
-- 0 REAL columns: a probability, a confidence, a floor, the threshold and a cost are all TEXT,
-- rounded once in `router/decisions.py` and never recomputed (MUST rule 29 extended, decision 61).
-- `cost_nanos` is the one integer column a sum may use.

CREATE TABLE decisions (
    run_id               TEXT NOT NULL,
    idx                  INTEGER NOT NULL,
    message_seq          INTEGER NOT NULL,
    hop                  INTEGER NOT NULL,
    task                 TEXT,
    outcome              TEXT NOT NULL CHECK (outcome IN ('routed', 'uncertain', 'fallback')),
    route                TEXT,
    handler              TEXT CHECK (handler IN ('groq', 'agent', 'human')),
    confidence           TEXT,
    confidence_percent   TEXT,
    floor                TEXT,
    floor_percent        TEXT,
    needs_memory         TEXT,
    needs_memory_percent TEXT,
    memory_kind          TEXT,
    stop_reason          TEXT CHECK (stop_reason IN ('needs_human', 'below_floor')),
    fallback_cause       TEXT CHECK (fallback_cause IN ('unreachable', 'timeout', 'refused')),
    state_truncated      INTEGER NOT NULL,
    latency_seconds      TEXT NOT NULL,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cost                 TEXT,
    cost_nanos           INTEGER,
    model                TEXT NOT NULL,
    decision_id          TEXT,
    task_hash            TEXT,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (run_id, idx)
);

CREATE TABLE decision_options (
    run_id              TEXT NOT NULL,
    idx                 INTEGER NOT NULL,
    question            TEXT NOT NULL,
    position            INTEGER NOT NULL,
    option              TEXT NOT NULL,
    description         TEXT,
    probability         TEXT NOT NULL,
    probability_percent TEXT NOT NULL,
    PRIMARY KEY (run_id, idx, question, option)
);

CREATE INDEX decisions_recent_idx ON decisions (created_at DESC, run_id DESC) WHERE hop = 0;

ALTER TABLE turns ADD COLUMN message_seq INTEGER;
