/**
 * FR-78's read walk (ticket 149, splitting ticket 114's FR-53 walk): a walk of every page writes
 * nothing, reaches nothing but the dashboard, and never reaches the control app.
 *
 * The read-only proof splits rather than relaxing (decision 49). This walk keeps FR-53's "0"; the
 * console walk (`console-walk.spec.ts`) owns the console's writes and proves they are exactly one
 * run's rows; the models walk (`models.spec.ts`) owns the one key save and proves it changes `.env`
 * and no ledger row. The console page is walked here loaded, with no message sent.
 *
 * The other e2e specs each prove their own screen. This one is the cross-cutting claim the PRD
 * makes about the whole window, and it is three separate measurements of it, none of which trusts
 * a sentence the dashboard prints about itself (MUST rule 12):
 *
 *   1. **The network log.** A `page.route("**\/*")` hook records the method and the origin of
 *      every request the browser makes across the walk (decision 30). 0 of them may be a method
 *      other than `GET` to the read gateway at `/v1/**` (FR-53 as amended for v4, where the
 *      console walk owns the writes at `/control/v1`), 0 may be a write anywhere on the origin,
 *      0 may reach the control app at `/control/v1/**` by any method (the read walk sends
 *      nothing, so it has no business there), and 0 may be to any origin but the dashboard's.
 *      `HEAD` off `/v1` is a read and is let
 *      through: Next's export-mode prefetch sends one per linked page before it asks for that
 *      page's segment tree, so a navigation issues eight of them. The hook aborts every forbidden
 *      request rather than forwarding it, so a defect is recorded without a write ever leaving
 *      the browser.
 *   2. **The ledger.** `PRAGMA user_version` and the row count of every table in the one database
 *      are read before the walk and after it, through the Python package's read-only connection
 *      (`db.connect_readonly`, MUST rule 13) under the harness's own temporary `HARNESSED_HOME`.
 *      The tables are enumerated from `sqlite_master`, not listed here, so a table added later is
 *      counted without this file being edited. The architecture's FR-53 row says "through the
 *      CLI"; no CLI command prints raw row counts, and the shard recorded that resolution.
 *   3. **The controls.** Every control on every page is enumerated out of the DOM and its
 *      accessible name read. 0 may name a start, a promotion, an archive or a delete, and no
 *      `<form>` may carry an `action` or a method other than `GET`. The two Improvement pages,
 *      where a write is most tempting, must offer the CLI command to copy instead, and every
 *      page's provenance drawer must offer the CLI `--json` twin, never a route (`D-9`, FR-126).
 *
 * And the log's own check is proven able to fire: a second test injects a `POST` and a
 * cross-origin `GET` into the page and asserts that the same predicate reports them and that the
 * same assertion fails. A check that has never failed is not yet a check.
 *
 * The walk itself is read-only. Seeding happens in `beforeAll`, **before** the first ledger read,
 * through the CLI as an operator runs it (decision 30) - an improvement cycle for the cycle page
 * and its procedure, one ad-hoc `run` for an episode, and one fact - so every id the walk spells
 * into a URL is an id the gateway already holds and every page renders figures rather than an
 * empty state.
 *
 * The pages walked are not a list kept in step by hand: the route templates are read off
 * `app/**\/page.tsx` and the walk must cover every one of them. The tree holds fourteen page
 * files, which is the PRD's "thirteen pages" plus the compare page FR-56 added later.
 */
import { readdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { openDisclosures, pageFindings } from "./endpoint-walk";
import {
  DASHBOARD_ROOT,
  DASHBOARD_URL,
  GATEWAY_URL,
  cli as cliUnder,
  evidencePath,
  harnessHome,
  isWrite,
  ledger,
} from "./harness";

const TICKET = "149";

/** The one origin the browser may reach across the whole walk. */
const ORIGIN = new URL(DASHBOARD_URL).origin;

/** An origin the dashboard has no business asking anything of, used to prove the check fires. */
const FOREIGN = "https://harnessed.invalid/beacon";

/** The read gateway's prefix on the dashboard's origin, where nothing but `GET` may go. */
const GATEWAY_PATH = "/v1/";

/** The control app's prefix on the same origin, which the read walk may not reach at all. */
const CONTROL_PATH = "/control/v1/";

/** The CLI, the browser, two servers and a seeded improvement cycle share one machine. */
const WALK_TIMEOUT_MS = 300_000;

/** How long one page is given to settle, and a session stream to close. */
const SETTLE_TIMEOUT_MS = 60_000;

/**
 * Every element an operator can act on. A figure is not one; a control is - true before v5 because
 * a figure was a `span`, and kept true now that `Value` is its own trigger (`data-figure`): a
 * figure's only effect is opening the read-only drawer, so it is excluded here on the same
 * principle, not carved out because its ledger text happens to collide with a write verb (the
 * `.steps[]` list can legitimately hold the improve step named `promote`).
 */
const CONTROLS = 'button:not([data-figure]), a[href], input, select, textarea, summary, [role="button"]';

/**
 * The verbs a read-only window may not offer. `D-8` names four - start, promote, archive,
 * delete - and the rest are the same act said another way, so a control that renames itself
 * still fails. Matched against a control's accessible name only: the Improvement pages print
 * `harnessed improve promote <cand_id>` as copyable text, which is the opposite of a button.
 */
const WRITE_VERBS =
  /\b(start|starts|run now|launch|promote|promotes|demote|archive|archives|unarchive|delete|deletes|remove|removes|discard|create|creates|edit|save|apply|rollback|revert|approve|reject|abort|cancel|kill|stop|retry|rerun|re-run|execute|submit|publish|write|overwrite)\b/i;

/** What the walk presses on every panel, and what the drawer offers instead of a write. */
const REFETCH = "Refresh";
const COPY_COMMAND = "Copy command";

/** A figure that opens a provenance drawer: `Value` is its own trigger from v5 (`D-4`, `D-9`). */
const FIGURE_TRIGGER = "button[data-figure]";

/** One request of the walk, as the route hook saw it. */
type Logged = { method: string; url: string; page: string };

/**
 * What the log says about itself: the three lists FR-78's read walk requires to be empty. `nonGet`
 * is every request `forbiddenMethod` names, which is every non-`GET` to `/v1/**` and every write
 * elsewhere; `control` is every request to `/control/v1/**`, whatever its method.
 */
type Violations = { nonGet: Logged[]; control: Logged[]; foreign: Logged[] };

/** One page of the walk: where the operator is, and what has to be true before it is read. */
type Stop = {
  /** The URL, which is what a failure names. */
  at: string;
  /**
   * A session page waits for its stream to close; every other page waits only for its panels.
   * `"console"` is the console's own watch of the same stream (ticket 188): it mounts no
   * `StreamPanel` for a session at rest, and its signal that the watch closed is the `Composer`
   * enabling again, the moment `ConsolePanel` sets the run at rest.
   */
  stream?: boolean | "console";
  /** A forwarder (ticket 206): the URL the browser must land on, with the query it was opened with. */
  forwardsTo?: string;
  /**
   * The one control on this page `D-8` sanctions as a write (decision 43): named, never pressed.
   * The Models page's key form is the only one (ticket 147); the models walk (`models.spec.ts`)
   * is what presses it, and asserts `.env` changed and 0 ledger rows did.
   */
  sanctioned?: RegExp;
};

/** The lines written to `evidence/149/read-network-log.txt` and `evidence/149/read-controls.txt`. */
const requests: Logged[] = [];
const controlLog: string[] = [];

/** Which page the hook attributes a request to, set as the walk moves. */
let at = "(before the walk)";

/** The ids the walk spells into a URL, all of them read from the gateway in `beforeAll`. */
const ids = {
  runKey: "",
  sessionId: "",
  episodeSessionId: "",
  evalRunId: "",
  before: "",
  after: "",
  improvementRunId: "",
  memId: "",
  factQuery: "",
};

// ---- The harness's own home, the CLI and the gateway --------------------------------------------

/** One CLI call under the run's temporary home and the fake provider, as an operator makes it. */
function cli(...argv: string[]): { status: number | null; stdout: string; stderr: string } {
  return cliUnder(argv);
}

/**
 * The ledger as it stands: `PRAGMA user_version` and every table's row count, enumerated from
 * `sqlite_master` through `db.connect_readonly` (`harness.ts::ledger`), as printed JSON.
 */
function snapshot(): string {
  return JSON.stringify(ledger(), null, 2);
}

/** One gateway body, read straight from the gateway rather than through the dashboard. */
async function gatewayJson(path: string): Promise<Record<string, unknown>> {
  const answer = await fetch(`${GATEWAY_URL}${path}`, { cache: "no-store" });
  expect(answer.status, `GET ${path}`).toBe(200);
  return (await answer.json()) as Record<string, unknown>;
}

// ---- The pages, read off the tree rather than listed --------------------------------------------

/**
 * Every route a `page.tsx` under `app/` serves, as a template whose bracketed segments stand for
 * one segment each.
 */
function routeTemplates(): string[] {
  const root = resolve(DASHBOARD_ROOT, "app");
  return readdirSync(root, { recursive: true, encoding: "utf8" })
    .filter((entry) => entry.replaceAll("\\", "/").endsWith("page.tsx"))
    .map((entry) => `/${entry.replaceAll("\\", "/").replace(/\/?page\.tsx$/, "")}`);
}

/** Does a template describe this pathname? A bracketed segment stands for exactly one segment. */
function templateMatches(template: string, pathname: string): boolean {
  const wanted = template.split("/");
  const asked = pathname.split("/");
  if (wanted.length !== asked.length) return false;
  return wanted.every(
    (segment, index) => (segment.startsWith("[") && segment.endsWith("]")) || segment === asked[index],
  );
}

// ---- The network log ----------------------------------------------------------------------------

/**
 * Record every request, and let nothing through that FR-53 forbids.
 *
 * Aborting rather than forwarding is deliberate: if a page ever did try to write, the log records
 * the attempt and the ledger is still untouched, so the two measurements stay independent.
 */
async function watchNetwork(page: Page): Promise<void> {
  await page.route("**/*", async (route) => {
    const request = route.request();
    const entry = { method: request.method(), url: request.url(), page: at };
    requests.push(entry);
    if (new URL(request.url()).origin !== ORIGIN) return route.abort("blockedbyclient");
    if (forbiddenMethod(entry) || toControl(entry)) return route.abort("blockedbyclient");
    return route.continue();
  });
}

/** Is this request to the control app? The read walk may not reach it by any method. */
function toControl(entry: Logged): boolean {
  return new URL(entry.url).pathname.startsWith(CONTROL_PATH);
}

/**
 * Is this request's method one FR-53 forbids? Anything but `GET` to the read gateway is (MUST
 * rule 22 as amended: never send the gateway a method other than `GET`); anywhere else on the
 * origin a `HEAD` is a read, and every other non-`GET` is a write.
 */
function forbiddenMethod(entry: Logged): boolean {
  if (entry.method === "GET") return false;
  if (new URL(entry.url).pathname.startsWith(GATEWAY_PATH)) return true;
  return isWrite(entry.method, entry.url);
}

/** What the log says: the requests whose method is forbidden and the ones to another origin. */
function violations(log: Logged[]): Violations {
  return {
    nonGet: log.filter(forbiddenMethod),
    control: log.filter(toControl),
    foreign: log.filter((entry) => new URL(entry.url).origin !== ORIGIN),
  };
}

/** FR-78's three empty lists. Kept as a function so the proof test can watch it fail. */
function assertReadOnly(found: Violations): void {
  expect(found.nonGet, "requests whose method FR-53 forbids").toEqual([]);
  expect(found.control, "requests to the control app").toEqual([]);
  expect(found.foreign, "requests to an origin other than the dashboard's").toEqual([]);
}

// ---- One stop of the walk -----------------------------------------------------------------------

/**
 * Wait until the page has rendered and no panel on it is still fetching. The export renders every
 * page in the browser behind the root layout's empty Suspense fallback (decision 45), so `main`
 * is bare until hydration, and a panel count taken before then sees no panel at all rather than
 * a busy one; every page's heading is the signal that its body has arrived. `PanelFrame` then
 * says a fetch is still running with `aria-busy`.
 */
async function settle(page: Page): Promise<void> {
  await expect(page.locator("main h1").first()).toBeVisible({ timeout: SETTLE_TIMEOUT_MS });
  await expect
    .poll(() => page.locator('[aria-busy="true"]').count(), { timeout: SETTLE_TIMEOUT_MS })
    .toBe(0);
}

/** Every control on the page, as `<tag> <accessible name>`. */
async function controls(page: Page): Promise<string[]> {
  return page.evaluate((selector) => {
    const named = (node: Element): string => {
      const label = node.getAttribute("aria-label");
      if (label) return label;
      const text = (node.textContent ?? "").replace(/\s+/g, " ").trim();
      if (text) return text;
      if (node instanceof HTMLInputElement) {
        const own = node.labels?.[0]?.textContent ?? "";
        return `${node.type}:${own.replace(/\s+/g, " ").trim()}:${node.value}`;
      }
      return node.getAttribute("title") ?? "";
    };
    return [...document.querySelectorAll(selector)].map(
      (node) => `${node.tagName.toLowerCase()} ${named(node)}`,
    );
  }, CONTROLS);
}

/** Every `<form>` on the page, as `<method> <action>`. A form that navigates is a `GET`. */
async function forms(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll("form")].map(
      (form) => `${form.method} ${form.getAttribute("action") ?? ""}`,
    ),
  );
}

/** A file name for this stop, so a screenshot is named after the URL it shows. */
function slug(url: string): string {
  const path = new URL(url, DASHBOARD_URL);
  const name = `${path.pathname}${path.search}`.replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "");
  return name === "" ? "home" : name;
}

/**
 * One stop: open the page, let every panel finish, press `Refetch` on each, open a provenance
 * drawer and read what it offers, then enumerate the controls.
 */
async function walkTo(page: Page, stop: Stop): Promise<void> {
  at = stop.at;
  const response = await page.goto(`${DASHBOARD_URL}${stop.at}`);
  expect(response?.status(), `GET ${stop.at}`).toBe(200);
  await settle(page);
  if (stop.forwardsTo !== undefined) {
    await expect(page, `${stop.at} forwards`).toHaveURL(`${DASHBOARD_URL}${stop.forwardsTo}`);
  }

  // The subscription is a `GET` like any other read, and it has to be seen through: a stream
  // still open when the walk moves on is a request the log would attribute to the next page.
  if (stop.stream === true) {
    await expect(page.getByTestId("stream-status")).toHaveText(/^Done/, {
      timeout: SETTLE_TIMEOUT_MS,
    });
  } else if (stop.stream === "console") {
    // Ticket 210: the composer is Sidechat's, opened here so "enabled" still proves the page is at rest.
    await page.getByRole("button", { name: "Sidechat", exact: true }).click();
    await expect(page.getByRole("main").locator("textarea")).toBeEnabled({
      timeout: SETTLE_TIMEOUT_MS,
    });
  }

  // Ticket 203: the Overview's Latest run is the newest session's id, a link to its page.
  if (stop.at === "/") {
    const latest = page.getByRole("heading", { name: "Latest run" }).locator("xpath=..");
    const link = latest.getByRole("link");
    await expect(link, "the Latest run section holds one link").toHaveCount(1);
    await expect(link).toHaveAttribute("href", /^\/traces\?session=run_/);
    await link.scrollIntoViewIfNeeded();
    await page.screenshot({ path: evidencePath("203", "latest-run-link.png"), fullPage: true });

    // Ticket 222: a priced total carries no reason beside it; the reason is for a null leaf only.
    const spent = page.getByRole("region", { name: "Spent" });
    const total = spent.getByRole("group", { name: "Total", exact: true });
    await expect(total, "the Spent total is a served figure").toHaveCount(1);
    await expect(total).not.toContainText("unknown");
    await expect(
      spent.getByText("no turn could be priced"),
      "no reason renders beside a priced total",
    ).toHaveCount(0);
    await total.scrollIntoViewIfNeeded();
    await page.screenshot({ path: evidencePath("222", "no-reason-beside-value.png"), fullPage: true });
  }

  // Ticket 211: the shell's one `Refresh` is on every stop, the console included, and no panel
  // footer holds one. `exact`: a name is otherwise matched as a case-insensitive substring, and a
  // figure whose null reason reads "run `harnessed prices refresh`" (ticket 189's cost line) is a
  // `Value` button, which opens a drawer rather than reading the page again.
  const refetch = page.getByRole("button", { name: REFETCH, exact: true });
  await expect(refetch, `${stop.at} offers exactly one Refresh`).toHaveCount(1);
  await expect(page.getByRole("banner").getByRole("button", { name: REFETCH, exact: true })).toHaveCount(1);
  await expect(page.getByRole("main").getByRole("button", { name: REFETCH, exact: true })).toHaveCount(0);
  if (stop.at === "/") {
    await page.screenshot({ path: evidencePath("205", "one-refresh.png"), fullPage: true });
  }
  await page.screenshot({ path: evidencePath("211", "shell-refresh.png"), fullPage: true });
  await refetch.click();
  await settle(page);

  // The drawer, and the record it offers instead of anything that writes.
  const chip = page.locator(FIGURE_TRIGGER).first();
  await expect(chip, `${stop.at} renders a figure that can say where it came from`).toBeVisible();
  await chip.click();
  const drawer = page.getByRole("dialog");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId("record-view")).toBeVisible();
  await page.screenshot({ path: evidencePath(TICKET, `read-${slug(stop.at)}.png`), fullPage: true });

  // FR-126: with the drawer open, no endpoint string anywhere on the page.
  await openDisclosures(drawer);
  const drawn = await pageFindings(page);
  expect(drawn, `endpoint strings drawn on ${stop.at}`).toEqual([]);
  scanLog.push(`${stop.at}: ${drawn.length} endpoint strings`);

  // Every control the page offers, the drawer's included: the drawer is where a figure's full
  // provenance lives, so its controls belong to this page's enumeration.
  const named = await controls(page);
  const offending = named.filter(
    (control) => WRITE_VERBS.test(control) && !(stop.sanctioned?.test(control) ?? false),
  );
  const declared = await forms(page);
  const writing = declared.filter((form) => form.trim() !== "get");
  controlLog.push(
    `${stop.at}`,
    `  shell Refresh clicked, drawer opened, ${named.length} control(s)`,
    ...named.map((control) => `  - ${control}`),
    ...declared.map((form) => `  form: ${form}`),
    "",
  );

  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();

  expect(offending, `controls on ${stop.at} that would write`).toEqual([]);
  expect(writing, `forms on ${stop.at} that are not a GET`).toEqual([]);
}

/** The lines written to `evidence/191/`: one per page, the DOM scan's count (0). */
const scanLog: string[] = [];

function writeEvidence(): void {
  const found = violations(requests);
  writeFileSync(
    evidencePath(TICKET, "read-network-log.txt"),
    [
      `FR-78, ticket ${TICKET}: every request of the read walk`,
      `the one origin allowed: ${ORIGIN}`,
      "",
      ...requests.map((entry) => `${entry.method} ${entry.url}   [${entry.page}]`),
      "",
      `requests: ${requests.length}`,
      `non-GET: ${found.nonGet.length}`,
      `control app: ${found.control.length}`,
      `other origins: ${found.foreign.length}`,
      "",
    ].join("\n"),
    "utf8",
  );
  writeFileSync(
    evidencePath("191", "endpoint-scan.txt"),
    [`FR-126, ticket 191: the DOM scan of every page of the read walk, drawer open`, "", ...scanLog, ""].join("\n"),
    "utf8",
  );
  writeFileSync(
    evidencePath(TICKET, "read-controls.txt"),
    [`FR-78, ticket ${TICKET}: every control of every page`, "", ...controlLog].join("\n"),
    "utf8",
  );
}

// ---- Seeding: through the CLI, before the first ledger read -------------------------------------

test.beforeAll(async () => {
  test.setTimeout(WALK_TIMEOUT_MS);

  // An improvement cycle: a promotion, an active procedure and three more eval runs, so the cycle
  // page has an id and two runs of one suite exist for the comparison.
  const improved = cli("improve", "run", "--json");
  expect(improved.status, improved.stderr).toBe(0);
  ids.improvementRunId = (JSON.parse(improved.stdout) as { improvement_run_id: string })
    .improvement_run_id;

  // One ad-hoc run, which is the only thing that files an episode (`cli/run_cmd.py`), so the
  // episodes page reads a session that has one. "ping" is the fake corpus's scope fallback.
  expect(cli("run", "ping").status, "an ad-hoc run under the fake provider").toBe(0);

  // One fact, so the facts pages render a row rather than an empty state. `memory add` prints a
  // table rather than JSON, so the id is read back off the gateway with the rest of them.
  // One bare word: a hyphen is an operator in the FTS query the search route runs, so a hyphenated
  // term is a 500 from the gateway rather than an empty result.
  ids.factQuery = "readonly";
  const fact = `ticket ${TICKET} seeded this ${ids.factQuery} fact so the walk has one to open`;
  expect(cli("memory", "add", fact).status, "one seeded fact").toBe(0);

  const loopRuns = (await gatewayJson("/runs?limit=50")).runs as { run_key: string }[];
  const evalRuns = (await gatewayJson("/eval/runs")).runs as { eval_run_id: string; suite: string }[];
  const sessions = (await gatewayJson("/sessions?limit=100")).sessions as {
    session_id: string;
    terminal_event: string | null;
  }[];
  const facts = (await gatewayJson(`/memory/facts?q=${ids.factQuery}`)).facts as {
    mem_id: string;
  }[];
  expect(loopRuns.length, "the seeding left loop runs behind").toBeGreaterThan(0);
  expect(facts.length, "the seeded fact is searchable").toBeGreaterThan(0);
  ids.runKey = loopRuns[0].run_key;
  ids.memId = facts[0].mem_id;

  // A comparison the gateway can answer is two runs of the same suite; a mismatch is a 400 the
  // store raises before it scores.
  const paired = evalRuns.find((row, index) =>
    evalRuns.slice(index + 1).some((other) => other.suite === row.suite),
  );
  expect(paired, "the seeded cycle ran one suite more than once").toBeDefined();
  const other = evalRuns.find((row) => row.suite === paired?.suite && row !== paired);
  ids.evalRunId = paired?.eval_run_id ?? "";
  ids.after = ids.evalRunId;
  ids.before = other?.eval_run_id ?? "";

  // The session page follows a stream, so it needs a session whose stream ends: one whose
  // terminal event has already landed.
  const closed = sessions.find((row) => row.terminal_event !== null);
  expect(closed, "the seeding ran an eval to completion").toBeDefined();
  ids.sessionId = closed?.session_id ?? "";

  // And the episodes page needs the session the ad-hoc run filed its episode under, which is
  // asked of the gateway rather than assumed from the order of the listing.
  for (const row of sessions) {
    const held = (await gatewayJson(`/memory/episodes/${row.session_id}`)).episodes as unknown[];
    if (held.length > 0) {
      ids.episodeSessionId = row.session_id;
      break;
    }
  }
  expect(ids.episodeSessionId, "the ad-hoc run filed an episode").not.toBe("");
});

test("a walk of every page writes nothing, reaches nothing but the dashboard, and offers no control that writes", async ({
  page,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);

  const walk: Stop[] = [
    { at: "/" },
    { at: "/traces" },
    { at: `/traces?key=${encodeURIComponent(ids.runKey)}` },
    { at: `/traces?session=${encodeURIComponent(ids.sessionId)}`, stream: true },
    // Ticket 206: the two old pages are forwarders; each is opened and lands on `/traces`.
    { at: "/runs", forwardsTo: "/traces" },
    { at: `/runs?key=${encodeURIComponent(ids.runKey)}`, forwardsTo: `/traces?key=${encodeURIComponent(ids.runKey)}` },
    { at: "/sessions?limit=50", forwardsTo: "/traces?limit=50" },
    {
      at: `/sessions?session=${encodeURIComponent(ids.sessionId)}`,
      forwardsTo: `/traces?session=${encodeURIComponent(ids.sessionId)}`,
      stream: true,
    },
    // HISTORY's resume: the session replayed and streamed to its close, and nothing sent.
    { at: `/console?session=${encodeURIComponent(ids.sessionId)}`, stream: "console", sanctioned: /^button (Sidechat|Send)$/ },
    { at: "/evals" },
    { at: `/evals?id=${encodeURIComponent(ids.evalRunId)}` },
    { at: `/evals?before=${ids.before}&after=${ids.after}` },
    { at: `/evals?cycle=${encodeURIComponent(ids.improvementRunId)}` },
    // Ticket 207: the two old pages are forwarders; each is opened and lands on `/evals`.
    {
      at: `/evals/compare?before=${ids.before}&after=${ids.after}`,
      forwardsTo: `/evals?before=${ids.before}&after=${ids.after}`,
    },
    { at: "/improve?limit=50", forwardsTo: "/evals?limit=50" },
    {
      at: `/improve?id=${encodeURIComponent(ids.improvementRunId)}`,
      forwardsTo: `/evals?cycle=${encodeURIComponent(ids.improvementRunId)}`,
    },
    { at: "/memory" },
    { at: `/memory?q=${ids.factQuery}` },
    { at: `/memory?id=${encodeURIComponent(ids.memId)}` },
    { at: `/memory?session=${encodeURIComponent(ids.episodeSessionId)}` },
    // Ticket 208: the three old pages are forwarders; each is opened and lands on `/memory`.
    { at: "/memory/procedures", forwardsTo: "/memory#procedures" },
    { at: `/memory/facts?q=${ids.factQuery}`, forwardsTo: `/memory?q=${ids.factQuery}` },
    {
      at: `/memory/facts?id=${encodeURIComponent(ids.memId)}`,
      forwardsTo: `/memory?id=${encodeURIComponent(ids.memId)}`,
    },
    {
      at: `/memory/episodes?session=${encodeURIComponent(ids.episodeSessionId)}`,
      forwardsTo: `/memory?session=${encodeURIComponent(ids.episodeSessionId)}`,
    },
    // Loaded, refetched and its drawers opened like every page; its key form is never submitted.
    { at: "/models", sanctioned: /^button Save key$/ },
  ];

  // Every page the tree serves is walked. The templates are read off `app/`, so a page added
  // later is either walked or named here as missed.
  const templates = routeTemplates();
  const missed = templates.filter(
    (template) =>
      !walk.some((stop) => templateMatches(template, new URL(stop.at, DASHBOARD_URL).pathname)),
  );
  expect(missed, "pages of app/ this walk never opened").toEqual([]);
  expect(walk.length).toBeGreaterThanOrEqual(templates.length);

  // The ledger before the walk, with nothing but the CLI having touched it.
  const before = snapshot();

  await watchNetwork(page);
  for (const stop of walk) {
    await walkTo(page, stop);
    writeEvidence();
  }

  // Where a write would be tempting - a candidate sitting undecided on an Improvement page - the
  // command is there to copy instead. Asserted on the two pages FR-57 names, after the walk, so
  // a page that dropped it fails here rather than silently passing the enumeration above.
  for (const where of ["/evals", `/evals?cycle=${encodeURIComponent(ids.improvementRunId)}`]) {
    at = where;
    await page.goto(`${DASHBOARD_URL}${where}`);
    await settle(page);
    const copy = page.getByRole("button", { name: COPY_COMMAND });
    await expect(copy.first(), `${where} offers the CLI command to copy`).toBeVisible();
    const commands = await page.getByTestId("cli-command-text").allTextContents();
    expect(
      commands.some((command) => command.startsWith("harnessed ")),
      `${where} spells a harnessed command`,
    ).toBe(true);
  }
  at = "(after the walk)";
  writeEvidence();

  // The ledger after all of it. Same `user_version`, same row counts, table for table.
  const after = snapshot();
  writeFileSync(
    evidencePath(TICKET, "read-ledger-counts.txt"),
    [
      `FR-78, ticket ${TICKET}: the ledger before and after the read walk`,
      `read through harnessed.memory.db.connect_readonly under ${harnessHome()}`,
      "",
      "before:",
      before,
      "",
      "after:",
      after,
      "",
      `unchanged: ${before === after}`,
      "",
    ].join("\n"),
    "utf8",
  );

  const found = violations(requests);
  expect(requests.length, "the walk made requests at all").toBeGreaterThan(0);
  assertReadOnly(found);
  expect(after, "PRAGMA user_version and every table's row count across the walk").toBe(before);
});

/**
 * The check, proven able to fail.
 *
 * Both halves are injected by the spec rather than by the dashboard: a `POST` to the dashboard's
 * own origin and a `GET` to an origin it has no business asking anything of. Neither leaves the
 * browser - the hook aborts them - so this test writes nothing either, and what it proves is that
 * the log records them and that `assertReadOnly` throws on what it recorded.
 */
test("ticket 204: the first Recent activity row opens its own exchange in the console", async ({ page }) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  await page.goto(`${DASHBOARD_URL}/`);
  await settle(page);

  const row = page.getByTestId("activity-list").getByRole("link").first();
  await expect(row).toHaveAttribute("href", /^\/console\?session=[^#]+#exchange-\d+$/);
  await row.click();

  await expect(page).toHaveURL(/\/console\?session=[^#]+#exchange-\d+$/);
  const anchor = new URL(page.url()).hash.slice(1);
  const landed = page.locator(`[data-testid="exchange"][id="${anchor}"]`);
  await expect(landed).toBeFocused({ timeout: SETTLE_TIMEOUT_MS });
  await page.screenshot({ path: evidencePath("204", "activity-deep-link.png"), fullPage: true });
});

test("ticket 216: the Sidechat is present under a messages read answered 500", async ({ page }) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  // The one messages read is stubbed, so the ledger is untouched and the walk's "0 rows changed" holds.
  await page.route(/\/v1\/sessions\/[^/]+\/messages$/, (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: "InternalError", message: "stubbed for ticket 216" }),
    }),
  );
  await page.goto(`${DASHBOARD_URL}/console?session=${encodeURIComponent(ids.sessionId)}`);
  await expect(page.getByRole("alert").first()).toBeVisible({ timeout: SETTLE_TIMEOUT_MS });

  await expect(page.getByTestId("sidechat")).toHaveCount(1);
  await expect(page.getByRole("main").locator("textarea")).toBeEnabled();
  await page.screenshot({ path: evidencePath("216", "sidechat-error.png"), fullPage: true });
});

test("ticket 206: /traces holds both listings, and /sessions forwards to it", async ({ page }) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  await page.goto(`${DASHBOARD_URL}/traces`);
  await settle(page);
  await expect(page.getByRole("region", { name: "Sessions" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Runs" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Pages" });
  await expect(nav.getByRole("link", { name: "Traces" })).toBeVisible();
  await expect(nav.getByRole("link", { name: /^(Runs|Sessions)$/ })).toHaveCount(0);
  await page.screenshot({ path: evidencePath("206", "traces.png"), fullPage: true });

  await page.goto(`${DASHBOARD_URL}/sessions?session=${encodeURIComponent(ids.sessionId)}`);
  await expect(page).toHaveURL(`${DASHBOARD_URL}/traces?session=${encodeURIComponent(ids.sessionId)}`);
  await settle(page);
  await page.screenshot({ path: evidencePath("206", "forward-sessions.png"), fullPage: true });
});

test("ticket 207: /evals holds runs, Compare and Improvement, and the old pages forward", async ({ page }) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  await page.goto(`${DASHBOARD_URL}/evals`);
  await settle(page);
  await expect(page.locator("#compare")).toBeVisible();
  await expect(page.locator("#improvement")).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Pages" });
  await expect(nav.getByRole("link", { name: "Evals" })).toBeVisible();
  await expect(nav.getByRole("link", { name: /^(Compare|Improvement)$/ })).toHaveCount(0);
  await page.screenshot({ path: evidencePath("207", "evals.png"), fullPage: true });
});

test("ticket 208: /memory holds facts, procedures and episodes, and the old pages forward", async ({ page }) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  await page.goto(`${DASHBOARD_URL}/memory`);
  await settle(page);
  for (const id of ["facts", "procedures", "episodes"]) await expect(page.locator(`#${id}`)).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Pages" });
  await expect(nav.getByRole("link", { name: "Memory" })).toBeVisible();
  await expect(nav.getByRole("link", { name: /^(Procedures|Facts)$/ })).toHaveCount(0);
  await page.screenshot({ path: evidencePath("208", "memory.png"), fullPage: true });
});

test("ticket 251: pressing the theme toggle sends nothing and the walk still finds 0 writes", async ({ page }) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  await watchNetwork(page);
  await page.goto(`${DASHBOARD_URL}/`);
  await settle(page);
  const toggle = page.getByRole("navigation", { name: "Pages" }).getByRole("button", { name: /^Switch to (light|dark) theme$/ });
  await expect(toggle).toBeVisible();
  // The watched stream replays its session's frames after the panels settle, and each refetch signal
  // re-reads the Overview's panels; let that finish so the listener below hears only the press.
  await page.waitForTimeout(2000);
  await settle(page);
  const pressed: string[] = [];
  page.on("request", (request) => pressed.push(`${request.method()} ${request.url()}`));
  await toggle.click();
  await toggle.click();
  await page.waitForTimeout(300);
  assertReadOnly(violations(requests));
  expect(pressed, "requests a press made").toEqual([]);
});

test("the read walk's check fires on a POST to /v1, a request to the control app and another origin", async ({ page }) => {
  const seeded: Logged[] = [];
  await page.route("**/*", async (route) => {
    const request = route.request();
    const entry = { method: request.method(), url: request.url(), page: "(seeded)" };
    seeded.push(entry);
    if (new URL(request.url()).origin !== ORIGIN) return route.abort("blockedbyclient");
    if (forbiddenMethod(entry) || toControl(entry)) return route.abort("blockedbyclient");
    return route.continue();
  });

  await page.goto(`${DASHBOARD_URL}/`);
  await settle(page);
  expect(violations(seeded), "the walk's own log is clean before anything is seeded").toEqual({
    nonGet: [],
    control: [],
    foreign: [],
  });

  await page.evaluate(
    async ([write, control, foreign]) => {
      await fetch(write, { method: "POST", body: "{}" }).catch(() => undefined);
      await fetch(control, { method: "GET" }).catch(() => undefined);
      await fetch(foreign, { method: "GET" }).catch(() => undefined);
    },
    [`${ORIGIN}/v1/health`, `${ORIGIN}${CONTROL_PATH}runs`, FOREIGN],
  );

  const found = violations(seeded);
  expect(found.nonGet.map((entry) => entry.method)).toEqual(["POST"]);
  expect(found.control.map((entry) => entry.method)).toEqual(["GET"]);
  expect(found.foreign.map((entry) => entry.url)).toEqual([FOREIGN]);
  expect(() => assertReadOnly(found)).toThrow();

  writeFileSync(
    evidencePath(TICKET, "check-fires.txt"),
    [
      `FR-78, ticket ${TICKET}: the read walk's predicate, on a seeded write`,
      "",
      ...seeded.map((entry) => `${entry.method} ${entry.url}`),
      "",
      `non-GET: ${found.nonGet.length}, control app: ${found.control.length}, other origins: ${found.foreign.length}`,
      "assertReadOnly threw: true",
      "",
    ].join("\n"),
    "utf8",
  );
});
