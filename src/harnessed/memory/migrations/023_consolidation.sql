-- 023_consolidation: a batch of episodes distilled into facts, and which episodes it consumed
-- (ticket 247, FR-148, decision 81).
--
-- Committed. Never edited.
--
-- `consolidation_batches` is one row per batch, keyed by `batch_id` (`bat_` plus the first 16 hex
-- of a sha256 over the sorted episode ids, so the same episodes always make the same id).
-- `status` is `running`, `done` or `failed`; `failure` names why a failed batch failed and is
-- NULL otherwise. 0 REAL columns: `cost` is the figure as the provider reported it, TEXT, and
-- `cost_nanos` is the one integer column a sum may use.
--
-- `mem_consolidated` is one row per episode a done batch consumed: an episode with no row here is
-- unconsolidated. `mem_semantic_source_idx` lets a batch's facts be read by their exact source
-- (`consolidation:<batch_id>`), never by full-text search.

CREATE TABLE consolidation_batches (
    batch_id        TEXT PRIMARY KEY,
    session_id      TEXT,
    status          TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    failure         TEXT CHECK (failure IS NULL OR failure IN ('provider_error', 'timeout', 'unparseable')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    episode_count   INTEGER NOT NULL,
    facts_written   INTEGER NOT NULL DEFAULT 0,
    facts_archived  INTEGER NOT NULL DEFAULT 0,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost            TEXT,
    cost_nanos      INTEGER,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

CREATE TABLE mem_consolidated (
    episode_id      TEXT PRIMARY KEY REFERENCES mem_episodic (episode_id),
    batch_id        TEXT NOT NULL REFERENCES consolidation_batches (batch_id),
    consolidated_at TEXT NOT NULL
);

CREATE INDEX mem_semantic_source_idx ON mem_semantic (source);
