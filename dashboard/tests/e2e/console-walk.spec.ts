/**
 * FR-78's console walk (ticket 149): the console writes **exactly** the rows one run writes.
 *
 * The read walk (`read-only.spec.ts`) proves the rest of the window writes nothing. The console is
 * the one page that is meant to write, so its claim is not "0" but "these rows and no others":
 * one message and one follow-up sent through the page under `HARNESSED_PROVIDER=fake`, the row
 * count of every table read before and after through `db.connect_readonly`, and the delta compared
 * table by table against the delta `harnessed run` then `harnessed run --session` produce from the
 * CLI on a second, fresh home. Two paths, one delta: the control app calls the CLI's composition
 * directly (decision 49), so any row the browser path writes that the CLI does not is a write the
 * control app invented.
 *
 * The browser path is measured as a delta on the harness's home, which `globalSetup` seeded,
 * because the e2e harness serves one home from one process (decision 30, ticket 125); the CLI's
 * home is a fresh one, and its baseline is the migrated schema and nothing else. The tables a run
 * writes do not depend on what the home held before it, which is what makes the comparison fair.
 *
 * The network log is kept too: the page's only writes are its two `POST`s to `/control/v1/**`.
 *
 * And the check is proven able to fire: a second walk runs with one extra row written by the spec
 * through `db.connect()` while the page is sending, and the same comparison must throw.
 */
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import {
  DASHBOARD_URL,
  type Ledger,
  cli,
  evidencePath,
  harnessHome,
  isWrite,
  ledger,
  rowDelta,
  seedExtraRow,
} from "./harness";

const TICKET = "149";

/** The one origin the browser may reach, and where the control app answers on it. */
const ORIGIN = new URL(DASHBOARD_URL).origin;
const CONTROL_PATH = "/control/v1/";

/** The fake corpus's ad-hoc scope is `sha256("ping")`, and its base variant answers twice. */
const MESSAGE = "ping";
const FOLLOW_UP = "ping again";

/** The tables FR-78 says one run writes. A table outside this list that moved is a failure. */
const RUN_TABLES = [
  "decision_options",
  "decisions",
  "events",
  "mem_episodic",
  "run_messages",
  "tool_results",
  "turns",
];

/** Two runs under the slowed fake provider, twice over, plus the CLI's own two. */
const WALK_TIMEOUT_MS = 300_000;
const SETTLE_TIMEOUT_MS = 60_000;

/** The composer, as `ConsolePanel` names it. */
const COMPOSER = "Message to the harness";
const SEND = "Send";

/** One request of the walk, as the route hook saw it. */
type Logged = { method: string; url: string };

/** The CLI's delta, read once for both tests. */
let cliDelta: Record<string, number> = {};
let cliReport: string[] = [];

// ---- The CLI path, on a second fresh home -------------------------------------------------------

test.beforeAll(() => {
  test.setTimeout(WALK_TIMEOUT_MS);
  const fresh = join(mkdtempSync(join(tmpdir(), "harnessed-e2e-cli-")), "home");
  mkdirSync(fresh);

  const before = ledger(fresh, true);
  const opened = cli(["run", MESSAGE, "--json"], fresh);
  expect(opened.status, opened.stderr).toBe(0);
  const runKey = (JSON.parse(opened.stdout) as { run_key: string }).run_key;
  const continued = cli(["run", FOLLOW_UP, "--session", runKey, "--json"], fresh);
  expect(continued.status, continued.stderr).toBe(0);
  const after = ledger(fresh);

  cliDelta = rowDelta(before, after);
  cliReport = [
    `harnessed run ${MESSAGE}, then harnessed run ${JSON.stringify(FOLLOW_UP)} --session ${runKey}`,
    `on a fresh home: ${fresh}`,
    JSON.stringify(cliDelta, null, 2),
  ];
});

// ---- The browser path ---------------------------------------------------------------------------

/** Record every request; let nothing reach another origin. */
async function watchNetwork(page: Page, log: Logged[]): Promise<void> {
  await page.route("**/*", async (route) => {
    const request = route.request();
    log.push({ method: request.method(), url: request.url() });
    if (new URL(request.url()).origin !== ORIGIN) return route.abort("blockedbyclient");
    return route.continue();
  });
}

/**
 * The ledger once it has stopped moving. The stream closes at `run.finished`, and the worker is
 * still filing the run's episode after it, so a reading taken at that instant can miss a row that
 * belongs to this run. Two readings in a row that agree are the run at rest.
 */
async function settledLedger(): Promise<Ledger> {
  let previous = ledger();
  let matches = 0;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await new Promise((done) => setTimeout(done, 500));
    const next = ledger();
    if (JSON.stringify(next) === JSON.stringify(previous)) {
      matches += 1;
      // Two readings in a row that agree could still both land inside the worker's own gap
      // between `run.finished` and filing the episode (this file's own note); a third agreeing
      // reading is what tells the two of them apart from that gap rather than from rest.
      if (matches >= 2) return next;
    } else {
      matches = 0;
    }
    previous = next;
  }
  throw new Error("the ledger never stopped moving after the run");
}

/** Type one string into the composer, send it, and wait until the page is at rest again. */
async function send(page: Page, text: string, viaSidechat = false): Promise<void> {
  // Ticket 210: the first message goes through the page-bottom composer, a follow-up through Sidechat.
  // The first accepted send already opened Sidechat, so the button is only there when it is closed.
  const closed = page.getByRole("button", { name: "Sidechat", exact: true });
  if (viaSidechat && (await closed.count()) > 0) await closed.click();
  const composer = page.getByLabel(COMPOSER);
  await expect(composer).toBeEnabled({ timeout: SETTLE_TIMEOUT_MS });
  await composer.fill(text);
  await page.getByRole("button", { name: SEND, exact: true }).click();
  // Accepted: the composer empties and holds while the run is in flight.
  await expect(composer).toHaveValue("", { timeout: SETTLE_TIMEOUT_MS });
  // At rest: the stream ended, the composer is ready and the reply renders in its exchange.
  await expect(composer).toBeEnabled({ timeout: SETTLE_TIMEOUT_MS });
  // The newest exchange has left `data-live` and holds its reply: it landed without a refresh.
  const newest = page.getByTestId("exchange").last();
  await expect(newest).not.toHaveAttribute("data-live", /.*/, { timeout: SETTLE_TIMEOUT_MS });
  await expect(newest.getByTestId("output-view")).toBeVisible({ timeout: SETTLE_TIMEOUT_MS });
  await expect
    .poll(() => page.locator('[aria-busy="true"]').count(), { timeout: SETTLE_TIMEOUT_MS })
    .toBe(0);
  await settledLedger();
}

/**
 * One console walk: the ledger before, one message and one follow-up through the page, the ledger
 * after. `during` runs between the two sends, which is where the proof seeds its extra row.
 */
async function consoleWalk(
  page: Page,
  log: Logged[],
  during: () => void = () => undefined,
): Promise<Record<string, number>> {
  await watchNetwork(page, log);
  const before = await settledLedger();
  await page.goto(`${DASHBOARD_URL}/console`);
  await expect(page.locator("main h1").first()).toBeVisible({ timeout: SETTLE_TIMEOUT_MS });
  await send(page, MESSAGE);
  // The stream closes at `run.finished`, but the worker is still filing the run's episode after
  // it (`settledLedger`'s own note); the reply this ticket's UI shows comes straight off `turns`,
  // so it can go visible before that filing lands. A stable ledger reading, not just a visible
  // reply, is what makes the second send's own state (its digest of "this session so far") see
  // everything the first send wrote.
  await settledLedger();
  during();
  await send(page, FOLLOW_UP, true);
  await page.screenshot({ path: evidencePath("200", "follow-up-reply.png"), fullPage: true });
  await page.screenshot({ path: evidencePath("210", "sidechat.png"), fullPage: true });
  return rowDelta(before, await settledLedger());
}

/**
 * FR-78's console claim. The browser's delta is the CLI's, table for table, and every table that
 * moved is one a run writes. Kept as a function so the proof test can watch it fail.
 */
function assertOneRunsRows(browser: Record<string, number>, reference: Record<string, number>): void {
  expect(browser, "the rows the console wrote, against the rows the CLI wrote").toEqual(reference);
  const strays = Object.keys(browser).filter((table) => !RUN_TABLES.includes(table));
  expect(strays, "tables the console wrote that a run does not").toEqual([]);
}

/** The page's writes: every non-`GET`, which must be the two sends to the control app. */
function writesOf(log: Logged[]): string[] {
  return log
    .filter((entry) => isWrite(entry.method, entry.url))
    .map((entry) => `${entry.method} ${new URL(entry.url).pathname}`);
}

/** The page's own reads, `GET`/`HEAD` requests' paths (ticket 188's AC7: the messages read is
 * part of the walk's own delta, not the `/v1/runs/<id>` read the v4 console used). */
function readsOf(log: Logged[]): string[] {
  return log
    .filter((entry) => entry.method === "GET" || entry.method === "HEAD")
    .map((entry) => new URL(entry.url).pathname);
}

function report(name: string, lines: string[]): void {
  writeFileSync(evidencePath(TICKET, name), `${lines.join("\n")}\n`, "utf8");
}

test("a message and a follow-up through the console write exactly the rows the CLI writes", async ({
  page,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  const log: Logged[] = [];
  const browser = await consoleWalk(page, log);
  await page.screenshot({ path: evidencePath(TICKET, "console-walk.png"), fullPage: true });

  const writes = writesOf(log);
  const reads = readsOf(log);
  report("console-walk.txt", [
    `FR-78, ticket ${TICKET}: the console walk`,
    `home: ${harnessHome()}`,
    "",
    "browser path (the console page, delta over the harness's home):",
    JSON.stringify(browser, null, 2),
    "",
    "CLI path:",
    ...cliReport,
    "",
    "table-by-table diff (browser minus CLI):",
    ...[...new Set([...Object.keys(browser), ...Object.keys(cliDelta)])]
      .sort()
      .map((table) => `  ${table}: ${(browser[table] ?? 0) - (cliDelta[table] ?? 0)}`),
    "",
    "the page's writes:",
    ...writes.map((write) => `  ${write}`),
    `other origins: ${log.filter((entry) => new URL(entry.url).origin !== ORIGIN).length}`,
    "",
    "the messages read, part of this walk's own reads (ticket 188, AC7):",
    ...[...new Set(reads)].filter((path) => path.includes("/messages")).map((path) => `  GET ${path}`),
  ]);

  expect(Object.keys(cliDelta).length, "the CLI's two runs wrote rows").toBeGreaterThan(0);
  assertOneRunsRows(browser, cliDelta);
  expect(writes).toHaveLength(2);
  expect(writes.every((write) => write.startsWith(`POST ${CONTROL_PATH}runs`))).toBe(true);
  expect(log.filter((entry) => new URL(entry.url).origin !== ORIGIN)).toEqual([]);
  // The console renders the exchange from the session's messages read, not from `/v1/runs/<id>`
  // (the v4 console's own reply source): AC7's comparison of the two paths carries this read.
  expect(reads.some((path) => /\/v1\/sessions\/[^/]+\/messages$/.test(path))).toBe(true);
});

/**
 * The check, proven able to fail: the same walk with one extra row written by the spec through
 * `db.connect()` between the message and the follow-up. The page wrote exactly what it did before;
 * the ledger did not, and the comparison must say so.
 */
test("the console walk fails on one extra row the page did not write", async ({ page }) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  const log: Logged[] = [];
  const browser = await consoleWalk(page, log, () => seedExtraRow());

  let thrown = "";
  try {
    assertOneRunsRows(browser, cliDelta);
  } catch (error) {
    thrown = String(error);
  }
  report("console-walk-fires.txt", [
    `FR-78, ticket ${TICKET}: the console walk with one seeded row (db.connect(), mem_episodic)`,
    "",
    "browser path:",
    JSON.stringify(browser, null, 2),
    "",
    "CLI path:",
    JSON.stringify(cliDelta, null, 2),
    "",
    `assertOneRunsRows threw: ${thrown !== ""}`,
  ]);
  expect(thrown, "the comparison caught the seeded row").not.toBe("");
  expect(browser.mem_episodic).toBe((cliDelta.mem_episodic ?? 0) + 1);
});
