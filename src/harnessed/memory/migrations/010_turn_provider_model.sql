-- 010_turn_provider_model: which provider and which model produced a turn and an eval run (ticket 131, FR-72).
--
-- Committed. Never edited.
--
-- Tokens cannot be priced without knowing the model, so this lands before prices. Nullable with
-- no default on purpose: a row written from 010 on always carries both; NULL means the row was
-- recorded before this migration, and a reader renders it `unknown` rather than guessing a
-- default provider that would put a false name on an old score.

ALTER TABLE turns ADD COLUMN provider TEXT;
ALTER TABLE turns ADD COLUMN model TEXT;
ALTER TABLE eval_runs ADD COLUMN provider TEXT;
ALTER TABLE eval_runs ADD COLUMN model TEXT;
