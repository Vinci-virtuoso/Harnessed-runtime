/**
 * FR-78's models walk (ticket 149, FR-88): a key saved from the Models page changes `.env` and
 * writes **0** ledger rows.
 *
 * The third of the three walks. The read walk loads `/models` and never presses its one sanctioned
 * control; this walk presses it. A credential is typed into a provider card's field and saved, and
 * two things are measured, neither of them a sentence the page prints about itself (MUST rule 12):
 *
 *   1. **`.env`**, read off disk in the dashboard process's working directory, which the harness
 *      puts beside its home rather than in the checkout (`serverDirectory`), so the operator's own
 *      `.env` is never the file written. It must change, and it must name the key's variable.
 *   2. **The ledger**: `PRAGMA user_version` and every table's row count, before and after, through
 *      `db.connect_readonly`, enumerated from `sqlite_master`. They must be identical.
 *
 * The save is validated before it is written (FR-88, `onboarding.configure_provider`), which is
 * one call to the provider. The harness points the dashboard process's `HARNESSED_BASE_URL` at a
 * loopback stand-in started here, so the call is answered offline and the stand-in can say which
 * credential it was shown. The browser never reaches it: the network log still allows one origin.
 *
 * And the check is proven able to fire: a second save runs with one extra row written by the spec
 * through `db.connect()`, and the same ledger assertion must throw.
 */
import { createHash, randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { type Server, createServer } from "node:http";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import {
  DASHBOARD_URL,
  STUB_PORT,
  type Ledger,
  evidencePath,
  harnessHome,
  isWrite,
  ledger,
  rowDelta,
  seedExtraRow,
  serverDirectory,
} from "./harness";

const TICKET = "149";

const ORIGIN = new URL(DASHBOARD_URL).origin;
const SAVE_ROUTE = "/control/v1/providers";

/** A keyed provider on the OpenAI-compatible dialect, so the stand-in speaks one wire shape. */
const PROVIDER = "groq";
const KEY_VAR = "GROQ_API_KEY";

const SETTLE_TIMEOUT_MS = 60_000;

/** The file the control app writes, in the dashboard process's working directory. */
function envFile(): string {
  return join(serverDirectory(harnessHome()), ".env");
}

/** `.env` as bytes, fingerprinted, or `absent`: the walk compares fingerprints, never prints keys. */
function envState(): string {
  if (!existsSync(envFile())) return "absent";
  return createHash("sha256").update(readFileSync(envFile())).digest("hex");
}

/** A credential no provider has issued, fresh per save so a second save changes `.env` again. */
function credential(): string {
  return `gsk_e2e149_${randomBytes(12).toString("hex")}`;
}

// ---- The stand-in endpoint ----------------------------------------------------------------------

/** The `Authorization` header of every call the stand-in answered. */
const shown: string[] = [];
let stub: Server | undefined;

test.beforeAll(async () => {
  stub = createServer((request, response) => {
    shown.push(request.headers.authorization ?? "");
    request.resume();
    request.on("end", () => {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(
        JSON.stringify({
          id: "chatcmpl-e2e149",
          object: "chat.completion",
          created: 0,
          model: "stand-in",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "ok" },
              finish_reason: "stop",
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        }),
      );
    });
  });
  await new Promise<void>((listening) => stub?.listen(STUB_PORT, "127.0.0.1", listening));
});

test.afterAll(async () => {
  await new Promise((closed) => stub?.close(closed));
});

// ---- The walk -----------------------------------------------------------------------------------

type Logged = { method: string; url: string };

async function watchNetwork(page: Page, log: Logged[]): Promise<void> {
  await page.route("**/*", async (route) => {
    const request = route.request();
    log.push({ method: request.method(), url: request.url() });
    if (new URL(request.url()).origin !== ORIGIN) return route.abort("blockedbyclient");
    return route.continue();
  });
}

/** The provider's card on `/models`, found by its heading. */
function card(page: Page) {
  return page
    .getByTestId("provider-card")
    .filter({ has: page.getByRole("heading", { name: PROVIDER, exact: true }) });
}

/**
 * One models walk: the ledger and `.env` before, one key typed and saved from the card, the
 * gateway's `configured` read back, the ledger and `.env` after. `during` runs while the save is
 * being made, which is where the proof seeds its extra row.
 */
async function modelsWalk(
  page: Page,
  log: Logged[],
  during: () => void = () => undefined,
): Promise<{ before: Ledger; after: Ledger; envBefore: string; envAfter: string; key: string }> {
  await watchNetwork(page, log);
  const before = ledger();
  const envBefore = envState();

  await page.goto(`${DASHBOARD_URL}/models`);
  const target = card(page);
  await expect(target).toBeVisible({ timeout: SETTLE_TIMEOUT_MS });
  // A configured card keeps its field behind "rotate key"; the operator's environment may hold
  // the key already, and the second walk always finds it saved by the first.
  const rotate = target.getByRole("button", { name: "rotate key" });
  if (await rotate.isVisible()) await rotate.click();

  const key = credential();
  await target.getByLabel(`API key for ${PROVIDER}`).fill(key);
  const answered = page.waitForResponse(
    (response) => new URL(response.url()).pathname === SAVE_ROUTE,
    { timeout: SETTLE_TIMEOUT_MS },
  );
  await target.getByRole("button", { name: "Save key" }).click();
  during();
  expect((await answered).status(), `POST ${SAVE_ROUTE}`).toBe(200);
  // The page reads `/v1/providers` again after a 200, so `configured` is the gateway's word.
  await expect
    .poll(() => page.locator('[aria-busy="true"]').count(), { timeout: SETTLE_TIMEOUT_MS })
    .toBe(0);
  await expect(target.getByRole("button", { name: "rotate key" })).toBeVisible();
  await expect(target).toContainText("true");

  return { before, after: ledger(), envBefore, envAfter: envState(), key };
}

/** FR-78's models claim: 0 ledger rows changed. Kept as a function so the proof can watch it fail. */
function assertNoRows(before: Ledger, after: Ledger): void {
  expect(rowDelta(before, after), "ledger rows the models walk changed").toEqual({});
}

function report(name: string, lines: string[]): void {
  writeFileSync(evidencePath(TICKET, name), `${lines.join("\n")}\n`, "utf8");
}

test("a key saved from the Models page changes .env and writes 0 ledger rows", async ({ page }) => {
  const log: Logged[] = [];
  const walked = await modelsWalk(page, log);
  await page.screenshot({ path: evidencePath(TICKET, "models-walk.png"), fullPage: true });

  const written = readFileSync(envFile(), "utf8");
  const writes = log
    .filter((entry) => isWrite(entry.method, entry.url))
    .map((entry) => `${entry.method} ${new URL(entry.url).pathname}`);
  report("models-walk.txt", [
    `FR-78, ticket ${TICKET}: the models walk (${PROVIDER})`,
    `.env: ${envFile()}`,
    `  before: ${walked.envBefore}`,
    `  after:  ${walked.envAfter}`,
    `  names ${KEY_VAR}: ${written.includes(`${KEY_VAR}=`)}`,
    "",
    "ledger delta:",
    JSON.stringify(rowDelta(walked.before, walked.after), null, 2),
    "",
    "the page's writes:",
    ...writes.map((write) => `  ${write}`),
    `other origins: ${log.filter((entry) => new URL(entry.url).origin !== ORIGIN).length}`,
    `the credential in any URL: ${log.some((entry) => entry.url.includes(walked.key))}`,
    `the stand-in was shown the typed key: ${shown.includes(`Bearer ${walked.key}`)}`,
  ]);

  expect(walked.envAfter, ".env changed").not.toBe(walked.envBefore);
  expect(written, `.env names ${KEY_VAR}`).toContain(`${KEY_VAR}=`);
  assertNoRows(walked.before, walked.after);
  expect(writes).toEqual([`POST ${SAVE_ROUTE}`]);
  expect(log.filter((entry) => new URL(entry.url).origin !== ORIGIN)).toEqual([]);
  expect(log.filter((entry) => entry.url.includes(walked.key))).toEqual([]);
  expect(shown, "the one validation call carried the typed key").toContain(`Bearer ${walked.key}`);
});

/** The check, proven able to fail: the same save with one extra row written by the spec. */
test("the models walk fails on one extra ledger row", async ({ page }) => {
  const log: Logged[] = [];
  const walked = await modelsWalk(page, log, () => seedExtraRow());

  let thrown = "";
  try {
    assertNoRows(walked.before, walked.after);
  } catch (error) {
    thrown = String(error);
  }
  report("models-walk-fires.txt", [
    `FR-78, ticket ${TICKET}: the models walk with one seeded row (db.connect(), mem_episodic)`,
    "",
    "ledger delta:",
    JSON.stringify(rowDelta(walked.before, walked.after), null, 2),
    "",
    `.env changed: ${walked.envAfter !== walked.envBefore}`,
    `assertNoRows threw: ${thrown !== ""}`,
  ]);
  expect(walked.envAfter, ".env changed on the second save too").not.toBe(walked.envBefore);
  expect(thrown, "the ledger check caught the seeded row").not.toBe("");
});
