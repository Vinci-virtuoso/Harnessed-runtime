/**
 * FR-86 (ticket 144), in a real browser against the seeded process: the diagram's box labels are
 * the Overview tiles' own `Leaf`s, each rendered through `Value` with its source, each matching its
 * tile character for character, and a label's chip opens the drawer over the tile's response.
 *
 * The unit half (`tests/unit/diagram-labels.test.tsx`) proves the same against the recorded
 * corpus in jsdom and renders no pixels; this spec is where the labels are seen, and it writes the
 * screenshots into `evidence/144/`.
 */
import { expect, test } from "@playwright/test";

import { DASHBOARD_URL, evidencePath } from "./harness";

const TICKET = "144";

/** Each label: its region's group, and the tile's route and path it was lent from. */
const LABELS = [
  { key: "turns", region: "Loop", route: "/v1/runs/totals", path: ".turns" },
  { key: "facts active", region: "Memory", route: "/v1/memory/totals", path: ".facts_active" },
  { key: "skip", region: "Memory", route: "/v1/events/gate", path: ".skip" },
  { key: "retrieve", region: "Memory", route: "/v1/events/gate", path: ".retrieve" },
  { key: "failed open", region: "Memory", route: "/v1/events/gate", path: ".failed_open" },
] as const;

test("the diagram's labels are the tiles' own figures, and open the tiles' drawers", async ({ page }) => {
  const response = await page.goto(`${DASHBOARD_URL}/`);
  expect(response?.status()).toBe(200);

  const diagram = page.getByTestId("harness-diagram");
  await expect(diagram.locator("[data-figure]")).toHaveCount(LABELS.length, { timeout: 30_000 });

  for (const label of LABELS) {
    const figure = `[data-figure][data-route="${label.route}"][data-path="${label.path}"]`;
    const onBox = diagram.getByRole("group", { name: label.region }).locator(figure);
    const tile = page.locator(`section[aria-labelledby] ${figure}`);
    await expect(onBox, label.key).toHaveCount(1);
    await expect(tile, label.key).toHaveCount(1);
    expect(await onBox.textContent(), label.key).toBe(await tile.textContent());
  }

  await diagram.scrollIntoViewIfNeeded();
  await diagram.screenshot({ path: evidencePath(TICKET, "diagram-labels.png"), animations: "disabled" });

  const turns = diagram.locator('[data-figure][data-path=".turns"]');
  await turns.click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.getByRole("heading", { name: "Where this came from" })).toBeVisible();
  await page.screenshot({ path: evidencePath(TICKET, "label-drawer.png"), animations: "disabled" });
});
