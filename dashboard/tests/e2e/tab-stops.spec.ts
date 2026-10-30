/**
 * Traces is one tab stop per row (FR-174, X6; decision 103). With 100 task rows and 100 session
 * rows seeded, Tab is pressed through the page and the stops are counted: the page's own controls
 * plus one per row, and an extra stop fails.
 */
import { writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

import { DASHBOARD_URL, evidencePath } from "./harness";

const TICKET = "301";
const ROWS = 100;
const ROW = "[data-testid='task-row'], tbody tr";

test("Traces offers one Tab stop per row, and Enter on it opens its drawer", async ({ page }) => {
  await page.goto(`${DASHBOARD_URL}/traces?limit=${ROWS}`);
  await expect(page.getByTestId("task-list").getByTestId("task-row")).toHaveCount(ROWS);
  await expect(page.locator("tbody tr").first()).toBeVisible();

  const sessionRows = await page.locator("main table:has(caption:text-is('Sessions, in the order the gateway sent them')) tbody tr").count();
  expect(sessionRows).toBeGreaterThanOrEqual(ROWS);

  // Everything the browser would Tab to, from the DOM.
  const tabbable = await page.evaluate((rowSelector) => {
    const sel = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]";
    // A radio group is one stop whatever its size, so only its first member counts.
    const groups = new Set<string>();
    const all = [...document.querySelectorAll<HTMLElement>(sel)].filter((el) => {
      if (el.tabIndex < 0 || el.getClientRects().length === 0) return false;
      if (el instanceof HTMLInputElement && el.type === "radio") {
        if (groups.has(el.name)) return false;
        groups.add(el.name);
      }
      return true;
    });
    const inRows = all.filter((el) => el.closest(rowSelector) !== null);
    const perRow = new Map<Element, number>();
    for (const el of inRows) {
      const row = el.closest(rowSelector)!;
      perRow.set(row, (perRow.get(row) ?? 0) + 1);
    }
    return {
      total: all.length,
      outside: all.length - inRows.length,
      rows: document.querySelectorAll(rowSelector).length,
      worst: Math.max(0, ...perRow.values()),
    };
  }, ROW);
  expect(tabbable.worst, "no row offers a second stop").toBeLessThanOrEqual(1);
  expect(tabbable.total).toBe(tabbable.outside + tabbable.rows);

  // And Tab itself reaches exactly that many, then wraps.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  const seen = new Set<string>();
  let count = 0;
  for (let press = 0; press < tabbable.total + 5; press += 1) {
    await page.keyboard.press("Tab");
    const id = await page.evaluate((n) => {
      const el = document.activeElement as HTMLElement | null;
      if (el === null || el === document.body) return "(none)";
      if (!el.dataset.walk) el.dataset.walk = `s${n}`;
      return el.dataset.walk;
    }, press);
    if (id === "(none)" || seen.has(id)) break;
    seen.add(id);
    count += 1;
  }
  expect(count, "Tab reaches every control once, and no row's figures").toBeLessThanOrEqual(tabbable.total + 1);
  expect(count).toBeGreaterThanOrEqual(tabbable.total);
  writeFileSync(
    evidencePath(TICKET, "tab-count.txt"),
    `rows ${tabbable.rows}; controls outside rows ${tabbable.outside}; tabbable ${tabbable.total}; tab presses reaching a new stop ${count}\n`,
    "utf8",
  );
  await page.screenshot({ path: evidencePath(TICKET, "traces.png") });

  // A row's stop opens its drawer on Enter, and the figures are reachable there.
  const stop = page.getByTestId("task-row").first().locator("button[data-figure]").first();
  await stop.focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByRole("dialog");
  await expect(drawer).toBeVisible();
  await expect(drawer.locator("button, a[href]").first()).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
});
