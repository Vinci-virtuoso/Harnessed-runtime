-- 025_permission_exchange: a permission request names the exchange it belongs to
-- (ticket 257, FR-130, FR-154, decision 74).
--
-- Committed. Never edited.
--
-- `message_seq` and `hop` are the routed message and chain hop the delegation answered when the
-- request was recorded. Both are nullable: a request made outside a routed message, and every
-- row written before this migration, reads null in both.

ALTER TABLE permission_decisions ADD COLUMN message_seq INTEGER;
ALTER TABLE permission_decisions ADD COLUMN hop INTEGER;
