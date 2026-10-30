/**
 * The DOM scan of FR-126 as the browser runs it: on a page as it stands, then with a figure's
 * drawer and every Details disclosure in it opened. Shares its predicate with the unit test
 * (`tests/support/endpoint-scan.ts`), so the two cannot drift.
 */
import type { Locator, Page } from "@playwright/test";

import { collectStrings, scanStrings } from "../support/endpoint-scan";

/** Every endpoint string drawn on the page now: text, titles and accessible names. */
export async function pageFindings(page: Page): Promise<string[]> {
  return scanStrings(await page.evaluate(`(${collectStrings.toString()})()`) as string[]);
}

/**
 * Open every collapsed disclosure in `drawer`. The locator is re-evaluated on each click and an
 * opened one stops matching, so the first match is clicked until none is left.
 */
export async function openDisclosures(drawer: Locator): Promise<void> {
  const collapsed = drawer.locator("button[aria-expanded='false']");
  for (let left = await collapsed.count(); left > 0; left = await collapsed.count()) {
    await collapsed.first().click();
  }
}

/** The same scan with the first figure's drawer and all its disclosures open. Closes the drawer. */
export async function drawerFindings(page: Page): Promise<string[]> {
  const chip = page.locator("button[data-figure]").first();
  if ((await chip.count()) === 0) return [];
  await chip.click();
  const drawer = page.getByRole("dialog");
  await openDisclosures(drawer);
  const found = await pageFindings(page);
  await page.keyboard.press("Escape");
  return found;
}
