-- 018_hop_results: how each hop's handler settled, on the hop's own `decisions` row (ticket 232,
-- FR-134, decision 73).
--
-- Committed. Never edited.
--
-- `decisions` rows are written before the handler starts (MUST rule 6, migration 014); the
-- begin-then-settle update in `cli/routed.py` fills these two columns when the handler returns or
-- raises. `result` is `succeeded` or `failed`; `result_reason` names why a hop failed. Both stay
-- NULL on every row recorded before this migration, which reads back as "recorded before
-- Harnessed recorded a hop's result" and is never guessed from the events.

ALTER TABLE decisions ADD COLUMN result TEXT CHECK (result IN ('succeeded', 'failed'));
ALTER TABLE decisions ADD COLUMN result_reason TEXT CHECK (
    result_reason IN (
        'timeout', 'wrote_files', 'error', 'quota_refused', 'dirty_tree', 'version_mismatch',
        'raised'
    )
);
