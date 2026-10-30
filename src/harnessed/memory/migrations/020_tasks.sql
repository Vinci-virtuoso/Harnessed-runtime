-- 020_tasks: the tasks ledger, its message join and the schedule tables (ticket 239, FR-140,
-- decisions 76 and 79).
--
-- Committed. Never edited.
--
-- `tasks` is one row per job the operator hands the harness: its scrubbed text, working directory,
-- budget and state. Every figure the store keeps at a checkpoint (`used_*`, `human_messages`,
-- `pauses`) is a column, never a sum. `session_id` is the join to a session: unique, never a column
-- on `events`. `task_messages` marks the messages the task composition wrote, keyed
-- `(run_id, message_seq)`; a message with no row was written by the operator.
--
-- `schedules` and `schedule_installs` are decision 79's; ticket 242 writes them, so they ship here
-- in build order and stay empty until then.

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    cwd TEXT NOT NULL,
    entry TEXT,
    allow_dirty INTEGER NOT NULL DEFAULT 0 CHECK (allow_dirty IN (0, 1)),
    budget_iterations INTEGER NOT NULL,
    budget_seconds INTEGER NOT NULL,
    budget_dollars TEXT NOT NULL,
    budget_nanos INTEGER NOT NULL,
    budget_source TEXT NOT NULL CHECK (budget_source IN ('default', 'given')),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'needs_human', 'done', 'failed', 'cancelled')
    ),
    stop_reason TEXT CHECK (
        stop_reason IN (
            'finished', 'budget', 'ceiling', 'dirty_tree', 'orphaned', 'raised', 'cancelled'
        )
    ),
    pause_reason TEXT CHECK (pause_reason IN ('permission', 'uncertain')),
    paused_at TEXT,
    pending_permission_id TEXT,
    paused_message_seq INTEGER,
    session_id TEXT UNIQUE,
    queued_via TEXT NOT NULL CHECK (queued_via IN ('cli', 'console')),
    started_by TEXT CHECK (started_by IN ('schedule', 'hand')),
    schedule_id TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    human_messages INTEGER NOT NULL DEFAULT 0,
    pauses INTEGER NOT NULL DEFAULT 0,
    used_iterations INTEGER NOT NULL DEFAULT 0,
    used_seconds TEXT NOT NULL DEFAULT '0.0',
    used_dollars TEXT NOT NULL DEFAULT '0.000000',
    used_nanos INTEGER NOT NULL DEFAULT 0,
    unpriced_turns INTEGER NOT NULL DEFAULT 0,
    last_message_seq INTEGER,
    pid INTEGER,
    pid_started_at TEXT
);

CREATE INDEX tasks_state_idx ON tasks (state, created_at DESC);
CREATE INDEX tasks_paused_idx ON tasks (paused_at) WHERE state = 'needs_human';

CREATE TABLE task_messages (
    run_id TEXT NOT NULL,
    message_seq INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('first', 'continuation')),
    PRIMARY KEY (run_id, message_seq)
);

CREATE INDEX task_messages_task_idx ON task_messages (task_id);

CREATE TABLE schedules (
    schedule_id TEXT PRIMARY KEY,
    rule_kind TEXT NOT NULL CHECK (rule_kind IN ('every', 'cron')),
    rule_value TEXT NOT NULL,
    rule_words TEXT NOT NULL,
    utc_offset_minutes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    next_due_at TEXT,
    removed_at TEXT
);

CREATE INDEX schedules_due_idx ON schedules (next_due_at) WHERE removed_at IS NULL;

CREATE TABLE schedule_installs (
    install_id INTEGER PRIMARY KEY AUTOINCREMENT,
    "where" TEXT NOT NULL CHECK ("where" IN ('task_scheduler', 'crontab')),
    entry_name TEXT NOT NULL,
    command TEXT NOT NULL,
    arguments TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    entry_sha256 TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    uninstalled_at TEXT
);
