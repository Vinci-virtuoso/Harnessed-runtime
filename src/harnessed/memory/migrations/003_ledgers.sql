-- 003_ledgers: the four ledgers that make a run resumable (FR-7, FR-16, FR-18, FR-23).
--
-- Committed. Never edited. Every one of these is read with the read-then-write-before-the-next-
-- step pattern in state-and-idempotency.md; the primary key is the done-state key.

-- Run journal: one row per model turn.
CREATE TABLE turns (
    run_id       TEXT NOT NULL,
    iteration    INTEGER NOT NULL,
    request      TEXT NOT NULL,
    response     TEXT NOT NULL,
    stop_reason  TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, iteration)
);

-- Run journal: one row per tool outcome. Four outcomes, one shape, so parse_error is a column
-- and not a magic key inside the arguments.
CREATE TABLE tool_results (
    run_id       TEXT NOT NULL,
    iteration    INTEGER NOT NULL,
    call_id      TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    arguments    TEXT NOT NULL,
    output       TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    parse_error  TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, iteration, call_id),
    FOREIGN KEY (run_id, iteration) REFERENCES turns (run_id, iteration)
);

-- One row per suite execution.
CREATE TABLE eval_runs (
    eval_run_id  TEXT PRIMARY KEY,
    suite        TEXT NOT NULL,
    label        TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);

-- One row per case attempt. The key is what stops a resumed run from re-spending tokens.
CREATE TABLE eval_results (
    eval_run_id  TEXT NOT NULL,
    case_id      TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    score        REAL NOT NULL,
    passed       INTEGER NOT NULL,
    detail       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (eval_run_id, case_id, attempt),
    FOREIGN KEY (eval_run_id) REFERENCES eval_runs (eval_run_id)
);

-- One row per flywheel cycle.
CREATE TABLE improvement_runs (
    improvement_run_id TEXT PRIMARY KEY,
    suite              TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT
);

-- Flywheel done-state plus the payload the step produced.
CREATE TABLE improvement_steps (
    improvement_run_id TEXT NOT NULL,
    step               TEXT NOT NULL,
    key                TEXT NOT NULL,
    payload            TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (improvement_run_id, step, key),
    FOREIGN KEY (improvement_run_id) REFERENCES improvement_runs (improvement_run_id)
);

-- Delegation done-state. The row is written before the spawn with status 'attempting' and
-- updated afterwards, so a crash during a spawn still leaves the attempt recorded (FR-23).
CREATE TABLE delegations (
    task_hash    TEXT PRIMARY KEY,
    agent        TEXT NOT NULL,
    cwd          TEXT NOT NULL,
    task         TEXT NOT NULL,
    acceptance   TEXT NOT NULL,
    status       TEXT NOT NULL,
    outcome      TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);
