/**
 * Ticket 202 end to end: the Models page's Prices panel is searched by model name. `/v1/prices`
 * is answered from the recorded body (a stub, not a ledger write: the e2e seed stores no prices,
 * because the only price writer asks a provider), a model name is typed, and its card is shown.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type Request } from "@playwright/test";

import { DASHBOARD_URL, REPO_ROOT, evidencePath } from "./harness";

const TICKET = "202";

const BODY = readFileSync(resolve(REPO_ROOT, "dashboard/tests/fixtures/gateway/prices.body"), "utf8");

test("typing a model name shows its stored price as a card", async ({ page }) => {
  const origin = new URL(DASHBOARD_URL).origin;
  const log: Request[] = [];
  await page.route("**/*", async (route) => {
    const request = route.request();
    log.push(request);
    const url = new URL(request.url());
    if (url.origin !== origin) {
      await route.abort("blockedbyclient");
    } else if (url.pathname === "/v1/prices") {
      await route.fulfill({ status: 200, contentType: "application/json", body: BODY });
    } else {
      await route.continue();
    }
  });

  await page.goto(`${DASHBOARD_URL}/models`);
  const search = page.getByLabel("Search models");
  await expect(search).toBeVisible();
  await expect(page.getByText("Type a model name to see its stored price.")).toBeVisible();

  await search.fill("gpt-oss");
  const card = page.getByRole("listitem").filter({ hasText: "openai/gpt-oss-120b" });
  await expect(card).toHaveCount(1);
  await expect(card).toContainText("https://openrouter.ai/api/v1/models");
  await expect(page.getByText("stealth/space-bunny-alpha")).toHaveCount(0);

  await page.screenshot({ path: evidencePath(TICKET, "models-search.png"), fullPage: true });
  expect(log.length).toBeGreaterThan(0);
});
