-- 024_fact_link_relation: a fact link says why it exists, and a batch records who holds it
-- (ticket 248, FR-150, FR-151, decision 81; operator ruling 2026-10-04).
--
-- Committed. Never edited.
--
-- `mem_fact_links` (005) was `(mem_id, to_id)` only. A contradicting fact archives the one it
-- overturns, and the link row carries `relation` (`contradicts` for now; NULL for a plain link
-- written before this) and `reason` (the batch id). SQLite cannot add a CHECK in place, so the
-- table is rebuilt as 022 was and its rows copied across unchanged.
--
-- `consolidation_batches` gains the pid and start time of the process running a batch, so a
-- second pass can tell a `running` row under a live process from one a crash left behind.

CREATE TABLE mem_fact_links_new (
    mem_id   TEXT NOT NULL REFERENCES mem_semantic (mem_id),
    to_id    TEXT NOT NULL REFERENCES mem_semantic (mem_id),
    relation TEXT CHECK (relation IS NULL OR relation IN ('contradicts')),
    reason   TEXT,
    PRIMARY KEY (mem_id, to_id)
);

INSERT INTO mem_fact_links_new (mem_id, to_id) SELECT mem_id, to_id FROM mem_fact_links;

DROP TABLE mem_fact_links;

ALTER TABLE mem_fact_links_new RENAME TO mem_fact_links;

CREATE INDEX idx_mem_fact_links_to ON mem_fact_links (to_id);

ALTER TABLE consolidation_batches ADD COLUMN holder_pid INTEGER;
ALTER TABLE consolidation_batches ADD COLUMN holder_started_at TEXT;
