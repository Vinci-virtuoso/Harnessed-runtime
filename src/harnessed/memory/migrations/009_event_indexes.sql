-- 009_event_indexes: two partial indexes over `events`, decision 47's answer to a measurement
-- (ticket 128, FR-69).
--
-- Committed. Never edited.
--
-- Measured 2026-09-22 over 500,000 events holding 50,000 gate decisions: the gate split read
-- 208.2 ms without an index, over the 200 ms NFR, and 64.1 ms through `events_gate_idx`; the
-- already shipped session listing (`seq = 1`, newest first) read 163.1 ms and 8.8 ms through
-- `events_first_idx`. No column is touched: the primary key `(run_id, seq)` stays the only order
-- the replay walks, and the five-column write in events/bus.py is unchanged. The first v4
-- migration by build order (decision 9): 010, 011 and 012 follow.
--
-- Both are partial, so each costs only the rows it serves. The gate index names the event as a
-- literal, and so must the query that wants it: SQLite proves a partial index applies from the
-- query text, and a bound parameter proves nothing (events/read.py::_SELECT_GATE_SPLIT).

CREATE INDEX events_first_idx ON events (created_at DESC, run_id DESC) WHERE seq = 1;
CREATE INDEX events_gate_idx  ON events (name, payload) WHERE name = 'retrieval.gate.decided';
