-- 015_agent_turns: one row per agent turn, a sibling of `turns` (ticket 171, FR-112, decisions 63
-- and 64).
--
-- Committed. Never edited.
--
-- `agent_turns` is keyed (run_id, idx) and owned by `acp/turns.py`. It is never rows in `turns`:
-- `turns` is keyed on the loop's iteration and replayed by `journal.recorded()`, so an agent turn
-- there would replay as a loop turn, move `last_iteration` and be rate-priced by spend's `prices`
-- join. `source` says where a row came from: an ACP transcript (`acp`), the metering proxy
-- (`proxy`) or a `cli` run. `message_seq` is nullable because a delegation the
-- `delegate_to_agent` tool starts has no routed message behind it.
--
-- 0 REAL columns: `cost` is the figure as the agent reported it, TEXT, never recomputed; `cost_nanos`
-- is the one integer column a sum may use. `cost_kind` is `reported` (an `openrouter` bill) or
-- `api_equivalent` (a subscription agent's `usage_update.cost`), and null when the turn is not
-- metered - never a zero.
--
-- Decision 64's three covering indexes ride along: spend sums agent costs by entry and kind,
-- router costs by `cost_nanos`, and loop turns grouped by provider and model.

CREATE TABLE agent_turns (
    run_id              TEXT NOT NULL,
    idx                 INTEGER NOT NULL,
    message_seq         INTEGER,
    hop                 INTEGER NOT NULL,
    task_hash           TEXT,
    entry               TEXT NOT NULL,
    billing             TEXT,
    source              TEXT NOT NULL CHECK (source IN ('acp', 'proxy', 'cli')),
    model               TEXT,
    request             TEXT,
    response            TEXT NOT NULL,
    tool_calls          TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cached_input_tokens INTEGER,
    reasoning_tokens    INTEGER,
    cost                TEXT,
    cost_nanos          INTEGER,
    currency            TEXT,
    cost_kind           TEXT CHECK (cost_kind IN ('reported', 'api_equivalent')),
    generation_id       TEXT,
    complete            INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, idx)
);

CREATE INDEX agent_turns_cost_idx ON agent_turns (entry, billing, cost_kind, cost_nanos);

CREATE INDEX decisions_cost_idx ON decisions (cost_nanos);

CREATE INDEX turns_spend_idx ON turns (provider, model, input_tokens, output_tokens, cached_input_tokens);
