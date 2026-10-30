-- 006_turn_usage: a turn's token usage, so a run's cost survives the process (ticket 085, FR-7).
--
-- Committed. Never edited.
--
-- Nullable with no default on purpose. A row written from 006 on always carries all three
-- counts; NULL means the turn was recorded before this migration, and it reads back as unknown
-- rather than as a zero that would quietly undercount a run's cost.

ALTER TABLE turns ADD COLUMN input_tokens INTEGER;
ALTER TABLE turns ADD COLUMN output_tokens INTEGER;
ALTER TABLE turns ADD COLUMN cached_input_tokens INTEGER;
