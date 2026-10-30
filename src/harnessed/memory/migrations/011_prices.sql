-- 011_prices: the rates each provider publishes in its own catalog (ticket 132, FR-73).
--
-- Committed. Never edited.
--
-- Written by `harnessed prices refresh` and by nothing else (decision 53, MUST rule 28). Every
-- rate is a TEXT decimal string per million tokens as the provider published it: money is never a
-- float here, so there is not one floating-point column. `cached_input` is nullable because a
-- catalog may publish no cache rate, and a missing rate is unpriced rather than substituted.
-- `fetched_at` is the row's own: when this rate was read, never when it is shown.

CREATE TABLE prices (
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    input        TEXT NOT NULL,
    output       TEXT NOT NULL,
    cached_input TEXT,
    currency     TEXT NOT NULL,
    source_url   TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (provider, model)
);
