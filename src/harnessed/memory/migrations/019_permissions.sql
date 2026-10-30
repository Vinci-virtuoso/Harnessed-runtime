-- 019_permissions: an agent's permission requests, and the one-run lease (ticket 234, FR-130,
-- decisions 74 and 78).
--
-- Committed. Never edited.
--
-- `permission_decisions` is the pending decision as its own table: one row per request, written
-- when the request arrives and answered once by a compare-and-set (`answer IS NULL`). `rule` names
-- why: `operator` (a person answered), `ceiling` (nobody did in time), `allowlist` or `deny_list`
-- (the configuration answered at once). `answered_via` is `console` or `cli` for an operator's
-- answer and NULL otherwise. The state a reader sees (pending, approved, denied, expired, allowed,
-- refused) is computed by `acp/pending.py` from these columns, never stored.
--
-- `run_lease` is the single row a run holds while it works (decision 78): at most one row, whose
-- `pid` and `started_at` identify the holder's process so a dead holder is never a live one.

CREATE TABLE permission_decisions (
    permission_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    tool_kind TEXT NOT NULL,
    description TEXT,
    requested_at TEXT NOT NULL,
    ceiling_at TEXT,
    answer TEXT CHECK (answer IN ('approve', 'deny')),
    rule TEXT CHECK (rule IN ('operator', 'ceiling', 'allowlist', 'deny_list')),
    answered_via TEXT CHECK (answered_via IN ('console', 'cli')),
    answered_at TEXT,
    CHECK ((answer IS NULL) = (rule IS NULL)),
    CHECK ((answer IS NULL) = (answered_at IS NULL))
);

CREATE INDEX permission_decisions_session ON permission_decisions (session_id, requested_at);

CREATE TABLE run_lease (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    session_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    started_at TEXT,
    acquired_at TEXT NOT NULL
);
