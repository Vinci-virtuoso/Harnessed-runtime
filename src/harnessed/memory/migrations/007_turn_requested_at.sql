-- 007_turn_requested_at: when a turn's request was sent, so a run's duration is true (ticket 087, FR-7).
--
-- Committed. Never edited.
--
-- `created_at` is stamped when the turn is recorded, after the reply arrives, so a run read from
-- it alone started at its first reply and a one-turn run took no time. Nullable with no default
-- on purpose: NULL means the turn was recorded before this migration, and a reader falls back to
-- that turn's `created_at` rather than inventing an instant.

ALTER TABLE turns ADD COLUMN requested_at TEXT;
