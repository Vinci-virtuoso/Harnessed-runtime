-- 002_memory: the three kinds of memory (FR-10).
--
-- Committed. Never edited.

-- Semantic memory: facts, searchable. mem_semantic_fts is a standalone FTS5 table rather than
-- an external-content one, so a write is two plain inserts in one transaction and there is no
-- trigger to keep in step with this file.
CREATE TABLE mem_semantic (
    mem_id     TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE mem_semantic_fts USING fts5 (mem_id UNINDEXED, text);

-- Episodic memory: what happened, per run. kind separates a loop transcript from a delegation
-- transcript; rowid gives the insertion order the reader replays.
CREATE TABLE mem_episodic (
    episode_id TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX mem_episodic_run_idx ON mem_episodic (run_id);

-- Procedural memory: every procedure version ever written, content-addressed and never
-- deleted, so a promotion can be rolled back and diffed (architecture decision 5).
CREATE TABLE procedures (
    proc_id    TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
