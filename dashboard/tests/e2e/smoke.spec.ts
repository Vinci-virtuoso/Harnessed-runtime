/**
 * FR-43 end to end: with every `/v1` read refused, `/` renders `UnreachableState`, and the walk
 * asks nothing of any origin but its own, with `GET` only.
 *
 * "Unreachable" is a rejected browser `fetch` and nothing else (MUST rule 23), so from v4 the case
 * is a `page.route` abort of `/v1/**` on the one process rather than a second dashboard pointed at
 * a dead port (decision 30, FR-66): the gateway is mounted in the process that served the page,
 * and there is no second origin left to point at.
 */
import { writeFileSync } from "node:fs";
import { expect, test, type Request } from "@playwright/test";

import { DASHBOARD_URL, GATEWAY_URL, admitsHead, evidencePath, isWrite } from "./harness";

const TICKET = "092";
const FONT_HOSTS = new Set(["fonts.googleapis.com", "fonts.gstatic.com"]);

/** The mounted gateway's own prefix, as the page fetches it: relative, on its own origin. */
const GATEWAY_PREFIX = "/v1/";

test("with every /v1 read refused, / renders UnreachableState and reaches only its own origin", async ({
  page,
}) => {
  const origin = new URL(DASHBOARD_URL).origin;
  const log: Request[] = [];
  const refused: string[] = [];
  await page.route("**/*", async (route) => {
    const request = route.request();
    log.push(request);
    const url = new URL(request.url());
    if (url.origin !== origin) {
      await route.abort("blockedbyclient");
    } else if (url.pathname.startsWith(GATEWAY_PREFIX)) {
      refused.push(url.pathname);
      await route.abort("connectionrefused");
    } else {
      await route.continue();
    }
  });

  const response = await page.goto(`${DASHBOARD_URL}/`);
  expect(response?.status()).toBe(200);

  // `/` is nine panels (tickets 252 and 253 added "Needs you" and "Tasks" to the seven below), each reading its own `/v1` route, so each is its own UnreachableState:
  // every one says so, and one of them holds focus. The six aggregates of ticket 134, then the
  // activity list of ticket 187; the health panel that made eight moved to the Models page's
  // SystemSection in ticket 186 (FR-129).
  const headings = page.getByRole("heading", { name: "Gateway unreachable" });
  await expect(headings).toHaveCount(9);
  for (const heading of await headings.all()) await expect(heading).toBeVisible();
  await expect(page.locator("h2:focus", { hasText: "Gateway unreachable" })).toHaveCount(1);
  // The state names `harnessed serve` as the fix and draws no route: since ticket 191 no endpoint
  // string is rendered anywhere (MUST rule 31), the URL it tried included.
  await expect(page.getByText(/harnessed serve/).first()).toBeVisible();
  expect(await page.getByRole("main").textContent()).not.toContain(GATEWAY_PREFIX);
  await page.evaluate(() => document.fonts.ready);
  const fontFamily = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
  expect(fontFamily).toMatch(/Geist/);
  for (const theme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme: theme });
    const name = theme === "dark" ? "unreachable.png" : "unreachable-light.png";
    await page.screenshot({ path: evidencePath(TICKET, name), fullPage: true });
  }

  const urls = log.map((request) => request.url());
  const fontRequests = urls.filter((url) => FONT_HOSTS.has(new URL(url).hostname));
  const foreign = urls.filter((url) => new URL(url).origin !== origin);
  // FR-53 as amended: nothing but `GET` to `/v1/**`, and no write anywhere else. `HEAD` off `/v1`
  // is Next's export-mode prefetch of a linked page, a read, and the walk lets it stand.
  const nonGet = log
    .filter((request) => isWrite(request.method(), request.url()))
    .map((request) => `${request.method()} ${new URL(request.url()).pathname}`);
  writeFileSync(
    evidencePath(TICKET, "network-log.txt"),
    [
      ...log.map((request) => `${request.method()} ${request.url()}`),
      "",
      `gateway reads refused at the browser: ${refused.length}`,
      `font host requests: ${fontRequests.length}`,
      `other origins: ${foreign.length}`,
      `non-GET: ${nonGet.length}`,
      "",
    ].join("\n"),
  );
  expect(urls.length).toBeGreaterThan(0);
  expect(refused.length).toBeGreaterThan(0);
  expect(fontRequests).toEqual([]);
  expect(foreign).toEqual([]);
  expect(nonGet).toEqual([]);
});

// Widening (d), ratified 2026-10-11 (Q7), ticket 295: the log admits a `HEAD` only same-origin and
// off `/v1`. Seeded requests, not a broken page: each seeded violation must be counted as a write.
test("the request log admits a HEAD only same-origin and off /v1", () => {
  const prefetch = { method: "HEAD", url: `${DASHBOARD_URL}/runs` };
  const gateway = { method: "HEAD", url: `${GATEWAY_URL}/runs` };
  const foreign = { method: "HEAD", url: "https://example.com/runs" };
  const seeded = [prefetch, gateway, foreign];
  const writes = seeded.filter((entry) => isWrite(entry.method, entry.url));
  expect(admitsHead(prefetch.method, prefetch.url), "a same-origin HEAD off /v1 is admitted").toBe(
    true,
  );
  expect(writes, "a HEAD to /v1/runs and a HEAD to another origin each fail").toEqual([
    gateway,
    foreign,
  ]);
  expect(isWrite("POST", `${DASHBOARD_URL}/runs`), "only a HEAD is ever admitted").toBe(true);
  expect(isWrite("GET", `${GATEWAY_URL}/runs`), "a GET is a read").toBe(false);
  writeFileSync(
    evidencePath("295", "head-widening.txt"),
    [
      ...seeded.map((entry) => `${entry.method} ${entry.url}: write=${isWrite(entry.method, entry.url)}`),
      "",
    ].join("\n"),
  );
});

test("the harness started one seeded process serving the UI and the gateway", async ({
  request,
}) => {
  expect(new URL(GATEWAY_URL).origin).toBe(new URL(DASHBOARD_URL).origin);

  const health = await request.get(`${GATEWAY_URL}/health`);
  expect(health.status()).toBe(200);
  const body = (await health.json()) as { schema_version: number; database_path: string };
  expect(body.schema_version).toBeGreaterThan(0);

  const runs = await request.get(`${GATEWAY_URL}/eval/runs`);
  expect(runs.status()).toBe(200);
  expect(await runs.text()).toContain("smoke");

  const home = await request.get(`${DASHBOARD_URL}/`);
  expect(home.status()).toBe(200);
});
