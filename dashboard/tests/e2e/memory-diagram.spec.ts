/**
 * FR-85, FR-128 (ticket 209), in a real browser against the seeded process: `/memory` draws the
 * memory architecture above its figures, its labels are the figures row's own figures, and the
 * screenshot lands in `evidence/209/memory-diagram.png`. The unit half
 * (`tests/unit/memory-diagram.test.tsx`) proves the structure, the lighting and the drift in jsdom.
 */
import { expect, test } from "@playwright/test";

import { DASHBOARD_URL, evidencePath } from "./harness";

const TICKET = "209";

const LABELS = [
  { route: "/v1/events/gate", path: ".skip" },
  { route: "/v1/events/gate", path: ".retrieve" },
  { route: "/v1/events/gate", path: ".failed_open" },
  { route: "/v1/memory/totals", path: ".facts_active" },
  { route: "/v1/memory/totals", path: ".facts_archived" },
  { route: "/v1/memory/totals", path: ".episodes" },
  { route: "/v1/memory/totals", path: ".procedures_active" },
] as const;

test("the Memory page draws the memory architecture, labelled with its own figures", async ({ page }) => {
  const response = await page.goto(`${DASHBOARD_URL}/memory`);
  expect(response?.status()).toBe(200);

  const diagram = page.getByTestId("memory-diagram");
  await expect(diagram.locator("[data-figure]")).toHaveCount(LABELS.length, { timeout: 30_000 });
  await expect(diagram.getByRole("img", { name: /^Memory architecture/ })).toBeVisible();

  for (const label of LABELS) {
    const figure = `[data-figure][data-route="${label.route}"][data-path="${label.path}"]`;
    const onBox = diagram.locator(figure);
    const tile = page.locator(`section[aria-labelledby] ${figure}`);
    await expect(onBox, label.path).toHaveCount(1);
    await expect(tile, label.path).toHaveCount(1);
    expect(await onBox.textContent(), label.path).toBe(await tile.textContent());
  }

  await expect(diagram.locator('[data-node="identity"]').locator("a, [data-figure]")).toHaveCount(0);
  await expect(diagram.getByRole("status")).toBeVisible();

  await diagram.scrollIntoViewIfNeeded();
  await diagram.screenshot({ path: evidencePath(TICKET, "memory-diagram.png"), animations: "disabled" });
});
