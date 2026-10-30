-- 004_flywheel: candidates, the promotion record and the active set (FR-17, FR-20).
--
-- Committed. Never edited.

-- A candidate procedure the reflector wrote, with the case ids it came from. Content-addressed
-- by cand_id, so the same text from the same failures is the same candidate.
CREATE TABLE candidates (
    cand_id           TEXT PRIMARY KEY,
    text              TEXT NOT NULL,
    source_case_ids   TEXT NOT NULL,
    provenance        TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- The decision record. Both scores and the regression count are stored, not recomputed, so a
-- past promotion can be audited without re-running the suite.
CREATE TABLE promotions (
    cand_id        TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    promoted       INTEGER NOT NULL,
    score_before   REAL NOT NULL,
    score_after    REAL NOT NULL,
    passes_before  INTEGER NOT NULL,
    passes_after   INTEGER NOT NULL,
    regressions    INTEGER NOT NULL,
    soft_drops     INTEGER NOT NULL,
    reason         TEXT NOT NULL,
    PRIMARY KEY (cand_id, decided_at),
    FOREIGN KEY (cand_id) REFERENCES candidates (cand_id)
);

-- The current active set. Rows are removed on a rollback; the procedures themselves never are.
CREATE TABLE active_procedures (
    proc_id      TEXT PRIMARY KEY,
    activated_at TEXT NOT NULL,
    cand_id      TEXT,
    FOREIGN KEY (proc_id) REFERENCES procedures (proc_id)
);
