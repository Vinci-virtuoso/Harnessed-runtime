-- 026_v6_debts: the permission fields and the skipped-fact count
-- (ticket 287, FR-167, FR-170, decisions 99, 100).
--
-- Committed. Never edited.
--
-- Columns only; tickets 288 and 289 write and serve them. Every column is nullable: a row
-- written before this migration reads null in each.

ALTER TABLE permission_decisions ADD COLUMN task_hash TEXT;
ALTER TABLE permission_decisions ADD COLUMN detail TEXT;
ALTER TABLE permission_decisions ADD COLUMN warning TEXT;
ALTER TABLE permission_decisions ADD COLUMN waiter_pid INTEGER;
ALTER TABLE permission_decisions ADD COLUMN waiter_started_at TEXT;
ALTER TABLE consolidation_batches ADD COLUMN facts_skipped INTEGER;
