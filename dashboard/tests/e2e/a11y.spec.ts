/**
 * FR-61 end to end: every page is usable by keyboard, in both themes, and at 400px.
 *
 * `ux/design.md` ("Motion & accessibility rules") and `ux/experience.md` ("Accessibility
 * requirements") make seven separate claims about the whole window. This spec is seven separate
 * measurements of them, taken against the live loopback gateway of decision 30, and not one of
 * them reads a sentence the dashboard prints about itself (MUST rule 12):
 *
 *   1. **Axe.** `@axe-core/playwright` runs on every page of `app/**` in three colour states -
 *      dark, light by `prefers-color-scheme`, and light by `[data-theme="light"]` - and 0 of its
 *      findings may be `serious` or `critical`.
 *   2. **The tab order.** Tab is pressed from the top of a page and the elements that take focus
 *      are recorded in the order they took it. The first four stops are the documented path -
 *      skip link, nav, pill, main - and every stop inside `main` must then run in visual order,
 *      which is read off the boxes the browser laid out rather than assumed from the markup.
 *      Every stop must wear a 2px ring.
 *   3. **The drawer.** Tab at the end of an open drawer must land back inside it, and Esc must
 *      close it and give focus back to the chip that opened it. Both are done, not asserted about.
 *   4. **Reduced motion.** Under `prefers-reduced-motion: reduce` every computed transition and
 *      animation duration on the page is 0s, and the event-arrival fade is absent from a row that
 *      really did arrive on a live stream - so a run is started and watched, not simulated.
 *   5. **The marks.** Every pass, fail, warn and unknown mark on the walk is read for its word:
 *      a mark whose only signal is its colour has no text and fails here.
 *   6. **400px.** Each page is rendered at 400x800. The sidebar must be a menu button, the
 *      document must not scroll horizontally, and a table wider than the viewport must be
 *      scrollable inside its own panel rather than pushing the page wide.
 *   7. **Contrast.** The token values are read out of the live `:root` in each theme and the
 *      ratios are computed here, in the test - never in `app/`, `components/` or `lib/`
 *      (decision 32). Text and figures clear 4.5:1; the focus ring clears 3:1.
 *
 * The pages are not a list kept in step by hand: the route templates are read off
 * `app/**\/page.tsx`, as ticket 114's walk reads them, and a page added later is either covered
 * or named here as missed. Seeding happens once in `beforeAll`, through the CLI as an operator
 * runs it, so every id spelled into a URL is an id the gateway already holds.
 */
import { spawn, spawnSync } from "node:child_process";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

import { drawerFindings, pageFindings } from "./endpoint-walk";
import { DASHBOARD_ROOT, DASHBOARD_URL, GATEWAY_URL, HOME_VARIABLE, REPO_ROOT, evidencePath } from "./harness";

const TICKET = "115";

/** The CLI, the browser and two servers share one machine, so each walk gets its own ceiling. */
const WALK_TIMEOUT_MS = 300_000;

/**
 * The axe walk's budget scales with what it walks (ticket 266): one page is opened, settled and
 * read in five colour states (three themes, two solid fallbacks). The slower Windows PC ran out
 * of a flat 300 s ceiling (255's third close run: more than 300 s for the whole walk, no
 * violation reported), so the ceiling is this per-page figure times the page count.
 */
const AXE_PER_PAGE_MS = 40_000;

/** How long one page is given to settle, and a stream to deliver its first live frame. */
const SETTLE_TIMEOUT_MS = 60_000;

/** The two findings FR-61 forbids outright. `minor` and `moderate` are reported, not failed. */
const FORBIDDEN_IMPACTS = new Set(["serious", "critical"]);

/** The narrow viewport `ux/experience.md` names, and the height that goes with it. */
const NARROW = { width: 400, height: 800 };

/** The ring `ux/design.md` specifies: 2px, never removed. */
const RING_WIDTH = "2px";

/** WCAG's two floors: text and figures, and the ring around a control. */
const TEXT_RATIO = 4.5;
const RING_RATIO = 3;

/** A figure that opens a provenance drawer: `Value` is its own trigger from v5 (`D-4`, `D-9`). */
const FIGURE_TRIGGER = "button[data-figure]";

/**
 * Two boxes are on the same visual row when they overlap vertically. A table row is 40px tall and
 * its cells are middle-aligned, so two controls an operator reads left to right can sit twenty
 * pixels apart in `top` and still be the same line; a fixed band would call that a new row and
 * every row would read as running backwards.
 */
function sameRow(a: Box, b: Box): boolean {
  return a.top < b.bottom && b.top < a.bottom;
}

/**
 * The cell-like boxes a stop can sit in: a table cell, a list item, or the `role="group"` a
 * `StatTile` renders. These are the boxes an operator reads *as* the row - see `forwards`.
 */
const CELL = 'td, th, li, [role="group"]';

/** Every element that can take focus from a Tab. Matches `ProvenanceDrawer`'s own trap list. */
const FOCUSABLE = "a[href], button, input, select, textarea, summary, [tabindex]";

/** The same box, to the pixel: what makes two stops neighbours inside one cell rather than two. */
function sameBox(a: Box, b: Box): boolean {
  return a.top === b.top && a.left === b.left && a.bottom === b.bottom && a.right === b.right;
}

/** Does `outer` fully enclose `inner` (its own box included)? */
function encloses(outer: Box, inner: Box): boolean {
  return outer.top <= inner.top && outer.left <= inner.left && outer.bottom >= inner.bottom && outer.right >= inner.right;
}

/**
 * Are these two cells one *inside* the other, rather than two side by side?
 *
 * `CELL` matches `li` at every depth an operator's eye never treats as a row of its own: a
 * `RecordView` array wraps each item in a bare `li` (`D-10`), and a `TracePanel` turn is itself an
 * `li` (`styles.turn`) holding, further down its own flow, a tool call's `li` (`styles.call`) or a
 * request's `RecordView` `li`. A td/th pair, and the `role="group"` `StatTile` renders, are never
 * nested this way - only ever siblings - so this only ever fires for a list nested in a list.
 */
function cellsNested(a: Box, b: Box): boolean {
  return !sameBox(a, b) && (encloses(a, b) || encloses(b, a));
}

/**
 * Does Tab moving from `previous` to `stop` go the way the eye goes?
 *
 * The comparison is between the *cells* the two controls sit in, and only falls back to the
 * controls' own boxes for two stops inside one cell - or one cell nested inside the other. A row
 * of `StatTile`s is the case that forces the cell comparison: the tiles are equal-height grid
 * items an operator reads left to right, but a figure long enough to wrap puts its `SourceChip` on
 * a second line inside its own tile, so that chip's box sits a line below the boxes of the chips
 * either side of it. Compared as bare boxes the middle tile is a row of its own and the third tile
 * then reads as running backwards, while nothing on the screen ran backwards at all. Compared as
 * cells the three tiles are the one row they look like, and a tab order that really did jump - a
 * chip reached before the figure it belongs to, a row reached before the row above it - still
 * fails, because those move between cells.
 *
 * A *nested* cell breaks that same `sameRow` reading, the opposite way: an outer `li` (a turn)
 * that holds an inner `li` (a call, or a `RecordView` array item) further down its own flow always
 * vertically overlaps it - containment guarantees `sameRow` - so comparing the two cells' `left`
 * edges answers a question ("which column?") that was never asked of two boxes one of which is
 * the other's ancestor. Two cells where neither contains the other are always true siblings (a
 * table's `td`s, a `StatTile` row's groups), so this falls back to the controls' own boxes exactly
 * where it falls back for one shared cell - visual order, read off the stops themselves.
 */
function movesForward(previous: Stop, stop: Stop): boolean {
  // Between two panels the panels are what the eye reads as the row: the Overview lays six out
  // in a grid (ticket 134), so the last control of one panel is followed by the first of the
  // panel beside it, which starts higher up the page. Inside one panel the cells decide.
  // Widening (e), ratified 2026-10-11 (Q7), ticket 295: panel boxes are compared only when BOTH
  // stops sit inside a `PanelFrame`; a stop outside one never reaches this branch.
  if (previous.panel !== null && stop.panel !== null && !sameBox(previous.panel, stop.panel)) {
    return sameRow(previous.panel, stop.panel)
      ? stop.panel.left >= previous.panel.left
      : stop.panel.top > previous.panel.top;
  }
  if (sameBox(previous.cell, stop.cell) || cellsNested(previous.cell, stop.cell)) {
    return sameRow(previous.box, stop.box)
      ? stop.box.left >= previous.box.left
      : stop.box.top > previous.box.top;
  }
  return sameRow(previous.cell, stop.cell)
    ? stop.cell.left >= previous.cell.left
    : stop.cell.top > previous.cell.top;
}

/** A synthetic stop for the seeded cases of ticket 295: a control at `at`, in a panel or in none. */
function seededStop(at: Box, panel: Box | null): Stop {
  return {
    tag: "button",
    name: "seeded",
    region: "main",
    box: at,
    cell: at,
    panel,
    ring: { width: RING_WIDTH, style: "solid", colour: "rgb(0, 0, 0)" },
  };
}

// Widening (e), ticket 295: the walk compares panel boxes only for stops inside a PanelFrame. Run
// against synthetic stops, so no page has to be broken to see each seeded violation fail.
test("the tab walk compares panel boxes only for stops inside a PanelFrame", () => {
  const box = (top: number, left: number): Box => ({ top, left, bottom: top + 40, right: left + 100 });
  const left = box(0, 0);
  const right = box(0, 300);
  // Two panels side by side: left to right is forward, right to left is a jump.
  expect(movesForward(seededStop(box(10, 10), left), seededStop(box(10, 310), right))).toBe(true);
  expect(movesForward(seededStop(box(10, 310), right), seededStop(box(10, 10), left))).toBe(false);
  // A focus jump outside any panel still fails, on the controls' own boxes.
  const outsideA = seededStop(box(200, 300), null);
  const outsideB = seededStop(box(100, 0), null);
  expect(movesForward(outsideA, outsideB)).toBe(false);
  // One stop inside a panel and one outside: the panel boxes are not compared, the cells are.
  expect(movesForward(seededStop(box(10, 310), right), seededStop(box(10, 10), null))).toBe(false);
  expect(movesForward(seededStop(box(10, 10), left), seededStop(box(60, 10), null))).toBe(true);
  writeFileSync(
    evidencePath("295", "panel-box-widening.txt"),
    "panel boxes compared only for stops inside a PanelFrame; seeded jumps outside one fail\n",
    "utf8",
  );
});

/** The lines written to `evidence/191/endpoint-scan-a11y.txt`: the DOM scan's count per page. */
const endpointLog: string[] = [];

/** The lines written to `evidence/115/*.txt`. */
const axeLog: string[] = [];

/** The ids the walks spell into a URL, all of them read from the gateway in `beforeAll`. */
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

// ---- The harness's own home and the CLI ---------------------------------------------------------

function home(): string {
  const directory = process.env[HOME_VARIABLE];
  if (!directory) throw new Error(`${HOME_VARIABLE} is not set; playwright.config.ts sets it`);
  return directory;
}

/** One CLI call under the run's temporary home and the fake provider, as an operator makes it. */
function cli(...argv: string[]): { status: number | null; stdout: string; stderr: string } {
  const done = spawnSync("uv", ["run", "harnessed", ...argv], {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home(), HARNESSED_PROVIDER: "fake" },
    encoding: "utf8",
    shell: false,
  });
  return { status: done.status, stdout: done.stdout ?? "", stderr: done.stderr ?? "" };
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

/** Every page of the route map, with the seeded ids in place. Built after `beforeAll` has run. */
function pages(): string[] {
  return [
    "/",
    "/traces",
    `/traces?key=${encodeURIComponent(ids.runKey)}`,
    `/traces?session=${encodeURIComponent(ids.sessionId)}`,
    // Ticket 206: the two forwarders, each of which must land on `/traces` (see `open`).
    "/runs",
    "/sessions?limit=50",
    `/console?session=${encodeURIComponent(ids.sessionId)}`,
    "/evals",
    `/evals?id=${encodeURIComponent(ids.evalRunId)}`,
    `/evals?before=${ids.before}&after=${ids.after}`,
    `/evals?cycle=${encodeURIComponent(ids.improvementRunId)}`,
    // Ticket 207: the two forwarders, each of which must land on `/evals` (see `open`).
    `/evals/compare?before=${ids.before}&after=${ids.after}`,
    "/improve?limit=50",
    "/memory",
    `/memory?q=${ids.factQuery}`,
    `/memory?id=${encodeURIComponent(ids.memId)}`,
    `/memory?session=${encodeURIComponent(ids.episodeSessionId)}`,
    // Ticket 208: the three forwarders, each of which must land on `/memory` (see `open`).
    "/memory/procedures",
    `/memory/facts?q=${ids.factQuery}`,
    `/memory/facts?id=${encodeURIComponent(ids.memId)}`,
    `/memory/episodes?session=${encodeURIComponent(ids.episodeSessionId)}`,
    "/models",
  ];
}

/** Every page of `app/` that the list above never opens. Empty is the only passing answer. */
function missedPages(walked: string[]): string[] {
  return routeTemplates().filter(
    (template) =>
      !walked.some((at) => templateMatches(template, new URL(at, DASHBOARD_URL).pathname)),
  );
}

/** A file name for a page, so a screenshot is named after the URL it shows. */
function slug(at: string): string {
  const url = new URL(at, DASHBOARD_URL);
  const name = `${url.pathname}${url.search}`.replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "");
  return name === "" ? "home" : name;
}

// ---- One stop -----------------------------------------------------------------------------------

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

/**
 * Wait for every running transition to land. Controls transition colour at `motion-micro`
 * (FR-162), so axe read straight after a colour state is put on the page would measure a colour
 * halfway between two themes; contrast is a claim about the state the page settles in.
 */
async function atRest(page: Page): Promise<void> {
  await page.evaluate(() =>
    Promise.allSettled(
      document
        .getAnimations()
        .filter((animation) => animation instanceof CSSTransition)
        .map((animation) => animation.finished),
    ),
  );
}

/** Open a page and let every panel finish. A page that does not answer 200 fails here. */
async function open(page: Page, at: string): Promise<void> {
  const response = await page.goto(`${DASHBOARD_URL}${at}`);
  expect(response?.status(), `GET ${at}`).toBe(200);
  await settle(page);
  if (/^\/(runs|sessions)(\?|$)/.test(at)) {
    // A forwarder lands on `/traces` with the query it was opened with.
    await expect(page, `${at} forwards`).toHaveURL(`${DASHBOARD_URL}/traces${new URL(at, DASHBOARD_URL).search}`);
  }
  if (/^\/(evals\/compare|improve)(\?|$)/.test(at)) {
    // A forwarder lands on `/evals`; `/improve?id=` renames its parameter to `cycle`.
    const search = new URL(at, DASHBOARD_URL).search.replace(/^\?id=/, "?cycle=");
    await expect(page, `${at} forwards`).toHaveURL(`${DASHBOARD_URL}/evals${search}`);
  }
  if (/^\/memory\/(facts|procedures|episodes)(\?|$)/.test(at)) {
    // A forwarder lands on `/memory`; the procedures page lands on its section.
    const url = new URL(at, DASHBOARD_URL);
    const hash = url.pathname === "/memory/procedures" ? "#procedures" : "";
    await expect(page, `${at} forwards`).toHaveURL(`${DASHBOARD_URL}/memory${url.search}${hash}`);
  }
}

/**
 * A session page holds a stream open; letting it close keeps the next measurement still. The wait
 * is not asserted on - the walk only needs the page to stop changing under it.
 */
async function quiesce(page: Page, at: string): Promise<void> {
  if (!/^\/sessions\/[^/]+$/.test(new URL(at, DASHBOARD_URL).pathname)) return;
  await expect(page.getByTestId("stream-status")).toHaveText(/^Done/, {
    timeout: SETTLE_TIMEOUT_MS,
  });
}

// ---- Colour: the arithmetic lives here, never in app/, components/ or lib/ (decision 32) --------

type Rgb = { r: number; g: number; b: number };

/** `#rrggbb`, `#rgb` or `rgb(r g b)` as the browser resolved it, in 0-255 channels. */
function parseColour(value: string): Rgb {
  const text = value.trim();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(text);
  if (hex) {
    const digits =
      hex[1].length === 3
        ? [...hex[1]].map((digit) => `${digit}${digit}`)
        : [hex[1].slice(0, 2), hex[1].slice(2, 4), hex[1].slice(4, 6)];
    const [r, g, b] = digits.map((pair) => parseInt(pair, 16));
    return { r, g, b };
  }
  const channels = text.match(/[\d.]+/g);
  if (channels === null || channels.length < 3) throw new Error(`not a colour: ${value}`);
  const [r, g, b] = channels.slice(0, 3).map((channel) => parseFloat(channel));
  return { r, g, b };
}

/** WCAG 2.1 relative luminance of an opaque colour. */
function luminance({ r, g, b }: Rgb): number {
  const channel = (raw: number): number => {
    const scaled = raw / 255;
    return scaled <= 0.03928 ? scaled / 12.92 : Math.pow((scaled + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG 2.1 contrast ratio, lighter over darker. */
function contrast(foreground: string, background: string): number {
  const a = luminance(parseColour(foreground));
  const b = luminance(parseColour(background));
  const [light, dark] = a > b ? [a, b] : [b, a];
  return (light + 0.05) / (dark + 0.05);
}

/** Two decimals, without asking the page for them. */
function ratioText(ratio: number): string {
  return `${Math.round(ratio * 100) / 100}:1`;
}

// ---- The three colour states a page is measured in ----------------------------------------------

type Theme = { name: string; scheme: "dark" | "light"; attribute: "dark" | "light" | null };

/**
 * Dark is the lead. Light arrives two ways and `app/tokens.css` restates the same names for each,
 * so both are measured: the media query, and the explicit `[data-theme="light"]` over a dark
 * system - which is the case that would break if the attribute block were ever dropped.
 */
const THEMES: readonly Theme[] = [
  { name: "dark", scheme: "dark", attribute: null },
  { name: "light (prefers-color-scheme)", scheme: "light", attribute: null },
  { name: 'light ([data-theme="light"])', scheme: "dark", attribute: "light" },
];

/** Put the page into one colour state. The attribute is set on `:root`, where tokens.css reads it. */
async function wear(page: Page, theme: Theme): Promise<void> {
  await page.emulateMedia({ colorScheme: theme.scheme });
  await page.evaluate((attribute) => {
    if (attribute === null) document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", attribute);
  }, theme.attribute);
}

// ---- Seeding: through the CLI, before anything is measured --------------------------------------

test.beforeAll(async () => {
  test.setTimeout(WALK_TIMEOUT_MS);

  // An improvement cycle: a promotion, an active procedure and more eval runs, so the cycle page
  // has an id and two runs of one suite exist for the comparison.
  const improved = cli("improve", "run", "--json");
  expect(improved.status, improved.stderr).toBe(0);
  ids.improvementRunId = (JSON.parse(improved.stdout) as { improvement_run_id: string })
    .improvement_run_id;

  // One ad-hoc run, the only thing that files an episode, so the episodes page reads a session
  // that has one. "ping" is the fake corpus's scope fallback.
  expect(cli("run", "ping").status, "an ad-hoc run under the fake provider").toBe(0);

  // One fact, so the facts pages render a row rather than an empty state. A bare word: a hyphen
  // is an operator in the FTS query the search route runs (ticket 114).
  ids.factQuery = "keyboard";
  expect(
    cli("memory", "add", `ticket ${TICKET} seeded this ${ids.factQuery} fact for the walk`).status,
    "one seeded fact",
  ).toBe(0);

  const loopRuns = (await gatewayJson("/runs?limit=50")).runs as { run_key: string }[];
  const evalRuns = (await gatewayJson("/eval/runs")).runs as { eval_run_id: string; suite: string }[];
  const sessions = (await gatewayJson("/sessions?limit=100")).sessions as {
    session_id: string;
    terminal_event: string | null;
  }[];
  const facts = (await gatewayJson(`/memory/facts?q=${ids.factQuery}`)).facts as { mem_id: string }[];
  expect(loopRuns.length, "the seeding left loop runs behind").toBeGreaterThan(0);
  expect(facts.length, "the seeded fact is searchable").toBeGreaterThan(0);
  ids.runKey = loopRuns[0].run_key;
  ids.memId = facts[0].mem_id;

  // A comparison the gateway can answer is two runs of the same suite.
  const paired = evalRuns.find((row, index) =>
    evalRuns.slice(index + 1).some((other) => other.suite === row.suite),
  );
  expect(paired, "the seeded cycle ran one suite more than once").toBeDefined();
  const other = evalRuns.find((row) => row.suite === paired?.suite && row !== paired);
  ids.evalRunId = paired?.eval_run_id ?? "";
  ids.after = ids.evalRunId;
  ids.before = other?.eval_run_id ?? "";

  const closed = sessions.find((row) => row.terminal_event !== null);
  expect(closed, "the seeding ran an eval to completion").toBeDefined();
  ids.sessionId = closed?.session_id ?? "";

  for (const row of sessions) {
    const held = (await gatewayJson(`/memory/episodes/${row.session_id}`)).episodes as unknown[];
    if (held.length > 0) {
      ids.episodeSessionId = row.session_id;
      break;
    }
  }
  expect(ids.episodeSessionId, "the ad-hoc run filed an episode").not.toBe("");
});

// ---- 1. Axe, on every page, in both themes ------------------------------------------------------

test("axe finds no serious or critical violation on any page, in dark or in light", async ({
  page,
}) => {
  const walked = pages();
  test.setTimeout(Math.max(WALK_TIMEOUT_MS, AXE_PER_PAGE_MS * walked.length));
  expect(missedPages(walked), "pages of app/ this run never opened").toEqual([]);

  const offending: string[] = [];
  axeLog.push(
    `FR-61, ticket ${TICKET}: axe-core on every page, in three colour states`,
    `serious and critical are failures; minor and moderate are reported`,
    "",
  );

  for (const at of walked) {
    await open(page, at);
    await quiesce(page, at);
    // FR-126: the page as it stands, then with a figure's drawer open, draws no endpoint string.
    const drawn = [...(await pageFindings(page)), ...(await drawerFindings(page))];
    endpointLog.push(`${at}: ${drawn.length} endpoint strings`);
    expect(drawn, `endpoint strings drawn on ${at}`).toEqual([]);
    for (const theme of THEMES) {
      await wear(page, theme);
      // Ticket 251: the theme toggle is in the sidebar under every colour state axe reads.
      await expect(
        page.getByRole("navigation", { name: "Pages" }).getByRole("button", { name: /^Switch to (light|dark) theme$/ }),
        `${at} [${theme.name}]: the theme toggle`,
      ).toHaveCount(1);
      await atRest(page);
      const { violations } = await new AxeBuilder({ page }).analyze();
      const counted = new Map<string, number>();
      for (const violation of violations) {
        const impact = violation.impact ?? "unknown";
        counted.set(impact, (counted.get(impact) ?? 0) + 1);
        if (!FORBIDDEN_IMPACTS.has(impact)) continue;
        offending.push(
          `${at} [${theme.name}] ${impact} ${violation.id}: ${violation.help} (${violation.nodes.length} node(s)) ${violation.nodes[0]?.target.join(" ")}`,
        );
      }
      const summary = [...counted.entries()].map(([impact, count]) => `${impact}=${count}`);
      axeLog.push(
        `${at} [${theme.name}]: ${violations.length} violation(s)${summary.length === 0 ? "" : ` (${summary.join(", ")})`}`,
        ...violations.map((violation) => `    - ${violation.impact ?? "unknown"} ${violation.id}`),
      );
    }
    // FR-158: the solid fallback is the same page with the glass off; axe reads it in both themes.
    for (const scheme of ["dark", "light"] as const) {
      const cdp = await page.context().newCDPSession(page);
      await cdp.send("Emulation.setEmulatedMedia", {
        features: [
          { name: "prefers-color-scheme", value: scheme },
          { name: "prefers-reduced-transparency", value: "reduce" },
        ],
      });
      await page.evaluate(() => document.documentElement.removeAttribute("data-theme"));
      await atRest(page);
      const { violations } = await new AxeBuilder({ page }).analyze();
      for (const violation of violations) {
        if (!FORBIDDEN_IMPACTS.has(violation.impact ?? "unknown")) continue;
        offending.push(`${at} [solid fallback, ${scheme}] ${violation.impact} ${violation.id}: ${violation.help}`);
      }
      axeLog.push(`${at} [solid fallback, ${scheme}]: ${violations.length} violation(s)`);
      await cdp.send("Emulation.setEmulatedMedia", { features: [] });
    }
    await wear(page, THEMES[0]);
    writeFileSync(evidencePath(TICKET, "axe.txt"), [...axeLog, ""].join("\n"), "utf8");
  }

  writeFileSync(
    evidencePath(TICKET, "axe.txt"),
    [...axeLog, "", `serious or critical: ${offending.length}`, ...offending, ""].join("\n"),
    "utf8",
  );
  writeFileSync(
    evidencePath("191", "endpoint-scan-a11y.txt"),
    ["FR-126, ticket 191: the DOM scan of every page of the a11y walk", "", ...endpointLog, ""].join("\n"),
    "utf8",
  );
  expect(offending, "serious or critical axe violations").toEqual([]);
});

// ---- 2. The tab order, and the ring on every stop -----------------------------------------------

/** One tab stop, as the page described the element that took focus. */
type Stop = {
  tag: string;
  name: string;
  /** Where it sits: `nav`, `header`, `main` or the skip link itself. */
  region: string;
  box: Box;
  /** The box of the cell-like element it sits in, or its own where there is none. */
  cell: Box;
  /** The box of the outermost panel (`section`) in `main` it sits in, or `null` outside one. */
  panel: Box | null;
  /** The `:focus-visible` ring as the browser computed it while this element held focus. */
  ring: { width: string; style: string; colour: string };
};

/**
 * Where a stop sits in the page's layout, with every scrolling ancestor's offset added back.
 *
 * `getBoundingClientRect` answers where an element is on screen *now*, and focusing a control
 * inside a table wider than its panel scrolls that panel: the next control then reports a smaller
 * `left` than the one before it while sitting further right on the line. Adding each ancestor's
 * `scrollLeft` and `scrollTop` back gives a position that does not move as the walk moves, which
 * is the one the eye reads.
 */
type Box = { top: number; left: number; bottom: number; right: number };

/** Read whatever holds focus right now, as a `Stop`. */
async function focused(page: Page): Promise<Stop> {
  return page.evaluate((cellSelector) => {
    const node = document.activeElement;
    if (node === null || node === document.body) {
      return {
        tag: "(none)",
        name: "",
        region: "(none)",
        box: { top: 0, left: 0, bottom: 0, right: 0 },
        cell: { top: 0, left: 0, bottom: 0, right: 0 },
        panel: null,
        ring: { width: "", style: "", colour: "" },
      };
    }
    // Where an element sits in the page's layout, with every scrolling ancestor's offset added
    // back, so a position does not move as the walk scrolls a panel under itself.
    const stable = (of: Element) => {
      const edges = of.getBoundingClientRect();
      let down = 0;
      let across = 0;
      for (let above = of.parentElement; above !== null; above = above.parentElement) {
        down = down + above.scrollTop;
        across = across + above.scrollLeft;
      }
      return {
        top: edges.top + down,
        left: edges.left + across,
        bottom: edges.bottom + down,
        right: edges.right + across,
      };
    };
    // The panel a stop sits in: the outermost `section` below `main`, so a state card nested in
    // a `PanelFrame` (itself a `section`) still reads as its panel.
    let outermost: Element | null = null;
    for (let above = node.parentElement; above !== null; above = above.parentElement) {
      if (above.tagName === "MAIN") break;
      if (above.tagName === "SECTION") outermost = above;
    }
    const style = getComputedStyle(node);
    const region =
      node.classList.toString().includes("skip") || node.getAttribute("href") === "#main"
        ? "skip"
        : (node.closest("nav") && "nav") ||
          (node.closest("header") && "header") ||
          (node.closest("main") && "main") ||
          "(outside)";
    const label = node.getAttribute("aria-label");
    const text = (node.textContent ?? "").replace(/\s+/g, " ").trim();
    return {
      tag: node.tagName.toLowerCase(),
      name: label ?? (text === "" ? (node.getAttribute("title") ?? "") : text),
      region,
      box: stable(node),
      cell: stable(node.closest(cellSelector) ?? node),
      panel: outermost === null ? null : stable(outermost),
      ring: {
        width: style.outlineWidth,
        style: style.outlineStyle,
        colour: style.outlineColor,
      },
    };
  }, CELL);
}

/**
 * Press Tab until the order wraps, or until `limit` presses have been made.
 *
 * The wrap is what ends the walk, not a count: the selector below matches elements the browser
 * will not actually focus - a `display: none` menu button above 400px, a disabled `Open` - so a
 * fixed number of presses runs off the end of the page and comes back round to the skip link,
 * which then reads as the order leaving `main`. The first element to take focus is remembered on
 * the page itself and the walk stops the moment focus returns to it.
 */
async function tabThrough(page: Page, limit: number): Promise<Stop[]> {
  const stops: Stop[] = [];
  await page.evaluate(() => {
    delete (globalThis as { __firstStop?: unknown }).__firstStop;
  });
  for (let press = 0; press < limit; press += 1) {
    await page.keyboard.press("Tab");
    const wrapped = await page.evaluate(() => {
      const held = globalThis as { __firstStop?: Element | null };
      if (held.__firstStop === undefined) {
        held.__firstStop = document.activeElement;
        return false;
      }
      return held.__firstStop === document.activeElement;
    });
    if (wrapped) return stops;
    stops.push(await focused(page));
  }
  return stops;
}

test("Tab runs skip link, nav, pill, main, then every control in main in visual order, each with a 2px ring", async ({
  page,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);

  const lines: string[] = [`FR-61, ticket ${TICKET}: the tab order of every page`, ""];
  const ringless: string[] = [];
  const outOfOrder: string[] = [];

  for (const at of pages()) {
    await open(page, at);
    await quiesce(page, at);
    // Focus starts at the top of the document, so the first Tab is the first stop of the page.
    // A fragment landing (`/memory/procedures` forwards to `/memory#procedures`) moves the
    // browser's sequential focus starting point to its target, so the same page is loaded again
    // without the fragment.
    if (new URL(page.url()).hash !== "") {
      await page.goto(page.url().split("#")[0]);
      await quiesce(page, at);
    }
    await page.evaluate(() => {
      (document.activeElement as HTMLElement | null)?.blur();
    });

    // The ceiling is the page's own count of candidates, not a number chosen here; the walk ends
    // at the wrap well before it, and a page that never wraps fails on the ceiling rather than
    // running forever.
    const reachable = await page.locator(FOCUSABLE).count();
    const stops = (await tabThrough(page, reachable)).filter((stop) => stop.tag !== "(none)");
    expect(stops.length, `${at} offers something to Tab to`).toBeGreaterThan(3);

    // The documented path: skip link, nav, pill, then main.
    expect(stops[0].region, `${at}: the first tab stop`).toBe("skip");
    const regions = stops.map((stop) => stop.region);
    const firstNav = regions.indexOf("nav");
    const firstHeader = regions.indexOf("header");
    const firstMain = regions.indexOf("main");
    expect(firstNav, `${at}: the nav is reached`).toBe(1);
    expect(firstHeader, `${at}: the pill follows the nav`).toBeGreaterThan(firstNav);
    expect(firstMain, `${at}: main follows the pill`).toBeGreaterThan(firstHeader);
    // Ticket 251: the theme toggle is the last stop of the nav, after every nav link.
    const lastNav = regions.lastIndexOf("nav");
    expect(stops[lastNav].tag, `${at}: the last nav stop is a button`).toBe("button");
    expect(stops[lastNav].name, `${at}: the last nav stop is the theme toggle`).toMatch(/^Switch to (light|dark) theme$/);
    expect(
      regions.slice(firstMain).every((region) => region === "main"),
      `${at}: nothing comes back out of main`,
    ).toBe(true);
    expect(
      stops[firstHeader].name,
      `${at}: the top bar stop is the gateway pill`,
    ).toMatch(/gateway|health|\/v1/i);

    // Inside main, the order Tab takes must be the order the eye takes: down, then across.
    const inMain = stops.slice(firstMain);
    for (const [index, stop] of inMain.entries()) {
      if (index === 0) continue;
      const previous = inMain[index - 1];
      if (movesForward(previous, stop)) continue;
      outOfOrder.push(
        `${at}: ${previous.tag} "${previous.name}" at (${previous.box.top}, ${previous.box.left}) in a cell at (${previous.cell.top}, ${previous.cell.left}) then ${stop.tag} "${stop.name}" at (${stop.box.top}, ${stop.box.left}) in a cell at (${stop.cell.top}, ${stop.cell.left})`,
      );
    }

    // And every stop wears the ring, the skip link and the nav included.
    for (const stop of stops) {
      if (stop.ring.width === RING_WIDTH && stop.ring.style !== "none") continue;
      ringless.push(
        `${at}: ${stop.tag} "${stop.name}" ring ${stop.ring.width} ${stop.ring.style} ${stop.ring.colour}`,
      );
    }

    lines.push(
      `${at}  (${stops.length} stop(s))`,
      ...stops.map(
        (stop, index) =>
          `  ${index}. [${stop.region}] ${stop.tag} "${stop.name}" at (${stop.box.top}, ${stop.box.left})-(${stop.box.bottom}, ${stop.box.right}) ring ${stop.ring.width} ${stop.ring.style} ${stop.ring.colour}`,
      ),
      "",
    );
    writeFileSync(evidencePath(TICKET, "tab-order.txt"), [...lines, ""].join("\n"), "utf8");
  }

  writeFileSync(
    evidencePath(TICKET, "tab-order.txt"),
    [
      ...lines,
      `stops without a ${RING_WIDTH} ring: ${ringless.length}`,
      ...ringless,
      `stops out of visual order: ${outOfOrder.length}`,
      ...outOfOrder,
      "",
    ].join("\n"),
    "utf8",
  );
  expect(ringless, `tab stops without a ${RING_WIDTH} focus ring`).toEqual([]);
  expect(outOfOrder, "tab stops that run against the visual order").toEqual([]);
});

// ---- 3. The drawer: Tab stays inside, Esc gives the chip its focus back -------------------------

test("an open drawer keeps Tab inside it, and Esc closes it and returns focus to the chip", async ({
  page,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);

  await open(page, "/traces");
  const chip: Locator = page.locator(FIGURE_TRIGGER).first();
  await expect(chip).toBeVisible();
  const chipHandle = await chip.elementHandle();
  expect(chipHandle, "the chip is identifiable, so focus returning to it is provable").not.toBeNull();

  await chip.focus();
  await expect(chip, "the chip takes focus from the keyboard").toBeFocused();
  await page.evaluate(() => {
    const seen: string[] = [];
    (globalThis as { __seen?: string[] }).__seen = seen;
    for (const kind of ["keydown", "keyup", "click"]) {
      document.addEventListener(
        kind,
        (event) =>
          seen.push(
            `${kind} ${"key" in event ? String(event.key) : ""} on ${(event.target as Element | null)?.tagName ?? "?"}`,
          ),
        true,
      );
    }
  });
  await chip.press("Enter");
  const heard = await page.evaluate(
    () => (globalThis as { __seen?: string[] }).__seen?.join(" | ") ?? "(nothing)",
  );
  const opened = await page.evaluate(() => ({
    dialogs: document.querySelectorAll('[role="dialog"]').length,
    expanded: document.querySelectorAll('[aria-haspopup="dialog"][aria-expanded="true"]').length,
    active: document.activeElement?.outerHTML.slice(0, 160) ?? "",
  }));
  expect(
    opened.dialogs,
    `Enter on a focused figure opens its drawer; the page heard ${heard}; focus was on ${opened.active}, expanded chips ${opened.expanded}`,
  ).toBe(1);
  const drawer = page.getByRole("dialog");
  await expect(drawer).toBeVisible();
  await expect(drawer).toHaveAttribute("aria-modal", "true");

  // Tab all the way round the drawer and one past the end: focus must still be inside it.
  const inside = await drawer.locator(FOCUSABLE).count();
  expect(inside, "the drawer holds controls to trap").toBeGreaterThan(0);
  const visited: string[] = [];
  for (let press = 0; press <= inside; press += 1) {
    await page.keyboard.press("Tab");
    const held = await page.evaluate(() => {
      const node = document.activeElement;
      return {
        inDrawer: node?.closest('[role="dialog"]') !== null && node !== null,
        name: (node?.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 40),
      };
    });
    visited.push(`${press}: ${held.name} (inside: ${held.inDrawer})`);
    expect(held.inDrawer, `Tab ${press} left the drawer`).toBe(true);
  }
  await page.screenshot({ path: evidencePath(TICKET, "drawer-focus.png"), fullPage: false });

  // Shift+Tab off the first control is the other edge of the same trap.
  await page.keyboard.press("Shift+Tab");
  expect(
    await page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null),
    "Shift+Tab left the drawer",
  ).toBe(true);

  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  expect(
    await page.evaluate((el) => document.activeElement === el, chipHandle),
    "Esc gave focus back to the chip that opened the drawer",
  ).toBe(true);

  writeFileSync(
    evidencePath(TICKET, "drawer-focus.txt"),
    [
      `FR-61, ticket ${TICKET}: the drawer's focus trap`,
      "opened from the figure that triggered it",
      "",
      ...visited,
      "",
      "Shift+Tab off the first control stayed inside: true",
      "Esc closed the drawer and the chip took focus back: true",
      "",
    ].join("\n"),
    "utf8",
  );
});

// ---- 4. Reduced motion: 0ms everywhere, and no arrival fade -------------------------------------

/** Every element whose computed transition or animation would still run. Empty is the answer. */
async function moving(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>("*")]
      .flatMap((node) => [
        { node, style: getComputedStyle(node), where: "" },
        { node, style: getComputedStyle(node, "::before"), where: "::before" },
        { node, style: getComputedStyle(node, "::after"), where: "::after" },
      ])
      .filter(({ style }) => {
        const still = (durations: string) =>
          durations.split(",").every((one) => one.trim() === "0s" || one.trim() === "");
        return !still(style.transitionDuration) || !still(style.animationDuration);
      })
      .map(
        ({ node, style, where }) =>
          `${node.tagName.toLowerCase()}.${node.className}${where} transition ${style.transitionDuration} animation ${style.animationDuration}`,
      ),
  );
}

test("under prefers-reduced-motion every duration is 0s and the arrival fade is absent", async ({
  page,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);

  await page.emulateMedia({ reducedMotion: "reduce" });
  const lines: string[] = [
    `FR-61, ticket ${TICKET}: prefers-reduced-motion: reduce`,
    "",
    "every computed transition-duration and animation-duration, per page:",
  ];
  const still: string[] = [];

  for (const at of pages()) {
    await open(page, at);
    await quiesce(page, at);
    const found = await moving(page);
    lines.push(`  ${at}: ${found.length} element(s) still moving`);
    still.push(...found.map((one) => `${at}: ${one}`));
  }

  // The drawer, which is the one component with a slide to remove.
  await open(page, "/traces");
  await page.locator(FIGURE_TRIGGER).first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  const drawerMotion = await page
    .getByRole("dialog")
    .evaluate((node) => ({
      motion: node.getAttribute("data-motion"),
      duration: getComputedStyle(node).transitionDuration,
    }));
  lines.push("", `drawer: data-motion=${drawerMotion.motion ?? ""} transition ${drawerMotion.duration}`);
  still.push(
    ...(drawerMotion.duration.split(",").every((one) => one.trim() === "0s")
      ? []
      : [`/traces drawer transition ${drawerMotion.duration}`]),
  );
  await page.keyboard.press("Escape");

  // And the arrival fade, on a row that really did arrive on a live stream. The run is started
  // here rather than simulated: a fade that is only absent from a replayed row proves nothing.
  const before = new Set(
    ((await gatewayJson("/sessions?limit=100")).sessions as { session_id: string }[]).map(
      (row) => row.session_id,
    ),
  );
  const running = startEvalRun();
  let live = "";
  await expect
    .poll(
      async () => {
        const rows = (await gatewayJson("/sessions?limit=100")).sessions as {
          session_id: string;
        }[];
        const fresh = rows.find((row) => !before.has(row.session_id));
        live = fresh?.session_id ?? "";
        return live;
      },
      { timeout: SETTLE_TIMEOUT_MS },
    )
    .not.toBe("");

  await open(page, `/traces?session=${encodeURIComponent(live)}`);
  const arrived = page.locator("[data-arrived]").first();
  await expect(arrived, "a row arrived on the live stream").toBeVisible({
    timeout: SETTLE_TIMEOUT_MS,
  });
  const fade = await arrived.evaluate((node) => {
    const layer = getComputedStyle(node, "::before");
    return {
      animationName: layer.animationName,
      animationDuration: layer.animationDuration,
      opacity: layer.opacity,
    };
  });
  lines.push(
    "",
    `the event-arrival fade on a live row of /traces?session=${live}:`,
    `  animation-name ${fade.animationName}, animation-duration ${fade.animationDuration}, opacity ${fade.opacity}`,
    "",
    `elements still moving: ${still.length}`,
    ...still,
    "",
  );
  writeFileSync(evidencePath(TICKET, "reduced-motion.txt"), lines.join("\n"), "utf8");

  await expect(page.getByTestId("stream-status")).toHaveText(/^Done/, {
    timeout: SETTLE_TIMEOUT_MS,
  });
  await running;

  expect(still, "elements whose transition or animation still runs under reduced motion").toEqual([]);
  expect(fade.animationName, "the arrival fade's animation").toBe("none");
  expect(fade.opacity, "the arrival fade's layer").toBe("0");
});

/** `harnessed eval run smoke --fresh`, started in the background against the harness's own home. */
function startEvalRun(): Promise<void> {
  const child = spawn("uv", ["run", "harnessed", "eval", "run", "smoke", "--fresh", "--json"], {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home(), HARNESSED_PROVIDER: "fake" },
    shell: false,
  });
  child.stdout.resume();
  child.stderr.resume();
  return new Promise((done) => child.on("close", () => done()));
}

// ---- 5. Status is never colour alone ------------------------------------------------------------

test("every pass, fail, warn and unknown mark carries a word, never colour alone", async ({
  page,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);

  const lines: string[] = [`FR-61, ticket ${TICKET}: every status mark, and the word it carries`, ""];
  const wordless: string[] = [];
  let seen = 0;

  for (const at of pages()) {
    await open(page, at);
    await quiesce(page, at);
    const marks = await page.evaluate(() => {
      // A mark is a pass/fail pill, an unknown figure, a stale badge or a rule badge: every
      // element on the page whose meaning design.md carries in a colour.
      const selector = '[data-testid="pass-mark"], [data-testid="stale-badge"], [data-figure]';
      return [...document.querySelectorAll(selector)].map((node) => ({
        testid: node.getAttribute("data-testid") ?? "figure",
        text: (node.textContent ?? "").replace(/\s+/g, " ").trim(),
        label: node.getAttribute("aria-label") ?? "",
      }));
    });
    seen += marks.length;
    for (const mark of marks) {
      // The accessible name is the word, whether it is the text or an `aria-label` over it.
      if (mark.text !== "" || mark.label !== "") continue;
      wordless.push(`${at}: ${mark.testid} has no word`);
    }
    lines.push(
      `${at}: ${marks.length} mark(s)`,
      ...marks.map((mark) => `  - ${mark.testid}: "${mark.text}"${mark.label === "" ? "" : ` [${mark.label}]`}`),
      "",
    );
  }

  writeFileSync(
    evidencePath(TICKET, "marks.txt"),
    [...lines, `marks read: ${seen}`, `marks with no word: ${wordless.length}`, ...wordless, ""].join("\n"),
    "utf8",
  );
  expect(seen, "the walk found marks to read at all").toBeGreaterThan(0);
  expect(wordless, "status marks whose only signal is their colour").toEqual([]);
});

// ---- ticket 261 (FR-163): a StatTile has no box of its own, and its size says its rank ----------

test("every StatTile on the Overview and Memory has no border, ground or padding, a sentence-case label, 24px or 14px figures", async ({
  page,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  for (const at of ["/", "/memory"]) {
    for (const scheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await open(page, at);
      await quiesce(page, at);
      const tiles = await page.evaluate(() =>
        [...document.querySelectorAll<HTMLElement>("main [role=group][aria-labelledby]")]
          .filter((tile) => tile.querySelector("[data-figure]") !== null)
          .map((tile) => {
            const style = getComputedStyle(tile);
            const label = document.getElementById(tile.getAttribute("aria-labelledby") ?? "");
            const figure = tile.querySelector<HTMLElement>("[data-figure]") as HTMLElement;
            return {
              name: label?.textContent ?? "",
              border: style.borderTopWidth,
              ground: style.backgroundColor,
              padding: style.paddingTop,
              transform: label === null ? "" : getComputedStyle(label).textTransform,
              size: getComputedStyle(figure).fontSize,
              inGroup: tile.closest("section") !== null,
            };
          }),
      );
      expect(tiles.length, `${at} draws StatTiles`).toBeGreaterThan(0);
      for (const tile of tiles) {
        const where = `${at} ${scheme} ${tile.name}`;
        expect(tile.border, where).toBe("0px");
        expect(tile.ground, where).toBe("rgba(0, 0, 0, 0)");
        expect(tile.padding, where).toBe("0px");
        expect(tile.transform, where).not.toBe("uppercase");
        expect(["24px", "14px"], where).toContain(tile.size);
      }
      if (at === "/") {
        await page.screenshot({ path: evidencePath("261", `overview-${scheme}.png`), fullPage: true });
      } else {
        await page.screenshot({ path: evidencePath("261", `memory-${scheme}.png`), fullPage: true });
      }
    }
  }
});

// ---- 6. 400px: a menu button, no horizontal scroll, tables scrolling inside their panel ---------

test("at 400px the sidebar is a menu button, no page scrolls horizontally, and wide tables scroll inside their panel", async ({
  page,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);

  await page.setViewportSize(NARROW);
  const lines: string[] = [
    `FR-61, ticket ${TICKET}: every page at ${NARROW.width}x${NARROW.height}`,
    "",
  ];
  const overflowing: string[] = [];

  for (const at of pages()) {
    await open(page, at);
    await quiesce(page, at);

    // The sidebar is a menu button: the button is there, and the nav is behind it.
    const menu = page.getByRole("button", { name: "Menu" });
    await expect(menu, `${at}: the sidebar is a menu button at ${NARROW.width}px`).toBeVisible();
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("navigation", { name: "Pages" })).toBeHidden();
    await menu.click();
    await expect(page.getByRole("navigation", { name: "Pages" })).toBeVisible();
    await menu.click();
    await expect(page.getByRole("navigation", { name: "Pages" })).toBeHidden();

    // The page itself does not scroll sideways, and nothing sticks out past the viewport.
    const measured = await page.evaluate(() => {
      const root = document.documentElement;
      const wide = [...document.querySelectorAll<HTMLElement>("body *")]
        .filter((node) => node.getBoundingClientRect().right > root.clientWidth + 1)
        .map((node) => {
          const box = node.getBoundingClientRect();
          return `${node.tagName.toLowerCase()}.${node.className} right=${Math.round(box.right)}`;
        });
      const tables = [...document.querySelectorAll("table")].map((table) => {
        const holder = table.parentElement;
        const overflows = table.scrollWidth > (holder?.clientWidth ?? 0);
        return {
          scrolls: holder !== null && getComputedStyle(holder).overflowX === "auto",
          overflows,
          inPanel: table.closest("section") !== null,
        };
      });
      return {
        scrollWidth: root.scrollWidth,
        clientWidth: root.clientWidth,
        wide: wide.slice(0, 10),
        wideCount: wide.length,
        tables,
      };
    });

    if (measured.scrollWidth > measured.clientWidth) {
      overflowing.push(
        `${at}: document scrollWidth ${measured.scrollWidth} > clientWidth ${measured.clientWidth}; ${measured.wideCount} element(s) past the edge: ${measured.wide.join(" | ")}`,
      );
    }
    for (const table of measured.tables) {
      if (table.scrolls && table.inPanel) continue;
      overflowing.push(
        `${at}: a table is not inside a scrolling panel (scrolls=${table.scrolls}, inPanel=${table.inPanel})`,
      );
    }

    await page.screenshot({
      path: evidencePath(TICKET, `400px-${slug(at)}.png`),
      fullPage: true,
    });
    lines.push(
      `${at}: scrollWidth ${measured.scrollWidth}, clientWidth ${measured.clientWidth}, ${measured.tables.length} table(s), ${measured.wideCount} element(s) past the edge`,
      ...measured.wide.map((one) => `    - ${one}`),
    );
    writeFileSync(evidencePath(TICKET, "400px.txt"), [...lines, ""].join("\n"), "utf8");
  }

  writeFileSync(
    evidencePath(TICKET, "400px.txt"),
    [...lines, "", `pages that overflow: ${overflowing.length}`, ...overflowing, ""].join("\n"),
    "utf8",
  );
  expect(overflowing, `pages that scroll horizontally at ${NARROW.width}px`).toEqual([]);
});

// ---- 7. Contrast, per token pair, in both themes ------------------------------------------------

/**
 * The four opaque grounds of `app/tokens.css`. The `-subtle`, `-line` and `-hatch` tokens are
 * alpha over one of these rather than a ground of their own, so a pair against them is not a
 * ratio of two colours and is not computed here.
 */
const GROUNDS = [
  "--color-bg",
  "--color-surface",
  "--color-surface-raised",
  "--color-surface-sunken",
] as const;

/**
 * `--color-fg-faint` is the one foreground design.md puts no floor under: it is "never used for
 * information, only placeholders and hatch strokes". Every other token the tree sets as a `color:`
 * carries information and is under the 4.5:1 floor.
 */
const NOT_INFORMATION = new Set(["--color-fg-faint"]);

/** Every stylesheet the dashboard ships: the token file and every CSS module under it. */
function stylesheets(): string[] {
  return ["app", "components"].flatMap((directory) => {
    const root = resolve(DASHBOARD_ROOT, directory);
    return readdirSync(root, { recursive: true, encoding: "utf8" })
      .filter((entry) => entry.endsWith(".css"))
      .map((entry) => resolve(root, entry));
  });
}

/**
 * Every `--color-*` token the tree sets as the named property, read off the stylesheets rather
 * than listed here. This is what makes the pair table below the product's own and not a
 * transcription of it: a token that becomes a text colour later is under the text floor from the
 * moment the declaration is written, without this file being edited.
 */
function tokensSetAs(property: "color" | "background"): string[] {
  const head = property === "color" ? "color" : "background(?:-color)?";
  const declaration = new RegExp(String.raw`(?<![-\w])${head}:\s*var\((--color-[\w-]+)\)`, "g");
  const found = new Set<string>();
  for (const sheet of stylesheets()) {
    for (const hit of readFileSync(sheet, "utf8").matchAll(declaration)) found.add(hit[1]);
  }
  return [...found].filter((token) => !GROUNDS.includes(token as (typeof GROUNDS)[number]));
}

/** One pair under one floor, and why it is under that one. */
type Pair = { kind: string; floor: number; foreground: string; background: string };

/**
 * The pairs, derived from the declarations:
 *
 * - **text**, 4.5:1 - every token set as a `color:`, over every ground, minus `--color-fg-faint`.
 *   A figure is text here: `Value` renders one as `leaf.text` in `--color-fg`.
 * - **figure**, 3:1 - the two status dots, which are the only non-text marks the system draws in
 *   a colour (`GatewayStatusPill`, the `PanelFrame` footer). They are never the only signal - the
 *   word sits beside each - so WCAG's non-text floor is the one that applies.
 * - **ring**, 3:1 - the focus ring `app/tokens.css` draws, over every ground it can fall on.
 */
function pairsUnderFloor(): Pair[] {
  const text = tokensSetAs("color").filter((token) => !NOT_INFORMATION.has(token));
  const figures = tokensSetAs("background").filter(
    (token) => !NOT_INFORMATION.has(token) && !/-(subtle|line|hatch)$/.test(token),
  );
  return [
    ...text.flatMap((foreground) =>
      GROUNDS.map((background) => ({ kind: "text", floor: TEXT_RATIO, foreground, background })),
    ),
    ...figures.flatMap((foreground) =>
      GROUNDS.map((background) => ({ kind: "figure", floor: RING_RATIO, foreground, background })),
    ),
    ...GROUNDS.map((background) => ({
      kind: "ring",
      floor: RING_RATIO,
      foreground: "--color-accent",
      background,
    })),
  ];
}

test("every token pair clears 4.5:1, and the focus ring 3:1, in both themes", async ({ page }) => {
  test.setTimeout(WALK_TIMEOUT_MS);

  await open(page, "/");
  const pairs = pairsUnderFloor();
  const lines: string[] = [
    `FR-61, ticket ${TICKET}: contrast per token pair, computed in the test (decision 32)`,
    `text and figures >= ${TEXT_RATIO}:1, the status dots and the focus ring >= ${RING_RATIO}:1`,
    `${pairs.length} pair(s), derived from the color: and background: declarations of app/ and components/`,
    "",
  ];
  const failing: string[] = [];

  // The pairs have to be a real set, and the text floor has to reach the tokens that carry words.
  expect(pairs.length, "the stylesheets declare pairs to measure").toBeGreaterThan(0);
  for (const token of ["--color-fg", "--color-fg-secondary", "--color-fg-muted", "--color-accent"]) {
    expect(
      pairs.some((pair) => pair.kind === "text" && pair.foreground === token),
      `${token} is measured as text`,
    ).toBe(true);
  }

  for (const theme of THEMES) {
    await wear(page, theme);
    const names = [...new Set(pairs.flatMap((pair) => [pair.foreground, pair.background]))];
    // The values are the live `:root`'s, so `app/tokens.css` is the source and this list is not.
    const values = await page.evaluate((wanted) => {
      const root = getComputedStyle(document.documentElement);
      return Object.fromEntries(wanted.map((name) => [name, root.getPropertyValue(name).trim()]));
    }, names);
    for (const name of names) {
      expect(values[name], `${name} is defined in ${theme.name}`).not.toBe("");
    }

    lines.push(`[${theme.name}]`);
    for (const pair of pairs) {
      const ratio = contrast(values[pair.foreground], values[pair.background]);
      const ok = ratio >= pair.floor;
      lines.push(
        `  ${ok ? "ok  " : "FAIL"} ${pair.kind} ${pair.foreground} on ${pair.background}: ${ratioText(ratio)} (floor ${pair.floor}:1)`,
      );
      if (!ok) {
        failing.push(
          `${theme.name}: ${pair.kind} ${pair.foreground} (${values[pair.foreground]}) on ${pair.background} (${values[pair.background]}) is ${ratioText(ratio)}, under ${pair.floor}:1`,
        );
      }
    }
    lines.push("");
  }

  writeFileSync(
    evidencePath(TICKET, "contrast.txt"),
    [...lines, `pairs under their floor: ${failing.length}`, ...failing, ""].join("\n"),
    "utf8",
  );
  expect(failing, "token pairs under their contrast floor").toEqual([]);
});
