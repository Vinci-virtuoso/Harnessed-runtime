-- 005_fact_metadata: a fact's status, its tags and its links (ticket 047, FR-10, FR-11).
--
-- Committed. Never edited.
--
-- The operator's own knowledge vault gives every note a status, tags and links, and this store
-- gave a fact none of them. Each absence had a cost that ticket 041 made concrete by putting
-- retrieval on the run path: a fact that becomes false was retrievable forever and paid budget
-- to enter every future prompt, and keyword search could only ever find a fact that shared
-- words with the task.
--
-- Additive on purpose. `status` takes a default so every existing row reads back as active
-- without a rewrite, and the two new tables start empty, so applying this migration to a
-- populated database cannot lose or change a fact.

ALTER TABLE mem_semantic ADD COLUMN status TEXT NOT NULL DEFAULT 'active';

-- Archived facts are filtered on read rather than removed from the FTS index. The index is a
-- standalone FTS5 table, not an external-content one, so a write is two plain inserts in one
-- transaction with no trigger keeping them in step - deleting from one side here is exactly how
-- the two would drift apart.
CREATE INDEX idx_mem_semantic_status ON mem_semantic (status);

-- One row per tag, not a JSON column. The point of a tag is to narrow the candidate set before
-- the token ceiling has to decide anything, and a filter that has to parse a text column is a
-- filter nobody writes.
CREATE TABLE mem_fact_tags (
    mem_id  TEXT NOT NULL REFERENCES mem_semantic (mem_id),
    tag     TEXT NOT NULL,
    PRIMARY KEY (mem_id, tag)
);

CREATE INDEX idx_mem_fact_tags_tag ON mem_fact_tags (tag);

-- One row per link, directed, both ends real. A link to a fact that was never written is a
-- dangling wiki-link, which is harmless in a vault and is a retrieval bug here: expansion would
-- follow it and find nothing, and nothing is indistinguishable from a fact that did not match.
CREATE TABLE mem_fact_links (
    mem_id   TEXT NOT NULL REFERENCES mem_semantic (mem_id),
    to_id    TEXT NOT NULL REFERENCES mem_semantic (mem_id),
    PRIMARY KEY (mem_id, to_id)
);

CREATE INDEX idx_mem_fact_links_to ON mem_fact_links (to_id);
