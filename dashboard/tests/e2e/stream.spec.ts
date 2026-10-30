/**
 * FR-59 end to end (UJ-11): the operator starts a run in a terminal, finds its session in the
 * dashboard and watches it close.
 *
 * This is the PRD's user-success walk, driven against the live loopback gateway of decision 30:
 * the spec starts `harnessed eval run smoke --fresh` under `HARNESSED_PROVIDER=fake` against the
 * harness's own `HARNESSED_HOME` on port 18756, finds the session it opened on `/traces`, opens
 * it, and reads the stream until it closes. Nothing here writes the database and nothing here
 * touches the operator's 8756 or 8757.
 *
 * The walk measures before it asserts, and writes both numbers to `evidence/112/stream-e2e.txt`:
 * **how long after the CLI started the session became findable**, and **how wide the run's own
 * publish window was** - the span between the `ts` of its first event and the `ts` of its last.
 * Those two numbers are what decides whether a browser can attach while a session is still
 * open, and a walk that asserted without reporting them could not say why it failed.
 *
 * The arrival times are taken by a `MutationObserver` installed before the page's own scripts
 * run, so a row's time is the time the browser had it, not the time a poll noticed. It observes
 * `document` rather than `document.documentElement`, which is still null that early: observing a
 * null target throws, and the walk then reads an empty arrival list off a page that plainly has
 * the rows. Publish time is the `ts` the gateway replays for the same `seq`; both are this
 * machine's wall clock, so the difference is latency and not skew.
 *
 * MUST rule 12: nothing here asserts on a sentence the dashboard prints about itself. The rows
 * are counted, the ids are compared against the gateway's own replay of the same session, and
 * the cut is a real one - `context.setOffline`, which severs the connection the panel is reading.
 */
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { DASHBOARD_URL, GATEWAY_URL, HOME_VARIABLE, REPO_ROOT, cli, evidencePath } from "./harness";

const TICKET = "112";

/** The command the PRD's success criterion names, run exactly as it names it. */
const COMMAND = ["run", "harnessed", "eval", "run", "smoke", "--fresh", "--json"];

/** The event that opens an eval session, and the finish `events/read.py` pairs it with. */
const RUN_STARTED = "eval.run.started";
const RUN_FINISHED = "eval.run.finished";
const CASE_SCORED = "eval.case.scored";

/** What the panel reads once the stream delivers the finish this session's `seq` 1 pairs with -
 * "Done" either way now (FR-129 AC2): the terminal name is no longer spelled into the status
 * word, only into `TraceStep`'s own sentence for the `run.finished` row itself. */
const DONE = "Done";

/** The NFR: a frame is on the page within a second of the gateway recording it. */
const LATENCY_CEILING_MS = 1000;

/** How long the walk waits for the run's session to show up, and for the stream to close. */
const APPEARS_TIMEOUT_MS = 60_000;

/** The CLI, the browser and two servers share one machine, so the walk gets its own ceiling. */
const WALK_TIMEOUT_MS = 180_000;

/** The lines this run writes to `evidence/112/stream-e2e.txt`. */
const log: string[] = [];

type Session = { session_id: string };
type Replayed = { seq: number; name: string; ts: string };

/** One row of the stream log as the browser saw it land. */
type Arrival = { seq: string; at: number };

/** The CLI, started in the background against the harness's own home (decision 30). */
function startRun(): Promise<{ code: number | null; output: string }> {
  const home = process.env[HOME_VARIABLE];
  if (!home) throw new Error(`${HOME_VARIABLE} is not set; playwright.config.ts sets it`);
  const child = spawn("uv", COMMAND, {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home, HARNESSED_PROVIDER: "fake" },
    shell: false,
  });
  let output = "";
  child.stdout.on("data", (chunk: Buffer) => {
    output = `${output}${chunk.toString("utf8")}`;
  });
  child.stderr.on("data", (chunk: Buffer) => {
    output = `${output}${chunk.toString("utf8")}`;
  });
  return new Promise((resolve) => {
    child.on("close", (code) => resolve({ code, output }));
  });
}

/**
 * Watch every row the stream log adds, from before the page's own scripts run.
 *
 * The observer is what makes "under 1 s after publish" a measurement rather than a claim: it
 * records `Date.now()` in the browser at the moment the row is in the document.
 */
const WATCH_ARRIVALS = `
  window.__arrivals = [];
  const seen = new Set();
  new MutationObserver(() => {
    for (const row of document.querySelectorAll('[data-testid="stream-log"] [data-testid="trace-step"]')) {
      const seq = row.getAttribute('data-seq') ?? "";
      if (seq === "" || seen.has(seq)) continue;
      seen.add(seq);
      window.__arrivals.push({ seq, at: Date.now() });
    }
  }).observe(document, { childList: true, subtree: true });
`;

/** Every session id the gateway holds right now. */
async function sessionIds(request: APIRequestContext): Promise<string[]> {
  const body = (await (await request.get(`${GATEWAY_URL}/sessions?limit=100`)).json()) as {
    sessions: Session[];
  };
  return body.sessions.map((session) => session.session_id);
}

/** One session's events, as the gateway replays them. */
async function events(request: APIRequestContext, id: string): Promise<Replayed[]> {
  const body = (await (await request.get(`${GATEWAY_URL}/sessions/${id}/events`)).json()) as {
    events: Replayed[];
  };
  return body.events;
}

/** Every figure on the page that cannot say where it came from (FR-51, the per-page walk). */
async function sourceless(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll("[data-figure],[data-route],[data-path]")]
      .filter((node) => !(node.getAttribute("data-route") && node.getAttribute("data-path")))
      .map((node) => node.textContent ?? ""),
  );
}

/** The rows the stream itself put in the log, as opposed to the replay's own steps. */
function streamRows(page: Page) {
  return page.getByTestId("stream-log").getByTestId("trace-step");
}

/**
 * Wait until the log holds the last `seq` the gateway recorded.
 *
 * `StreamPanel` shows frames in batches of at most one a second, so the status can already read
 * `Done` while the tail of the last batch is still owed to the document. Reading the arrivals
 * before that lands would count rows the panel was about to show.
 */
async function waitForSeq(page: Page, seq: string): Promise<void> {
  await expect(streamRows(page).last()).toHaveAttribute("data-seq", seq, {
    timeout: APPEARS_TIMEOUT_MS,
  });
}

/** Every `seq` the replay's own steps carry, in the order they show them (FR-129: `data-seq` is a
 * non-drawn hook, the same class as a figure's `data-route`/`data-path`, never rendered text). */
async function replayedSeqs(page: Page): Promise<string[]> {
  const steps = await page
    .getByRole("region", { name: "Replay" })
    .getByTestId("trace-step")
    .all();
  const seqs: string[] = [];
  for (const step of steps) seqs.push((await step.getAttribute("data-seq")) ?? "");
  return seqs;
}

function writeEvidence(): void {
  writeFileSync(evidencePath(TICKET, "stream-e2e.txt"), `${log.join("\n")}\n`, "utf8");
}

test("the operator finds the session he just started and watches it close", async ({
  page,
  request,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  await page.addInitScript(WATCH_ARRIVALS);

  // The listing, before the run: every session the events table already holds.
  await page.goto(`${DASHBOARD_URL}/traces`);
  const table = page.getByRole("region", { name: "Sessions" }).getByRole("table");
  await expect(table).toBeVisible();
  const before = new Set(await sessionIds(request));
  await page.screenshot({
    path: evidencePath(TICKET, "sessions.png"),
    fullPage: true,
  });

  // The terminal: `harnessed eval run smoke --fresh`, against the same home the gateway serves.
  const started = Date.now();
  const run = startRun();

  // When the run opens its session, asked of the gateway rather than of the browser: a click
  // loop over the dashboard competes with the CLI for this machine and moves the number it is
  // supposed to be measuring.
  let sessionId = "";
  while (sessionId === "" && Date.now() - started < APPEARS_TIMEOUT_MS) {
    sessionId = (await sessionIds(request)).find((id) => !before.has(id)) ?? "";
  }
  const appeared = Date.now() - started;
  expect(sessionId, "the run opened a session").not.toBe("");
  log.push(`the run opened its session ${appeared} ms after the CLI started`);

  // And it is on `/traces` on `Refresh`, without the page having been navigated again.
  const refetch = page.getByRole("banner").getByRole("button", { name: "Refresh" });
  await refetch.click();
  const row = table.getByRole("link", { name: sessionId, exact: true });
  await expect(row).toBeVisible();
  log.push(`the session appeared on /traces ${Date.now() - started} ms after the CLI started`);

  // How much of this session was already on disk the moment it became findable: the operator
  // can only follow what has not happened yet, so this is what "live" is left to mean.
  const atSight = await events(request, sessionId);
  log.push(`at that moment the events table already held ${atSight.length} of its events`);
  writeEvidence();

  // The row's own link, followed: the page for this session, at the id the row carried.
  await row.click();
  await expect(page).toHaveURL(`${DASHBOARD_URL}/traces?session=${encodeURIComponent(sessionId)}`);
  await expect(page.getByRole("region", { name: "Replay" }).getByTestId("replay-log")).toBeVisible();

  const finished = await run;
  const replayed = await events(request, sessionId);
  const publishWindow = Date.parse(replayed.at(-1)?.ts ?? "") - Date.parse(replayed[0]?.ts ?? "");
  log.push(`the CLI exited ${finished.code} after ${Date.now() - started} ms`);
  log.push(`the run published ${replayed.length} events over a window of ${publishWindow} ms`);
  log.push(finished.output.trim());
  writeEvidence();

  expect(replayed[0]?.name).toBe(RUN_STARTED);
  expect(replayed.at(-1)?.name).toBe(RUN_FINISHED);

  // The panel ends at the finish this session's `seq` 1 pairs with, which the stream delivers.
  const status = page.getByTestId("stream-status");
  await expect(status).toHaveText(DONE, {
    timeout: APPEARS_TIMEOUT_MS,
  });
  await waitForSeq(page, String(replayed.at(-1)?.seq));
  await page.screenshot({
    path: evidencePath(TICKET, "live-stream.png"),
    fullPage: true,
  });

  // The rows that arrived over the stream, and the ones the replay had already shown.
  const arrivals = (await page.evaluate(() => window.__arrivals)) as Arrival[];
  const streamed = arrivals.map((arrival) => arrival.seq);
  const walked = [...(await replayedSeqs(page)), ...streamed];
  log.push(`replayed ${walked.length - streamed.length} rows, streamed ${streamed.length} rows`);

  // 0 lost and 0 repeated: every `seq` the gateway holds, once, in its own order.
  expect(walked).toEqual(replayed.map((event) => String(event.seq)));
  expect(new Set(walked).size).toBe(walked.length);

  // `eval.case.scored` rows arrived over the stream, not only in the replay.
  const scored = replayed.filter((event) => event.name === CASE_SCORED).map((e) => String(e.seq));
  expect(scored.filter((seq) => streamed.includes(seq)).length).toBeGreaterThan(0);

  // Each frame was on the page within a second of the gateway recording it.
  const published = new Map(replayed.map((event) => [String(event.seq), Date.parse(event.ts)]));
  for (const arrival of arrivals) {
    log.push(`seq ${arrival.seq}: ${arrival.at - (published.get(arrival.seq) ?? 0)} ms to render`);
  }
  writeEvidence();
  const late = arrivals.filter(
    (arrival) => arrival.at - (published.get(arrival.seq) ?? 0) > LATENCY_CEILING_MS,
  );
  expect(late).toEqual([]);

  expect(await sourceless(page)).toEqual([]);
});

/**
 * The cut, made while the run still owes the page events.
 *
 * 119's latency knob is what makes this a cut *mid-stream*: the page is open and rows are
 * landing when the connection is severed, so the panel is reading a subscription that is still
 * owed rows rather than one that has already closed. On restore it reconnects at its own cursor
 * and the gateway replays from there, which is the claim - 0 lost and 0 repeated.
 */
test("a connection cut mid-stream loses nothing and repeats nothing", async ({
  page,
  context,
  request,
}) => {
  test.setTimeout(WALK_TIMEOUT_MS);
  await page.addInitScript(WATCH_ARRIVALS);
  await page.goto(`${DASHBOARD_URL}/traces`);
  await expect(page.getByRole("region", { name: "Sessions" }).getByRole("table")).toBeVisible();
  const before = new Set(await sessionIds(request));

  const started = Date.now();
  const run = startRun();

  // The gateway is asked for the new session, not the browser: the same separation as the first
  // walk, and for the same reason.
  let sessionId = "";
  while (sessionId === "" && Date.now() - started < APPEARS_TIMEOUT_MS) {
    sessionId = (await sessionIds(request)).find((id) => !before.has(id)) ?? "";
  }
  expect(sessionId, "the run opened a session").not.toBe("");

  await page.goto(`${DASHBOARD_URL}/traces?session=${encodeURIComponent(sessionId)}`);
  await expect(page.getByRole("region", { name: "Replay" }).getByTestId("replay-log")).toBeVisible();

  // Rows are on the page over the stream before anything is cut.
  const rows = streamRows(page);
  await expect.poll(() => rows.count(), { timeout: APPEARS_TIMEOUT_MS }).toBeGreaterThan(0);
  const beforeCut = rows.count();

  // The cut is a real one: `setOffline` severs the connection the panel is reading, and the run
  // goes on publishing into the gateway while the page cannot reach it.
  await context.setOffline(true);
  const seenAtCut = await beforeCut;
  const finished = await run;
  expect(finished.code).toBe(0);
  await context.setOffline(false);

  const replayed = await events(request, sessionId);
  await expect(page.getByTestId("stream-status")).toHaveText(DONE, {
    timeout: APPEARS_TIMEOUT_MS,
  });
  await waitForSeq(page, String(replayed.at(-1)?.seq));

  const arrivals = (await page.evaluate(() => window.__arrivals)) as Arrival[];
  const walked = [...(await replayedSeqs(page)), ...arrivals.map((arrival) => arrival.seq)];

  // Every `seq` the gateway holds, once, in its own order: 0 lost across the outage and 0
  // repeated by the replay the reconnect asked for.
  expect(walked).toEqual(replayed.map((event) => String(event.seq)));
  expect(new Set(walked).size).toBe(walked.length);

  // And the cut was mid-stream: rows had landed before it, and more landed after it.
  expect(seenAtCut).toBeGreaterThan(0);
  expect(arrivals.length).toBeGreaterThan(seenAtCut);

  log.push(`before the cut: ${seenAtCut} rows had arrived over the stream`);
  log.push(
    `after the cut: ${walked.length - arrivals.length} replayed, ${arrivals.length} streamed`,
  );
  log.push(`the cut walk took ${Date.now() - started} ms`);
  writeEvidence();
});

declare global {
  interface Window {
    __arrivals: Arrival[];
  }
}

/**
 * The replay reads as a transcript (ticket 201): an ad-hoc run under the fake provider is the one
 * session the ledger holds a served message for, so its page opens on the operator's own words
 * and closes on the reply, with the steps between.
 */
test("the replay of an ad-hoc run reads as user input, steps, response", async ({ page, request }) => {
  const opened = cli(["run", "ping", "--json"]);
  expect(opened.status, opened.stderr).toBe(0);
  const sessionId = (JSON.parse(opened.stdout) as { run_key: string }).run_key;

  await page.goto(`${DASHBOARD_URL}/traces?session=${encodeURIComponent(sessionId)}`);
  const replayLog = page.getByRole("region", { name: "Replay" }).getByTestId("replay-log");
  await expect(replayLog).toBeVisible();
  const input = replayLog.getByTestId("transcript-input");
  const response = replayLog.getByTestId("transcript-response");
  await expect(input).toHaveText(/^user input:ping$/);
  await expect(response).toContainText("response:");
  await expect(replayLog.getByTestId("trace-step").first()).toBeVisible();

  // First row is the input, last is the response, and every step sits between them.
  const rows = replayLog.locator("ul > li");
  await expect(rows.first()).toHaveAttribute("data-testid", "transcript-input");
  await expect(rows.last()).toHaveAttribute("data-testid", "transcript-response");
  expect((await events(request, sessionId)).length).toBe(await replayLog.getByTestId("trace-step").count());
  expect(await sourceless(page)).toEqual([]);

  await page.screenshot({ path: evidencePath("201", "transcript.png"), fullPage: true });
});
