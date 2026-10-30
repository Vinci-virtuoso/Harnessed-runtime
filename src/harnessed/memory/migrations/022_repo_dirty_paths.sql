-- 022_repo_dirty_paths: a refused delegation keeps the paths that made the tree dirty (ticket 245,
-- FR-146, decision 80).
--
-- Committed. Never edited.
--
-- `repo_paths.kind` was `changed` and nothing else (migration 021). A delegation refused for a
-- dirty working tree records the paths `git status --porcelain` printed under `kind = 'dirty'`,
-- in git's order. SQLite cannot alter a CHECK, so the table is rebuilt with the same columns and
-- the wider check, and its rows copied across unchanged.

CREATE TABLE repo_paths_new (
    task_hash TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    position INTEGER NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('changed', 'dirty')),
    PRIMARY KEY (task_hash, attempt, position)
);

INSERT INTO repo_paths_new (task_hash, attempt, position, path, kind)
    SELECT task_hash, attempt, position, path, kind FROM repo_paths;

DROP TABLE repo_paths;

ALTER TABLE repo_paths_new RENAME TO repo_paths;
