-- 012_run_messages: a run's message list, one row per message (ticket 138, FR-80, decision 50).
--
-- Committed. Never edited.
--
-- A session is one run continued turn by turn, and this is what it continues from. Written by
-- `loop/journal.py::Journal.record` in the same transaction as the turn that added the rows, and
-- read back by `loop/journal.py::messages_for`, the journal's one sanctioned reader. A table
-- rather than a JSON column on a run row, so a row is one message and a walk can count them.
-- `tool_calls` and `tool_results` hold the message form the model is handed back, and are NULL
-- on a message that carries none.

CREATE TABLE run_messages (
    run_id       TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    tool_calls   TEXT,
    tool_results TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, idx)
);
