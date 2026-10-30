-- 017_eval_retries: every retry a live eval run made after a dropped connection (ticket 231,
-- FR-160, decision 72).
--
-- Committed. Never edited.
--
-- One row per retry, written through `on_retry` by the retrying provider and decider that
-- `cli/eval_cmd.py` wraps around a live `eval run` alone. `call` is `chat` or `decision`;
-- `attempt` is the retry's number (1 or 2), `backoff_s` the wait that preceded it, and
-- `error_class` the name of the drop that caused it. A case that still failed after its retries
-- has no `eval_results` row, so these rows are also how a run says what it spent getting there.

CREATE TABLE eval_retries (
    eval_run_id  TEXT NOT NULL,
    case_id      TEXT NOT NULL,
    call         TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    error_class  TEXT NOT NULL,
    backoff_s    REAL NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (eval_run_id) REFERENCES eval_runs (eval_run_id)
);

CREATE INDEX eval_retries_run_idx ON eval_retries (eval_run_id, case_id);
