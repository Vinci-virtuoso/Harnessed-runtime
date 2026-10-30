// @vitest-environment jsdom
/**
 * FR-75 (UJ-14, UJ-15): the Overview is six panels over six routes, refetched on a signal and
 * never on a timer (ticket 134; decisions 34, 41, 47).
 *
 * `/` holds one `PanelFrame` per aggregate route - spend, turn seconds, run totals, event totals,
 * the gate split and memory totals - each a group of `StatTile`s wrapping `Value`s, `ActivityList`
 * as Recent activity over `/v1/messages?limit=` (ticket 187, replacing "Latest turns"), and the
 * newest session's id as a link, Latest run. FR-54's health panel moved to the Models page's
 * `SystemSection` (ticket 186, FR-129): the Overview renders no "Gateway health" heading.
 * **SSE-for-change, fetch-to-read**: the page opens the watched stream (the newest session in the
 * listing) through `lib/stream.ts`, and a terminal frame is a refetch signal and never a figure.
 *
 * The inputs are the recorded gateway responses (decision 33, MUST rule 24): the six aggregate
 * bodies were recorded by this ticket through `--record-dashboard-fixtures`. The scripted stream
 * is cut from the two recorded streams, frame by frame, and delivered on the newest session's
 * route. One frame is **hand-written**, and marked as such below: no emitter writes
 * `memory.write.committed` yet, so no recording holds one, and decision 33 allows exactly this
 * class of hand-made input for stream tests.
 *
 * `verify` is this jsdom suite, so it renders no pixels: the evidence it writes is text - which
 * source each panel names, the refetch counts per frame, and the request census.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Home from "../../app/page";
import { AppShell } from "../../components/AppShell";
import { clockTime } from "../../lib/clock";
import { humanWord } from "../../lib/human-words";
import { LAST_EVENT_ID } from "../../lib/stream";

/*
 * Eight panels over forty minutes of fake clock is about 1.6 s alone and past vitest's default
 * 5 s when the whole suite shares the machine; the first full run timed out on exactly that. The
 * budget is time, never an assertion.
 */
vi.setConfig({ testTimeout: 20 * 1000 });

const DASHBOARD = resolve(import.meta.dirname, "../..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE_DIR = resolve(DASHBOARD, "../evidence/134");
const EVIDENCE_187 = resolve(DASHBOARD, "../evidence/187");

/** The dashboard's clock, frozen so "fetched hh:mm:ss" is the same characters on every run. */
const FROZEN = new Date("2026-09-23T09:15:00");

const SECOND = 1000;
const MINUTE = 60 * SECOND;

/** One recorded body, read from the committed record and never written by a test. */
function body(name: string): string {
  return readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8");
}

function recordedRoute(name: string): string {
  const meta = JSON.parse(
    readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8"),
  ) as { route: string };
  return meta.route;
}

/** The six panels, each with the route it owns, its recording and the CLI twin 126 to 133 ship. */
const PANELS = [
  { route: "/v1/spend", fixture: "spend", twin: "harnessed spend --json" },
  { route: "/v1/runs/turn_seconds", fixture: "runs.turn-seconds", twin: "harnessed runs turn-seconds --json" },
  { route: "/v1/runs/totals", fixture: "runs.totals", twin: "harnessed runs totals --json" },
  { route: "/v1/events/totals", fixture: "events.totals", twin: "harnessed events totals --json" },
  { route: "/v1/events/gate", fixture: "events.gate", twin: "harnessed events gate --json" },
  { route: "/v1/memory/totals", fixture: "memory.totals", twin: "harnessed memory totals --json" },
] as const;

const HEALTH_ROUTE = "/v1/health";
const SESSIONS_ROUTE = "/v1/sessions";
const ACTIVITY_ROUTE = "/v1/messages?limit=20";
/** Ticket 252's two reads, over ticket 256's seventh home (`tasked.*`). */
const TOTALS_ROUTE = "/v1/tasks/totals";
const NEEDS_ROUTE = "/v1/tasks?state=needs_human";

/** The newest session in the recorded listing, which is the session the page watches. */
const WATCHED = "run_000000000DGW1YRVDP3ZCRZF20";
const WATCHED_STREAM = `/v1/sessions/${WATCHED}/stream`;

/** Every route the page reads once on load and again on every terminal frame. */
const REFETCHED = [...PANELS.map((panel) => panel.route), ACTIVITY_ROUTE, TOTALS_ROUTE, NEEDS_ROUTE];

const BODIES: ReadonlyMap<string, string> = new Map([
  ...PANELS.map((panel) => [panel.route, body(panel.fixture)] as const),
  [HEALTH_ROUTE, body("health")],
  [SESSIONS_ROUTE, body("sessions.listing")],
  [ACTIVITY_ROUTE, body("messages.recent")],
  [TOTALS_ROUTE, body("tasked.tasks.totals")],
  [NEEDS_ROUTE, body("tasked.tasks.needs-human")],
]);

/** A frame ends at a blank line (decision 20). */
const SEPARATOR = "\n\n";

function framesOf(name: string): string[] {
  return readFileSync(resolve(GATEWAY_FIXTURES, `${name}.sse`), "utf8")
    .split(SEPARATOR)
    .filter((block) => block.length > 0)
    .map((block) => `${block}${SEPARATOR}`);
}

function named(frames: readonly string[], event: string): string {
  const found = frames.find((frame) => frame.includes(`\nevent: ${event}\n`));
  if (found === undefined) throw new Error(`no recorded frame named ${event}`);
  return found;
}

const IMPROVE_FRAMES = framesOf("improve.sessions.stream");
const RUN_FRAMES = framesOf("sessions.stream");

/** Cut from the recordings: a start, then each recorded terminal name. */
const START = named(IMPROVE_FRAMES, "improve.run.started");
const NOT_TERMINAL = named(IMPROVE_FRAMES, "eval.case.scored");
const RUN_FINISHED = named(RUN_FRAMES, "run.finished");
const EVAL_FINISHED = named(IMPROVE_FRAMES, "eval.run.finished");
const IMPROVE_FINISHED = named(IMPROVE_FRAMES, "improve.run.finished");

/**
 * **Hand-written** (decision 33): `memory.write.committed` is a frozen name in
 * `events/names.py` that no emitter writes yet, so no recording holds one. Four lines, exactly
 * `gateway/sse.py::frame`'s shape.
 */
const MEMORY_COMMITTED = [
  "id: 900",
  "event: memory.write.committed",
  'data: {"mem_id":"mem_000000000ZZZZZZZZZZZZZZZZZ","pillar":"semantic"}',
  "",
  "",
].join("\n");

/** Every figure's characters a frame's payload carries, none of which may land on the page. */
const FRAME_ONLY_TEXT = "mem_000000000ZZZZZZZZZZZZZZZZZ";

// ---- the scripted gateway -----------------------------------------------------------------------

/** Every request the page made, in order. */
let asked: { url: string; method: string; cursor: string | null }[] = [];

/** The open stream's controller, so a test says when a frame arrives. */
let stream: ReadableStreamDefaultController<Uint8Array> | undefined;

/** What the next stream request is answered with. */
let streamAnswer: "open" | "refused" = "open";

/** A route answered differently for one test: another recording, or a rejected `fetch`. */
let overrides = new Map<string, () => Promise<Response>>();

function json(text: string, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(text, { status, headers: { "content-type": "application/json" } }),
  );
}

function gateway(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = String(input);
  const headers = new Headers(init?.headers);
  asked.push({ url, method: init?.method ?? "GET", cursor: headers.get(LAST_EVENT_ID) });

  const override = overrides.get(url);
  if (override !== undefined) return override();
  if (url === WATCHED_STREAM) {
    if (streamAnswer === "refused") return json(body("notfound.sessions.stream"), 404);
    const opened = new ReadableStream<Uint8Array>({
      start(controller) {
        stream = controller;
      },
    });
    return Promise.resolve(
      new Response(opened, { status: 200, headers: { "content-type": "text/event-stream" } }),
    );
  }
  const found = BODIES.get(url);
  if (found === undefined) return json(body("notfound.route"), 404);
  return json(found);
}

async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function flushed(): Promise<void> {
  await settle();
  await settle();
  await settle();
}

/** One frame arrives on the watched stream, in a read of its own. */
async function arrive(frame: string): Promise<void> {
  await act(async () => {
    stream?.enqueue(new TextEncoder().encode(frame));
  });
  await flushed();
}

async function renderOverview(): Promise<void> {
  render(
    <AppShell>
      <Home />
    </AppShell>,
  );
  await flushed();
}

/** How many times each route was asked, by the page's own reads. */
function countOf(route: string): number {
  return asked.filter((request) => request.url === route).length;
}

function counts(): Record<string, number> {
  return Object.fromEntries(REFETCHED.map((route) => [route, countOf(route)]));
}

/** Every panel's own heading text, keyed by route: the footer names no route any more (`D-9`). */
const TITLES: Record<string, string> = {
  "/v1/spend": "Spent",
  "/v1/runs/turn_seconds": "Average turn",
  "/v1/runs/totals": "Turns and tool calls",
  "/v1/events/totals": "Events and sessions",
  "/v1/events/gate": "Memory check",
  "/v1/memory/totals": "Memory",
  [ACTIVITY_ROUTE]: "Recent activity",
};

/** The panel whose heading names `route`'s title: one `PanelFrame`, one route. */
function panelFor(route: string): HTMLElement {
  const title = TITLES[route];
  if (title === undefined) throw new Error(`no title mapped for ${route}`);
  const heading = screen.getByRole("heading", { name: title });
  const panel = heading.closest("section");
  expect(panel, `${title} sits in a PanelFrame`).not.toBeNull();
  return panel as HTMLElement;
}

function figuresIn(panel: HTMLElement): HTMLElement[] {
  return [...panel.querySelectorAll<HTMLElement>("[data-figure]")];
}

const evidence: Record<string, string[]> = { panels: [], refetches: [], census: [] };

function note(file: string, line: string): void {
  evidence[file]?.push(line);
}

/** Ticket 187's own evidence: Recent activity's rows and empty state, and the Latest run trace. */
const activity187: string[] = [];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  vi.setSystemTime(FROZEN);
  asked = [];
  stream = undefined;
  streamAnswer = "open";
  overrides = new Map();
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
  vi.stubGlobal("fetch", vi.fn(gateway));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

afterAll(() => {
  mkdirSync(EVIDENCE_DIR, { recursive: true });
  for (const [file, lines] of Object.entries(evidence)) {
    const path = resolve(EVIDENCE_DIR, `${file}.txt`);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, `${lines.join("\n")}\n`, "utf8");
  }
  mkdirSync(EVIDENCE_187, { recursive: true });
  writeFileSync(resolve(EVIDENCE_187, "activity-and-latest-run.txt"), `${activity187.join("\n")}\n`, "utf8");
});

// ---- AC 1: six panels, one route each ----------------------------------------------------------

describe("the Overview's panels", () => {
  it("recorded each of the six routes from the Python emitters", () => {
    for (const panel of PANELS) expect(recordedRoute(panel.fixture)).toBe(panel.route);
  });

  it("holds six PanelFrames, one per route, each a StatTile group", async () => {
    await renderOverview();

    for (const panel of PANELS) {
      const frame = panelFor(panel.route);
      const tiles = within(frame).getAllByRole("group");
      const figures = figuresIn(frame);
      expect(tiles.length, `${panel.route} is a StatTile group`).toBeGreaterThan(0);
      expect(figures.length).toBeGreaterThan(0);
      // 0 panels render a figure from another panel's response (MUST rule 21 as amended).
      const foreign = figures.filter((figure) => figure.dataset.route !== panel.route);
      expect(foreign.map((figure) => figure.dataset.route)).toEqual([]);
      note(
        "panels",
        `${panel.route}: ${tiles.length} tiles, ${figures.length} figures, all from ${panel.route}`,
      );
    }
  });

  // ---- FR-129 (ticket 186): the health panel moved to the Models page's SystemSection ----------

  it("renders no health panel: it moved to the Models page's SystemSection", async () => {
    await renderOverview();
    expect(screen.queryByRole("heading", { name: "Gateway health" })).toBeNull();
    note("panels", `${HEALTH_ROUTE}: the health panel is gone, moved to /models's SystemSection`);
  });

  it("renders every figure as the characters its own recording spelled", async () => {
    await renderOverview();

    for (const panel of PANELS) {
      const recordedBody = JSON.parse(body(panel.fixture)) as Record<string, unknown>;
      for (const figure of figuresIn(panelFor(panel.route))) {
        const path = figure.dataset.path ?? "";
        const key = path.replace(/^\./, "").replace(/\[\d+\]$/, "");
        const sent = recordedBody[key];
        const raw = Array.isArray(sent)
          ? String(sent[Number(path.match(/\[(\d+)\]$/)?.[1])])
          : sent === null
            ? "unknown"
            : String(sent);
        // `key_kind` (and any field `lib/human-words.ts` covers) renders its human word on screen,
        // with the ledger's own word kept reachable through the figure's own tooltip (`D-7`).
        const human = path.startsWith(".key_kinds[") ? humanWord("key_kind", raw) : undefined;
        expect(figure.textContent, `${panel.route} ${path}`).toBe(human ?? raw);
        if (human !== undefined) {
          expect(figure.getAttribute("title"), `${panel.route} ${path}`).toContain(raw);
        }
      }
    }
  });

});

// ---- ticket 187: Recent activity replaces Latest turns, and the Latest run trace ----------------

describe("Recent activity", () => {
  it("titles each row by the operator's own words, with 0 run keys, timestamps or stop words", async () => {
    await renderOverview();
    const recent = panelFor(ACTIVITY_ROUTE);
    const recorded = JSON.parse(body("messages.recent")) as {
      messages: { session_id: string; message_seq: number; text: string; created_at: string; handler: string; outcome: string; cost: string }[];
    };
    const message = recorded.messages[0];
    if (message === undefined) throw new Error("messages.recent recorded no rows");

    const row = within(recent).getByRole("link", { name: new RegExp(message.text) });
    expect(row.textContent).toBe(message.text);
    expect(row.getAttribute("href")).toBe(`/console?session=${message.session_id}#exchange-${message.message_seq}`);
    expect(recent.textContent).not.toContain(message.session_id);
    expect(recent.textContent).not.toContain(message.created_at);

    expect(within(recent).getByText(message.handler)).toBeTruthy();
    expect(within(recent).getByText(humanWord("outcome", message.outcome) ?? message.outcome)).toBeTruthy();
    expect(within(recent).getByText(message.cost)).toBeTruthy();
    note("panels", `${ACTIVITY_ROUTE}: row titled "${message.text}", 0 run keys or timestamps in text`);
    activity187.push(
      `rows: 1 row, title "${message.text}", handler ${message.handler}, outcome ` +
        `${humanWord("outcome", message.outcome) ?? message.outcome}, cost ${message.cost}; ` +
        `0 run keys or ISO timestamps in the row's text`,
    );
  });

  it("reads 'Nothing has run yet.' over an empty listing", async () => {
    overrides.set(ACTIVITY_ROUTE, () => json(body("empty.messages.recent")));
    await renderOverview();
    expect(within(panelFor(ACTIVITY_ROUTE)).getByText("Nothing has run yet.")).toBeTruthy();
    expect(within(panelFor(ACTIVITY_ROUTE)).queryByRole("list")).toBeNull();
    activity187.push(`empty: "Nothing has run yet." over ${body("empty.messages.recent")}`);
  });
});

describe("Latest run", () => {
  it("shows nothing when there is no session to watch", async () => {
    overrides.set(SESSIONS_ROUTE, () => json(body("empty.sessions.listing")));
    await renderOverview();
    expect(screen.queryByRole("heading", { name: "Latest run" })).toBeNull();
    activity187.push("latest run: no heading, no TraceTimeline, when the listing holds no session");
  });

  it("shows the newest session id as a link to its page", async () => {
    await renderOverview();

    const heading = screen.getByRole("heading", { name: "Latest run" });
    const section = heading.closest("section") as HTMLElement;
    expect(section).not.toBeNull();
    const links = within(section).getAllByRole("link");
    expect(links).toHaveLength(1);
    const link = links[0] as HTMLElement;
    expect(link.getAttribute("href")).toBe(`/traces?session=${WATCHED}`);
    expect(link.textContent).toContain(WATCHED);
    expect(link.getAttribute("data-route")).toBe(SESSIONS_ROUTE);
    expect(link.getAttribute("data-path")).toBe(".sessions[0].session_id");
    expect(within(section).queryByRole("heading", { name: "Replay" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Replay" })).toBeNull();
    expect(screen.queryByTestId("replay-log")).toBeNull();
    expect(screen.queryByRole("region", { name: "Session stream" })).toBeNull();
    // The Overview's own watch is the one stream it opens; the section adds none, and reads no events.
    const urls = asked.map((request) => request.url);
    expect(urls.filter((url) => url === `/v1/sessions/${WATCHED}/events`)).toEqual([]);
    expect(urls.filter((url) => url.includes("/messages") && url.includes(WATCHED))).toEqual([]);
    expect(urls.filter((url) => url === WATCHED_STREAM)).toHaveLength(1);
    note("panels", "Latest run: the newest session id, a link to its page; no TraceTimeline");
    activity187.push(`latest run: the id ${WATCHED} links to /traces?session=${WATCHED}; no Replay, no stream`);
  });
});

// ---- AC 2: each chip opens its own route, with its own CLI twin -----------------------------------

describe("each tile's source", () => {
  it("opens the drawer for its own route and names that route's CLI command", async () => {
    await renderOverview();

    for (const panel of PANELS) {
      const frame = panelFor(panel.route);
      // Every figure is its own trigger now: no separate `SourceChip`.
      const figures = figuresIn(frame);
      expect(figures.length).toBeGreaterThan(0);

      fireEvent.click(figures[0] as HTMLElement);
      const drawer = screen.getByRole("dialog");
      expect(within(drawer).getByRole("heading", { name: "Where this came from" })).toBeTruthy();
      expect(within(drawer).getByTestId("cli-twin").textContent).toBe(panel.twin);
      note("panels", `${panel.route}: drawer opens, CLI twin ${panel.twin}`);
      fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
      expect(screen.queryByRole("dialog")).toBeNull();
    }
  });
});

// ---- AC 3: a terminal frame is a refetch signal, never a figure -----------------------------------

describe("the watched stream", () => {
  it("is the newest session in the listing, opened from the start", async () => {
    await renderOverview();

    const opened = asked.filter((request) => request.url === WATCHED_STREAM);
    expect(opened).toHaveLength(1);
    expect(opened[0]?.cursor).toBeNull();
    expect(asked.every((request) => request.method === "GET")).toBe(true);
    expect(screen.getByTestId("watched-stream").textContent).toContain(WATCHED);
  });

  it("fires exactly one refetch per panel per terminal frame and none between them", async () => {
    await renderOverview();
    const once = Object.fromEntries(REFETCHED.map((route) => [route, 1]));
    expect(counts()).toEqual(once);
    const healthBefore = countOf(HEALTH_ROUTE);
    note("refetches", `on load: ${JSON.stringify(counts())}`);

    // A frame that is not one of the four is not a signal.
    await arrive(START);
    await arrive(NOT_TERMINAL);
    expect(counts()).toEqual(once);
    note("refetches", "after improve.run.started and eval.case.scored: unchanged");

    const terminals: [string, string][] = [
      ["run.finished", RUN_FINISHED],
      ["eval.run.finished", EVAL_FINISHED],
      ["improve.run.finished", IMPROVE_FINISHED],
      ["memory.write.committed", MEMORY_COMMITTED],
    ];
    for (const [index, [name, frame]] of terminals.entries()) {
      const before = counts();
      await arrive(frame);
      await flushed();
      const after = counts();
      for (const route of REFETCHED) {
        expect(after[route], `${route} after ${name}`).toBe((before[route] ?? 0) + 1);
      }
      note("refetches", `after ${name} (frame ${index + 1}): ${JSON.stringify(after)}`);

      // Between frames, nothing: ten minutes pass and no panel reads again.
      await settle(10 * MINUTE);
      expect(counts()).toEqual(after);
    }

    // The health panel is FR-54's, unchanged: it is not a refetch target, only the pill polls it.
    const panelHealthReads = countOf(HEALTH_ROUTE) - healthBefore;
    note("refetches", `/v1/health reads over 40 min of fake clock: ${panelHealthReads} (pill poll)`);

    // 0 figures rendered from a frame's payload.
    expect(document.body.textContent).not.toContain(FRAME_ONLY_TEXT);
    const fromStream = [...document.querySelectorAll<HTMLElement>("[data-figure]")].filter(
      (figure) => figure.dataset.route === WATCHED_STREAM,
    );
    expect(fromStream).toEqual([]);
  });

  it("reconnects after a drop from the last seq it saw, and refetches nothing for the drop", async () => {
    await renderOverview();
    await arrive(START);
    const before = counts();

    await act(async () => {
      stream?.error(new Error("the socket vanished"));
    });
    await flushed();
    expect(screen.getByTestId("watched-stream").textContent).toContain("reconnecting");

    await settle(SECOND);
    await flushed();
    const reopened = asked.filter((request) => request.url === WATCHED_STREAM);
    expect(reopened.map((request) => request.cursor)).toEqual([null, "1"]);
    expect(counts()).toEqual(before);
  });

  it("stops on a refusal and on a clean close, without retrying", async () => {
    streamAnswer = "refused";
    await renderOverview();
    expect(screen.getByTestId("watched-stream").textContent).toContain("could not connect (404)");
    await settle(30 * MINUTE);
    expect(countOf(WATCHED_STREAM)).toBe(1);

    cleanup();
    asked = [];
    streamAnswer = "open";
    await renderOverview();
    await act(async () => {
      stream?.close();
    });
    await flushed();
    expect(screen.getByTestId("watched-stream").textContent).toContain("finished");
    await settle(30 * MINUTE);
    expect(countOf(WATCHED_STREAM)).toBe(1);
  });
});

// ---- AC 4: no page-wide polling timer -------------------------------------------------------------

describe("the request census", () => {
  it("repeats only /v1/health without navigation, Refetch or a frame", async () => {
    await renderOverview();
    const onLoad = asked.length;
    await settle(30 * MINUTE);

    const repeated = asked.slice(onLoad).map((request) => request.url);
    expect(new Set(repeated)).toEqual(new Set([HEALTH_ROUTE]));
    const tally = new Map<string, number>();
    for (const request of asked) tally.set(request.url, (tally.get(request.url) ?? 0) + 1);
    for (const [url, times] of tally) note("census", `${url}: ${times}`);
    note("census", "over 30 min of fake clock with no frame: only /v1/health repeated");
  });
});

// ---- AC 5: the window and the fetch time ---------------------------------------------------------

describe("each tile's label", () => {
  it("states the window the counts cover and the fetch time", async () => {
    await renderOverview();

    for (const panel of PANELS) {
      const frame = panelFor(panel.route);
      expect(within(frame).getByText("all time, counted now")).toBeTruthy();
      // A group draws no "Updated" line of its own (FR-163): the page keeps its one, and the exact
      // fetch time is the drawer's "Updated" row, which every figure still opens onto.
      expect(within(frame).queryByTestId("stale-badge")).toBeNull();
      expect(frame.textContent).not.toContain("Updated");

      fireEvent.click(figuresIn(frame)[0] as HTMLElement);
      const drawer = screen.getByRole("dialog");
      expect(drawer.textContent).toContain(`${clockTime(FROZEN)} (dashboard clock)`);
      fireEvent.keyDown(document, { key: "Escape" });
      expect(screen.queryByRole("dialog")).toBeNull();
    }
  });
});

// ---- ticket 261 (FR-163): each read is one card of sentence-case figures --------------------------

/** "At a glance labels" (design.md): each group's title and the tile names it holds, in order. */
const GLANCE: { route: string; title: string; labels: string[] }[] = [
  {
    route: "/v1/spend",
    title: "Spent",
    labels: ["Total", "Currency", "Router", "Agents", "API-equivalent (not in the total)", "Not metered", "Priced turns"],
  },
  { route: "/v1/runs/turn_seconds", title: "Average turn", labels: ["Seconds", "Turns counted"] },
  { route: "/v1/runs/totals", title: "Turns and tool calls", labels: ["Turns", "Tool calls"] },
  { route: "/v1/events/totals", title: "Events and sessions", labels: ["Events", "Sessions"] },
  { route: "/v1/events/gate", title: "Memory check", labels: ["Skipped", "Retrieved", "Failed open"] },
  { route: "/v1/memory/totals", title: "Memory", labels: ["Facts", "Procedures", "Archived facts", "Episodes"] },
];

describe("At a glance cards share a row height (ticket 306, FR-174)", () => {
  it("stretches every card in a row instead of top-aligning them", () => {
    const grid = readFileSync(resolve(__dirname, "../../app/overview.module.css"), "utf8");
    const panels = /\.panels\s*\{([^}]*)\}/.exec(grid)?.[1] ?? "";
    expect(panels).toMatch(/align-items:\s*stretch/);
    const frame = readFileSync(resolve(__dirname, "../../components/PanelFrame.module.css"), "utf8");
    expect(/\.card\s*\{([^}]*)\}/.exec(frame)?.[1] ?? "").toMatch(/height:\s*100%/);
  });
});

describe("At a glance draws each read as one StatGroup (ticket 261, FR-163)", () => {
  it("holds 6 groups titled in order, each holding its labelled tiles", async () => {
    await renderOverview();
    const frames = GLANCE.map((group) => panelFor(group.route));
    expect(frames).toHaveLength(6);
    frames.forEach((frame, index) => {
      if (index === 0) return;
      const before = frames[index - 1] as HTMLElement;
      expect(before.compareDocumentPosition(frame) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });
    for (const group of GLANCE) {
      const frame = panelFor(group.route);
      expect(within(frame).getByRole("heading", { name: group.title })).toBeTruthy();
      const tiles = within(frame).getAllByRole("group");
      expect(tiles.map((tile) => tile.getAttribute("aria-labelledby")).map((id) => document.getElementById(id as string)?.textContent)).toEqual(group.labels);
      expect(group.labels.every((label) => within(frame).getByRole("group", { name: label }))).toBe(true);
    }
  });

  it("states no label in capitals: sentence case, never an uppercase string", async () => {
    await renderOverview();
    for (const group of GLANCE) {
      for (const label of group.labels) expect(label).not.toBe(label.toUpperCase());
    }
  });

  it("draws the Spent total as served beside its served currency, with 0 dollar signs", async () => {
    await renderOverview();
    const spent = panelFor("/v1/spend");
    const total = spent.querySelector<HTMLElement>('[data-path=".total"]') as HTMLElement;
    expect(total.textContent).toBe(JSON.parse(body("spend")).total);
    const currency = spent.querySelector<HTMLElement>('[data-path=".currency"]') as HTMLElement;
    expect(currency.textContent).toBe(JSON.parse(body("spend")).currency);
    expect(spent.textContent).not.toContain("$");
  });

  it("draws no Updated line and no Refresh inside any group", async () => {
    await renderOverview();
    for (const group of GLANCE) {
      const frame = panelFor(group.route);
      expect(frame.textContent).not.toContain("Updated");
      expect(within(frame).queryAllByRole("button", { name: "Refresh" })).toHaveLength(0);
    }
  });
});

// ---- AC 6: the gate split is three raw counts ------------------------------------------------------

describe("the gate split tile", () => {
  it("shows skip, retrieve, failed_open in declared order, glossing only the third", async () => {
    await renderOverview();

    const gate = panelFor("/v1/events/gate");
    const paths = figuresIn(gate).map((figure) => figure.dataset.path);
    expect(paths).toEqual([".skip", ".retrieve", ".failed_open"]);

    const glosses = within(gate).getAllByText(
      "failed open: the gate could not answer and memory was retrieved anyway",
    );
    expect(glosses).toHaveLength(1);
    // One gloss, beneath the three counts (design.md, "At a glance labels"), never inside a tile.
    expect((glosses[0] as HTMLElement).closest('[role="group"]')).toBeNull();

    // Never a percentage of `decided`, and `decided` itself is not offered.
    expect(gate.textContent).not.toContain("%");
    expect(paths).not.toContain(".decided");
  });
});

// ---- each panel's own states, from `PanelFrame` and `lib/fetch-panel.ts` -----------------------

describe("a panel that cannot answer", () => {
  const rejected = () => Promise.reject(new TypeError("Failed to fetch"));

  it("is unreachable on its own when its fetch rejects, and the other panels still answer", async () => {
    overrides.set("/v1/events/gate", rejected);
    await renderOverview();

    const gate = screen
      .getAllByRole("region")
      .find((region) => within(region).queryByRole("heading", { name: "Memory check" }) !== null);
    expect(gate).toBeDefined();
    expect(figuresIn(gate as HTMLElement)).toEqual([]);
    expect(within(gate as HTMLElement).getByText(/Failed to fetch/)).toBeTruthy();
    expect(figuresIn(panelFor("/v1/events/totals")).length).toBeGreaterThan(0);
  });

  it("quotes the gateway's own 404 and 500 bodies verbatim, and renders no figure", async () => {
    overrides.set("/v1/memory/totals", () => json(body("notfound.route"), 404));
    overrides.set("/v1/spend", () => json(body("nodatabase.runs.listing"), 500));
    await renderOverview();

    const memory = panelFor("/v1/memory/totals");
    expect(within(memory).getByText(/SurfaceNotFound/)).toBeTruthy();
    expect(figuresIn(memory)).toEqual([]);

    const spend = panelFor("/v1/spend");
    expect(within(spend).getByRole("alert").textContent).toContain("StorageError");
    expect(figuresIn(spend)).toEqual([]);

    // The page's one `Refresh` reads every route again, the failed ones included.
    const before = counts();
    expect(screen.getAllByRole("button", { name: "Refresh" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await flushed();
    for (const route of REFETCHED) expect(countOf(route), route).toBe((before[route] ?? 0) + 1);
    expect(countOf("/v1/spend")).toBe(2);
  });

  it("reads Recent activity empty when the listing is, and watches nothing without a session", async () => {
    overrides.set(ACTIVITY_ROUTE, () => json(body("empty.messages.recent")));
    overrides.set(SESSIONS_ROUTE, () => json(body("empty.sessions.listing")));
    await renderOverview();

    expect(within(panelFor(ACTIVITY_ROUTE)).queryByRole("list")).toBeNull();
    expect(screen.getByTestId("watched-stream").textContent).toContain(
      "the sessions listing holds no session to watch",
    );
    expect(countOf(WATCHED_STREAM)).toBe(0);
    expect(screen.queryByRole("heading", { name: "Latest run" })).toBeNull();
  });

  it("says so when the sessions listing does not answer, and opens no stream", async () => {
    overrides.set(SESSIONS_ROUTE, () => json(body("nodatabase.runs.listing"), 500));
    await renderOverview();
    expect(screen.getByTestId("watched-stream").textContent).toContain(
      "the sessions listing did not answer (500)",
    );

    cleanup();
    overrides.set(SESSIONS_ROUTE, rejected);
    await renderOverview();
    expect(screen.getByTestId("watched-stream").textContent).toContain(
      "the sessions listing did not answer (unreachable)",
    );
    expect(countOf(WATCHED_STREAM)).toBe(0);
  });

  it("opens a tile's drawer from the keyboard with Enter", async () => {
    await renderOverview();
    const chip = figuresIn(panelFor("/v1/events/totals"))[0] as HTMLElement;
    fireEvent.keyDown(chip, { key: "Enter" });
    expect(within(screen.getByRole("dialog")).getByTestId("cli-twin").textContent).toBe(
      "harnessed events totals --json",
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("a tile's drawer", () => {
  it("traps focus, closes on Escape, and copies a curl line naming its own route", async () => {
    const written: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: { writeText: (line: string) => Promise.resolve(void written.push(line)) },
    });
    await renderOverview();
    const spendTwin = PANELS.find((panel) => panel.route === "/v1/spend")?.twin;
    const chip = figuresIn(panelFor("/v1/spend"))[0] as HTMLElement;
    fireEvent.click(chip);

    // No more `curl` line (D-9): the drawer's raw access is the CLI `--json` twin instead.
    const drawer = screen.getByRole("dialog");
    expect(within(drawer).getByTestId("cli-twin").textContent).toBe(spendTwin);
    const controls = within(drawer).getAllByRole("button");
    const first = controls[0] as HTMLElement;
    const last = controls.at(-1) as HTMLElement;
    expect(document.activeElement).toBe(first);

    fireEvent.keyDown(drawer, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(drawer, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(drawer, { key: "a" });

    fireEvent.click(within(drawer).getByRole("button", { name: "Copy" }));
    expect(written).toEqual([spendTwin]);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(chip);
  });

  it("opens with no motion when the operator asked for none", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
    await renderOverview();
    fireEvent.click(figuresIn(panelFor("/v1/memory/totals"))[0] as HTMLElement);
    expect(screen.getByRole("dialog").dataset.motion).toBe("instant");
  });
});

describe("a watched stream that misbehaves", () => {
  it("signals once a frame split across two reads is whole, never on half of it", async () => {
    await renderOverview();
    const before = counts();
    const cut = RUN_FINISHED.indexOf("data:");

    await arrive(RUN_FINISHED.slice(0, cut));
    expect(counts()).toEqual(before);
    await arrive(RUN_FINISHED.slice(cut));
    await flushed();
    for (const route of REFETCHED) expect(countOf(route)).toBe((before[route] ?? 0) + 1);
  });

  it("backs off 1 s then 2 s when the stream will not open, and refetches nothing for it", async () => {
    overrides.set(WATCHED_STREAM, () => Promise.reject(new TypeError("Failed to fetch")));
    await renderOverview();
    const before = counts();
    expect(screen.getByTestId("watched-stream").textContent).toContain("reconnecting");

    await settle(SECOND);
    await flushed();
    expect(countOf(WATCHED_STREAM)).toBe(2);
    await settle(SECOND);
    await flushed();
    expect(countOf(WATCHED_STREAM)).toBe(2);
    await settle(SECOND);
    await flushed();
    expect(countOf(WATCHED_STREAM)).toBe(3);
    expect(counts()).toEqual(before);
  });

  it("treats a 5xx on the stream as a drop, not a refusal", async () => {
    overrides.set(WATCHED_STREAM, () => json(body("nodatabase.runs.listing"), 500));
    await renderOverview();
    expect(screen.getByTestId("watched-stream").textContent).toContain("reconnecting");
  });
});

describe("a 2xx the wire cannot read", () => {
  it("is that panel's error, quoting the body, never a figure", async () => {
    // Not a gateway body at all - an intermediary's page - so there is no recording to read.
    overrides.set("/v1/events/totals", () =>
      Promise.resolve(new Response("<html>proxy</html>", { status: 200 })),
    );
    await renderOverview();
    const events = panelFor("/v1/events/totals");
    expect(within(events).getByRole("alert").textContent).toContain("UnreadableBody");
    expect(figuresIn(events)).toEqual([]);
  });
});

describe("what a failure says for itself", () => {
  it("reports a rejection as itself, whatever was thrown, on a panel and on the stream", async () => {
    overrides.set("/v1/runs/totals", () => Promise.reject("the socket vanished"));
    overrides.set("/v1/runs/turn_seconds", () => Promise.reject(new Error("")));
    overrides.set(WATCHED_STREAM, () => Promise.reject("the stream vanished"));
    await renderOverview();

    const totals = screen
      .getAllByRole("region")
      .find((region) => within(region).queryByRole("heading", { name: "Turns and tool calls" }));
    expect((totals as HTMLElement).textContent).toContain("the socket vanished");
    const seconds = screen
      .getAllByRole("region")
      .find((region) => within(region).queryByRole("heading", { name: "Average turn" }));
    expect((seconds as HTMLElement).textContent).toContain("Error");
    expect(screen.getByTestId("watched-stream").textContent).toContain("reconnecting");
  });

  it("quotes an error body that is not the gateway's contract under UnreadableBody", async () => {
    // A proxy's page in front of a 502: not a gateway body, so no recording holds it.
    overrides.set("/v1/memory/totals", () =>
      Promise.resolve(new Response("<html>bad gateway</html>", { status: 502 })),
    );
    await renderOverview();
    const alert = within(panelFor("/v1/memory/totals")).getByRole("alert");
    expect(alert.textContent).toContain("UnreadableBody");
    expect(alert.textContent).toContain("<html>bad gateway</html>");
  });
});

describe("leaving the page", () => {
  it("drops the sessions answer, the open stream and a pending reconnect with the page", async () => {
    render(
      <AppShell>
        <Home />
      </AppShell>,
    );
    cleanup();
    await flushed();
    expect(countOf(WATCHED_STREAM)).toBe(0);

    await renderOverview();
    await act(async () => {
      stream?.error(new Error("the socket vanished"));
    });
    await flushed();
    cleanup();
    await settle(30 * SECOND);
    expect(countOf(WATCHED_STREAM)).toBe(1);

    await renderOverview();
    cleanup();
    await flushed();
    expect(countOf(WATCHED_STREAM)).toBe(2);
  });
});

/** The nine routes the Overview holds: six aggregates, the activity list, agents and sessions. */
const NINE = [...REFETCHED, "/v1/agents", SESSIONS_ROUTE];

describe("the Overview is re-read by the shell's one Refresh (tickets 205, 211)", () => {
  it("shows exactly 1 Refresh in the shell header, 0 in main and 0 in any panel footer", async () => {
    await renderOverview();
    const buttons = screen.getAllByRole("button", { name: "Refresh" });
    expect(buttons).toHaveLength(1);
    expect((document.querySelector("header") as HTMLElement).contains(buttons[0] as HTMLElement)).toBe(true);
    expect(within(screen.getByRole("main")).queryAllByRole("button", { name: "Refresh" })).toHaveLength(0);
    for (const footer of document.querySelectorAll("footer")) {
      expect(footer.querySelectorAll("button")).toHaveLength(0);
    }
    // A fresh group draws no footer and so no "Updated" line (FR-163).
    for (const panel of PANELS) expect(panelFor(panel.route).querySelector("footer")).toBeNull();
  });

  it("still counts 1 when one panel's read failed", async () => {
    overrides.set("/v1/spend", () => json(body("nodatabase.runs.listing"), 500));
    await renderOverview();
    expect(screen.getAllByRole("button", { name: "Refresh" })).toHaveLength(1);
    expect(within(panelFor("/v1/spend")).getByRole("button", { name: "Refetch" })).toBeTruthy();
  });

  it("reads all nine routes once more when the shell's Refresh is clicked", async () => {
    await renderOverview();
    const before = Object.fromEntries(NINE.map((route) => [route, countOf(route)]));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await flushed();
    for (const route of NINE) expect(countOf(route), route).toBe((before[route] ?? 0) + 1);
  });

  it("reads all nine routes once more on a terminal frame", async () => {
    await renderOverview();
    const before = Object.fromEntries(NINE.map((route) => [route, countOf(route)]));
    await arrive(RUN_FINISHED);
    await flushed();
    for (const route of NINE) expect(countOf(route), route).toBe((before[route] ?? 0) + 1);
    // The same session is not a new subscription.
    expect(asked.filter((request) => request.url === WATCHED_STREAM)).toHaveLength(1);
  });
});

describe("a refetch signal and the stale mark", () => {
  it("ages every panel past five minutes, and a terminal frame makes each fresh again", async () => {
    await renderOverview();
    await settle(6 * MINUTE);
    for (const panel of PANELS) {
      const frame = panelFor(panel.route);
      // The footer badge keeps counting; the live region freezes the sentence at the crossing.
      expect(within(frame).getByTestId("stale-badge").textContent).toBe("Updated 6 min ago");
      expect(within(frame).getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
      expect(within(frame).getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");
    }

    await arrive(IMPROVE_FINISHED);
    await flushed();
    for (const panel of PANELS) {
      const frame = panelFor(panel.route);
      expect(within(frame).queryByTestId("stale-badge")).toBeNull();
      expect(within(frame).getByTestId("panel-announcement").textContent).toBe("");
    }
  });
});

// ---- ticket 252 (FR-153): TasksTile, NeedsYouList and the Task chip --------------------------------

/** **Hand-written** (decision 33): the stream recordings hold no `task.*` frame. `gateway/sse.py::frame`'s shape. */
function taskFrame(name: string, id: number): string {
  return [`id: ${id}`, `event: ${name}`, 'data: {"task_id":"task_0000000014C0RVA6C5JTBD58CM"}', "", ""].join("\n");
}

function needsYou(): HTMLElement {
  const section = screen.getByRole("heading", { name: "Needs you", level: 2 }).closest("section");
  expect(section).not.toBeNull();
  return section as HTMLElement;
}

describe("TasksTile (FR-153)", () => {
  it("shows the four served figures, each a Value button, with the gloss", async () => {
    await renderOverview();
    const tile = screen.getByRole("heading", { name: "Tasks" }).closest("section") as HTMLElement;
    const served = JSON.parse(body("tasked.tasks.totals")) as Record<string, string>;
    const expected: [string, string][] = [
      ["Completed unattended", served.completed_unattended ?? ""],
      ["Running", served.running ?? ""],
      ["Queued", served.queued ?? ""],
      ["Needs you", served.needs_human ?? ""],
    ];
    for (const [label, text] of expected) {
      const group = within(tile).getByRole("group", { name: label });
      const figure = within(group).getByRole("button");
      expect(figure.textContent).toBe(text);
      expect((figure as HTMLElement).dataset.route).toBe(TOTALS_ROUTE);
    }
    expect(within(tile).getByText("Done with no message from you and no pause. All time.")).toBeTruthy();
    // "Needs you" is not "0" here, so it wears the warn treatment; the headline never does.
    expect(within(tile).getByRole("group", { name: "Needs you" }).parentElement?.className).toMatch(/warn/);
    expect(within(tile).getByRole("group", { name: "Running" }).parentElement?.className ?? "").not.toMatch(/warn/);
  });

  it("renders a served zero as 0 with no warn treatment", async () => {
    overrides.set(TOTALS_ROUTE, () => json(body("empty.tasks.totals")));
    await renderOverview();
    const tile = screen.getByRole("heading", { name: "Tasks" }).closest("section") as HTMLElement;
    const group = within(tile).getByRole("group", { name: "Needs you" });
    expect(within(group).getByRole("button").textContent).toBe("0");
    expect(group.parentElement?.className ?? "").not.toMatch(/warn/);
  });

  it("gives an unknown figure its own treatment, not a zero", async () => {
    const unknown = JSON.stringify({ ...JSON.parse(body("tasked.tasks.totals")), running: null });
    overrides.set(TOTALS_ROUTE, () => json(unknown));
    await renderOverview();
    const tile = screen.getByRole("heading", { name: "Tasks" }).closest("section") as HTMLElement;
    const running = within(within(tile).getByRole("group", { name: "Running" })).getByRole("button");
    expect(running.textContent).toBe("unknown");
  });
});

describe("NeedsYouList (FR-153)", () => {
  it("is the first section on the page, rows in the served order", async () => {
    await renderOverview();
    const first = document.querySelector("section");
    expect(first).toBe(needsYou());

    const served = (JSON.parse(body("tasked.tasks.needs-human")) as { tasks: { text: string; session_id: string }[] }).tasks;
    const rows = within(needsYou()).getAllByRole("listitem");
    expect(rows).toHaveLength(served.length);
    served.forEach((task, index) => {
      expect(rows[index]?.textContent).toContain(task.text);
      const open = within(rows[index] as HTMLElement).getByRole("link", { name: /^Open/ });
      expect(open.getAttribute("href")).toBe(`/console?session=${encodeURIComponent(task.session_id)}`);
    });
  });

  it("builds each pause sentence from the served pause fields only", async () => {
    await renderOverview();
    const rows = within(needsYou()).getAllByRole("listitem");
    expect(rows[0]?.textContent).toContain("recorded-agent asks to run a command: git log --oneline -20");
    expect(rows[1]?.textContent).toContain("The router was not sure: recorded-agent at 41%, below its 60% floor");
    expect(rows[0]?.textContent).toContain("since");
  });

  it("is a ul in a labelled section, with a separate polite status that holds no link", async () => {
    await renderOverview();
    const list = within(needsYou()).getByRole("list");
    expect(list.tagName).toBe("UL");
    expect(needsYou().getAttribute("aria-labelledby")).not.toBeNull();
    expect(list.closest("[role=status]")).toBeNull();
    expect(list.querySelector("[role=status]")).toBeNull();
    const status = screen.getByTestId("needs-you-count");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.querySelector("a, button")).toBeNull();
    // Nothing changed yet, so nothing is announced.
    expect(status.textContent).toBe("");
  });

  it("announces only a change in the row count", async () => {
    await renderOverview();
    const one = JSON.stringify({ tasks: (JSON.parse(body("tasked.tasks.needs-human")) as { tasks: unknown[] }).tasks.slice(0, 1) });
    overrides.set(NEEDS_ROUTE, () => json(one));
    await arrive(RUN_FINISHED);
    expect(screen.getByTestId("needs-you-count").textContent).toBe("1 task needs you");
    // Same count again: the sentence is not re-announced as something new.
    await arrive(taskFrame("task.paused", 910));
    expect(screen.getByTestId("needs-you-count").textContent).toBe("1 task needs you");
  });

  it("reads again on task.paused and task.finished frames", async () => {
    await renderOverview();
    expect(countOf(NEEDS_ROUTE)).toBe(1);
    await arrive(taskFrame("task.paused", 920));
    expect(countOf(NEEDS_ROUTE)).toBe(2);
    expect(countOf(TOTALS_ROUTE)).toBe(2);
    await arrive(taskFrame("task.finished", 921));
    expect(countOf(NEEDS_ROUTE)).toBe(3);
    expect(countOf(TOTALS_ROUTE)).toBe(3);
  });

  it("reads both again on the shell's Refresh", async () => {
    await renderOverview();
    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    await flushed();
    expect(countOf(NEEDS_ROUTE)).toBe(2);
    expect(countOf(TOTALS_ROUTE)).toBe(2);
  });

  it("says Nothing needs you. when the list is empty", async () => {
    overrides.set(NEEDS_ROUTE, () => json(body("empty.tasks")));
    await renderOverview();
    expect(within(needsYou()).getByText("Nothing needs you.")).toBeTruthy();
    expect(within(needsYou()).queryByRole("list")).toBeNull();
  });

  it("shows two skeleton rows while loading and an ErrorState on a failure", async () => {
    overrides.set(NEEDS_ROUTE, () => new Promise<Response>(() => undefined));
    await renderOverview();
    expect(within(needsYou()).getAllByTestId("skeleton-row")).toHaveLength(2);

    cleanup();
    overrides.set(NEEDS_ROUTE, () => json(body("nodatabase.runs.listing"), 500));
    await renderOverview();
    expect(within(needsYou()).getByRole("alert").textContent).toContain("StorageError");
  });
});

describe("Recent activity's Task chip (FR-153)", () => {
  it("marks a message the served author says a task wrote, and no other", async () => {
    overrides.set(ACTIVITY_ROUTE, () => json(body("tasked.messages.recent")));
    await renderOverview();
    const rows = within(screen.getByTestId("activity-list")).getAllByRole("listitem");
    const served = (JSON.parse(body("tasked.messages.recent")) as { messages: { author: string }[] }).messages;
    expect(served.some((message) => message.author === "task")).toBe(true);
    rows.forEach((row, index) => {
      const chip = within(row).queryByText("Task");
      expect(chip !== null, `row ${index}`).toBe(served[index]?.author === "task");
    });
  });

  it("draws no chip on the operator's own messages", async () => {
    await renderOverview();
    expect(within(screen.getByTestId("activity-list")).queryByText("Task")).toBeNull();
  });
});
