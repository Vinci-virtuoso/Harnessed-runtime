-- 021_repo_records: what a repository became under a delegation (ticket 244, FR-145, decision 80).
--
-- Committed. Never edited.
--
-- `repo_records` is one row per delegation attempt whose working directory is inside a git work
-- tree, keyed `(task_hash, attempt)`: the head before and after (full and git's own short form),
-- the branch, git's own diff stat verbatim, `changed_count` (the count of the path rows the store
-- wrote, never a figure read back from git) and whether the tree was already dirty at the start.
-- A delegation outside a repository, or with no `git` on PATH, has no row at all.
--
-- `repo_paths` is one row per changed path as git printed it (forward slashes on every host), in
-- git's order. `kind` is `changed` and nothing else yet.

CREATE TABLE repo_records (
    task_hash TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    head_before TEXT NOT NULL,
    head_after TEXT NOT NULL,
    head_before_short TEXT NOT NULL,
    head_after_short TEXT NOT NULL,
    branch TEXT NOT NULL,
    diff_stat TEXT NOT NULL,
    changed_count INTEGER NOT NULL,
    started_dirty INTEGER NOT NULL CHECK (started_dirty IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_hash, attempt)
);

CREATE TABLE repo_paths (
    task_hash TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    position INTEGER NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('changed')),
    PRIMARY KEY (task_hash, attempt, position)
);
