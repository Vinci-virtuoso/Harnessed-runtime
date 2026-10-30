/**
 * Ticket 133 (FR-74): `/v1/spend` serves `total: null` for two different causes, and the tile says
 * which one. The sentences are `ux/design.md`'s, verbatim; the choice is made on the served
 * `prices_fetched_at` leaf's kind, never on a figure read out of it.
 */

import { describe, expect, it } from "vitest";

import { nullReasonFor, spendTotalReason } from "../../lib/null-reasons";
import { parseWire } from "../../lib/wire";

const SPEND = "/v1/spend";
const NO_PRICES = "no prices fetched: run `harnessed prices refresh`";
const NONE_PRICED = "no turn could be priced";

function served(pricesFetchedAt: string | null) {
  const body = JSON.stringify({
    total: null,
    currency: "USD",
    priced_turns: "0",
    unknown_usage_turns: "0",
    unknown_model_turns: "0",
    unpriced_turns: "1",
    unpriced_models: ["openrouter/vendor/unlisted"],
    prices_fetched_at: pricesFetchedAt,
  });
  return parseWire(body, SPEND);
}

describe("spend.total's null reason", () => {
  it("names the refresh when the prices table is empty", () => {
    const wire = served(null);
    expect(wire.at(".total").kind).toBe("null");
    expect(spendTotalReason(wire.at(".prices_fetched_at"))).toBe(NO_PRICES);
  });

  it("says no turn could be priced when prices exist", () => {
    const wire = served("2026-09-23T12:00:00Z");
    expect(spendTotalReason(wire.at(".prices_fetched_at"))).toBe(NONE_PRICED);
  });

  it("keys both sentences on the route and path, query string ignored", () => {
    expect(nullReasonFor(SPEND, ".total")).toBe(NONE_PRICED);
    expect(nullReasonFor(`${SPEND}?session=run_x`, ".prices_fetched_at")).toBe(NO_PRICES);
  });
});
