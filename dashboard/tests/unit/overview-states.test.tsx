// @vitest-environment jsdom
/**
 * FR-76 (UJ-14, `D-2`, `D-3`): every Overview panel in each state it can reach, and `unknown` is
 * never zero (ticket 135, the Phase D close).
 *
 * The seven states are v3's, owned by `PanelFrame` and `lib/fetch-panel.ts`; this file walks them
 * per panel of `/` rather than once per component, because the Overview is the first screen that
 * reads six new routes at once. `empty` is the one state a totals route **cannot** reach: a count
 * is never an empty list, so a totals body always renders its tiles. The test says so by driving
 * each panel with the recorder's **empty home** - the one place the ledger holds no rows - and
 * showing it renders served `"0"`s and the documented nulls, never `EmptyState`.
 *
 * The inputs are recorded gateway responses only (decision 33, MUST rule 24): the six seeded bodies
 * from ticket 134 and the six `empty.*` bodies recorded for this ticket from the migrated, empty
 * home through `--record-dashboard-fixtures`. The empty home is what serves `average_seconds: null`
 * and `"0"` for every count, so neither is hand-written. The pre-migration `provider: null` is the
 * recorded zero-case eval run's: no Overview tile shows a provider, so its sentence is checked on
 * the tile it would be, keyed on its own route.
 *
 * `verify` is this jsdom suite, so it renders no pixels: the evidence it writes is text - the
 * state matrix per panel, and the null sweep.
 */
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Home from "../../app/page";
import { AppShell } from "../../components/AppShell";
import { StatTile } from "../../components/StatTile";
import { cliTwinFor } from "../../lib/cli-twins";
import { humanWord } from "../../lib/human-words";
import { nullReasonFor } from "../../lib/null-reasons";
import { parseWire } from "../../lib/wire";

vi.setConfig({ testTimeout: 20 * 1000 });

const DASHBOARD = resolve(import.meta.dirname, "../..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE_DIR = resolve(DASHBOARD, "../evidence/135");

const FROZEN = new Date("2026-09-23T09:15:00");
const MINUTE = 60 * 1000;

function body(name: string): string {
  return readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8");
}

function recordedRoute(name: string): string {
  const meta = JSON.parse(
    readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8"),
  ) as { route: string };
  return meta.route;
}

/** The six panels: the route each owns, its heading, and its seeded and empty-home recordings. */
const PANELS = [
  { route: "/v1/spend", headlines: 2, title: "Spent", seeded: "spend", empty: "empty.spend" },
  {
    route: "/v1/runs/turn_seconds",
    headlines: 1,
    title: "Average turn",
    seeded: "runs.turn-seconds",
    empty: "empty.runs.turn-seconds",
  },
  {
    route: "/v1/runs/totals",
    headlines: 2,
    title: "Turns and tool calls",
    seeded: "runs.totals",
    empty: "empty.runs.totals",
  },
  {
    route: "/v1/events/totals",
    headlines: 2,
    title: "Events and sessions",
    seeded: "events.totals",
    empty: "empty.events.totals",
  },
  { route: "/v1/events/gate", headlines: 3, title: "Memory check", seeded: "events.gate", empty: "empty.events.gate" },
  {
    route: "/v1/memory/totals",
    headlines: 2,
    title: "Memory",
    seeded: "memory.totals",
    empty: "empty.memory.totals",
  },
] as const;

type Panel = (typeof PANELS)[number];

/** The home the page is read over: every panel's seeded recording, or every panel's empty one. */
type Home = "seeded" | "empty";

/** The sentences `ux/design.md` gives each Overview null, copied here so a drift is a red. */
const NO_PRICES = "no prices fetched: run `harnessed prices refresh`";
const NO_TIMESTAMPS = "no turn carries both timestamps";
const BEFORE_010 = "recorded before Harnessed recorded the provider and model";

const UNKNOWN = "unknown";

/** What the seeded home's one router decision reported, all time (FR-115, ticket 174). */
const SEEDED_SPEND_TOTAL = "0.000016";

/** The Overview's one panel that is not a totals route: Recent activity, `/v1/messages?limit=`
 * (ticket 187, `ActivityList`, replacing v3's "Latest turns" run listing). FR-54's health panel
 * moved to the Models page's `SystemSection` (ticket 186); `/v1/health` is still fetched below,
 * for `GatewayStatusPill`'s own poll, but is no longer one of this page's own panels. */
const ACTIVITY_ROUTE = "/v1/messages?limit=20";

/** The newest session in the recorded listing, and the stream the page watches for it. */
const WATCHED = "run_000000000DGW1YRVDP3ZCRZF20";
const WATCHED_STREAM = `/v1/sessions/${WATCHED}/stream`;

/** What a null must never look like (FR-76): a measurement, a dash, or nothing. */
const LOOKS_MEASURED = ["0", "$0.00", "-", "", "0.0", "null"];

// ---- the scripted gateway -----------------------------------------------------------------------

let home: Home = "seeded";
/** Whether the sessions listing names a session, and so whether the page opens a stream. */
let watching = false;
let stream: ReadableStreamDefaultController<Uint8Array> | undefined;
let overrides = new Map<string, () => Promise<Response>>();

function json(text: string, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(text, { status, headers: { "content-type": "application/json" } }),
  );
}

function gateway(input: RequestInfo | URL): Promise<Response> {
  const url = String(input);
  const override = overrides.get(url);
  if (override !== undefined) return override();
  const panel = PANELS.find((entry) => entry.route === url);
  if (panel !== undefined) return json(body(home === "seeded" ? panel.seeded : panel.empty));
  if (url === "/v1/health") return json(body("health"));
  // By default no session to watch, so no stream: the panel states are the panels' own.
  if (url === "/v1/sessions") {
    return json(body(watching ? "sessions.listing" : "empty.sessions.listing"));
  }
  if (url === WATCHED_STREAM) {
    const opened = new ReadableStream<Uint8Array>({
      start(controller) {
        stream = controller;
      },
    });
    return Promise.resolve(
      new Response(opened, { status: 200, headers: { "content-type": "text/event-stream" } }),
    );
  }
  if (url === ACTIVITY_ROUTE) return json(body("messages.recent"));
  if (url === "/v1/tasks/totals") return json(body("tasked.tasks.totals"));
  if (url === "/v1/tasks?state=needs_human") return json(body("tasked.tasks.needs-human"));
  return json(body("notfound.route"), 404);
}

async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function renderOverview(): Promise<void> {
  render(
    <AppShell>
      <Home />
    </AppShell>,
  );
  await settle();
  await settle();
  await settle();
}

/** The panel headed `title`: found by its heading, so a panel with no footer is found too. */
function panelTitled(title: string): HTMLElement {
  const heading = screen.getByRole("heading", { name: title });
  const panel = heading.closest("section");
  expect(panel, `${title} sits in a PanelFrame`).not.toBeNull();
  return panel as HTMLElement;
}

function figuresIn(panel: HTMLElement): HTMLElement[] {
  return [...panel.querySelectorAll<HTMLElement>("[data-figure]")];
}

/** The jq path of every `null` at the top of a totals body, walked here and not through the wire. */
function nullPaths(text: string): string[] {
  const parsed = JSON.parse(text) as Record<string, unknown>;
  return Object.keys(parsed)
    .filter((key) => parsed[key] === null)
    .map((key) => `.${key}`);
}

const evidence: Record<string, string[]> = { states: [], "null-sweep": [] };

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  vi.setSystemTime(FROZEN);
  home = "seeded";
  watching = false;
  stream = undefined;
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
    writeFileSync(resolve(EVIDENCE_DIR, `${file}.txt`), `${lines.join("\n")}\n`, "utf8");
  }
});

// ---- AC 1: a test per reachable state, per panel -------------------------------------------------

describe.each(PANELS)("the $title panel ($route)", (panel: Panel) => {
  it("recorded both of its bodies from the emitters, on its own route", () => {
    expect(recordedRoute(panel.seeded)).toBe(panel.route);
    expect(recordedRoute(panel.empty)).toBe(panel.route);
  });

  it("is loading while its read is in flight, and shows no figure", async () => {
    overrides.set(panel.route, () => new Promise<Response>(() => undefined));
    await renderOverview();
    const frame = panelTitled(panel.title);
    // The title, then one skeleton bar per headline figure (FR-163).
    expect(within(frame).getAllByTestId("skeleton-row")).toHaveLength(panel.headlines);
    expect(figuresIn(frame)).toEqual([]);
    evidence.states?.push(`${panel.route} loading: ${panel.headlines} skeleton bars, 0 figures`);
  });

  it("is ok on its seeded recording: every figure its own route's", async () => {
    await renderOverview();
    const figures = figuresIn(panelTitled(panel.title));
    expect(figures.length).toBeGreaterThan(0);
    expect(figures.every((figure) => figure.dataset.route === panel.route)).toBe(true);
    evidence.states?.push(`${panel.route} ok: ${figures.length} figures`);
  });

  it("cannot be empty: over the empty home it renders its counts, never EmptyState", async () => {
    // A count is never an empty list. The empty home is the only state that could have been
    // `empty`, and it serves "0"s and documented nulls, which are tiles, not zero rows.
    home = "empty";
    await renderOverview();
    const frame = panelTitled(panel.title);
    expect(within(frame).queryByText(/The ledger returned 0 rows/)).toBeNull();
    const figures = figuresIn(frame);
    expect(figures.length).toBeGreaterThan(0);
    // Each figure is the empty home's own characters: its "0"s, its nulls as `unknown`, and a
    // non-count leaf (`currency`) as served.
    const recorded = JSON.parse(body(panel.empty)) as Record<string, unknown>;
    for (const figure of figures) {
      const path = figure.dataset.path ?? "";
      const sent = recorded[path.replace(/^\./, "").replace(/\[\d+\]$/, "")];
      const raw = Array.isArray(sent)
        ? String(sent[Number(path.match(/\[(\d+)\]$/)?.[1])])
        : sent === null
          ? UNKNOWN
          : String(sent);
      // `key_kind` renders its human word on screen, the ledger's own word kept in the tooltip
      // (`D-7`); every other figure here is still its route's own characters, verbatim.
      const human = path.startsWith(".key_kinds[") ? humanWord("key_kind", raw) : undefined;
      expect(figure.textContent, `${panel.route} ${path}`).toBe(human ?? raw);
      if (human !== undefined) {
        expect(figure.getAttribute("title"), `${panel.route} ${path}`).toContain(raw);
      }
    }
    const counts = figures.filter((figure) => figure.textContent === "0");
    expect(counts.length, `${panel.route} serves at least one "0"`).toBeGreaterThan(0);
    evidence.states?.push(
      `${panel.route} empty: unreachable for a totals route; the empty home renders ` +
        `${figures.map((figure) => `${figure.dataset.path}=${figure.textContent}`).join(" ")}`,
    );
  });

  it("is absent on the gateway's own 404, quoting it, with no figure", async () => {
    overrides.set(panel.route, () => json(body("notfound.route"), 404));
    await renderOverview();
    const frame = panelTitled(panel.title);
    expect(within(frame).getByRole("alert").textContent).toContain("SurfaceNotFound");
    expect(within(frame).getByText(/^Not found\./)).toBeTruthy();
    expect(figuresIn(frame)).toEqual([]);
    evidence.states?.push(`${panel.route} absent: 404 SurfaceNotFound quoted, 0 figures`);
  });

  it("is an error on the gateway's own 500, quoting it, with no figure", async () => {
    overrides.set(panel.route, () => json(body("nodatabase.runs.listing"), 500));
    await renderOverview();
    const frame = panelTitled(panel.title);
    expect(within(frame).getByRole("alert").textContent).toContain("StorageError");
    expect(figuresIn(frame)).toEqual([]);
    evidence.states?.push(`${panel.route} error: 500 StorageError quoted, 0 figures`);
  });

  it("is unreachable when its read gets no answer, while the others still answer", async () => {
    overrides.set(panel.route, () => Promise.reject(new TypeError("Failed to fetch")));
    await renderOverview();
    const frame = panelTitled(panel.title);
    expect(within(frame).getByText(/Failed to fetch/)).toBeTruthy();
    expect(within(frame).getByText(/got no answer/)).toBeTruthy();
    expect(figuresIn(frame)).toEqual([]);
    const others = PANELS.filter((other) => other.route !== panel.route);
    for (const other of others) expect(figuresIn(panelTitled(other.title)).length).toBeGreaterThan(0);
    evidence.states?.push(`${panel.route} unreachable: cause quoted, 0 figures, 5 others answer`);
  });

  it("is stale past five minutes, and keeps its figures", async () => {
    await renderOverview();
    await settle(6 * MINUTE);
    const frame = panelTitled(panel.title);
    expect(within(frame).getByTestId("stale-badge").textContent).toBe("Updated 6 min ago");
    expect(within(frame).getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
    expect(figuresIn(frame).length).toBeGreaterThan(0);
    evidence.states?.push(`${panel.route} stale: badge after 6 min, figures kept`);
  });
});

// ---- AC 2: each documented null renders unknown with its reason ---------------------------------

describe("a documented null", () => {
  it("spend's total: null reads unknown with the no-prices sentence", async () => {
    home = "empty";
    await renderOverview();
    const frame = panelTitled("Spent");
    const total = frame.querySelector<HTMLElement>('[data-path=".total"]');
    expect(total?.textContent).toBe(UNKNOWN);
    expect(total?.getAttribute("aria-label")).toBe(`${UNKNOWN}, ${NO_PRICES}`);
    expect(within(frame).getByText(NO_PRICES)).toBeTruthy();
  });

  it("turn seconds' average_seconds: null reads unknown with its sentence from null-reasons", async () => {
    expect(nullReasonFor("/v1/runs/turn_seconds", ".average_seconds")).toBe(NO_TIMESTAMPS);
    home = "empty";
    await renderOverview();
    const frame = panelTitled("Average turn");
    const average = frame.querySelector<HTMLElement>('[data-path=".average_seconds"]');
    expect(average?.textContent).toBe(UNKNOWN);
    expect(average?.getAttribute("aria-label")).toBe(`${UNKNOWN}, ${NO_TIMESTAMPS}`);
    expect(within(frame).getByText(NO_TIMESTAMPS)).toBeTruthy();
  });

  it("a pre-migration provider: null reads unknown with the migration it predates", () => {
    const route = recordedRoute("eval.run.zero-case");
    const wire = parseWire(body("eval.run.zero-case"), route);
    const provider = wire.at(".provider");
    expect(provider.kind).toBe("null");
    const reason = nullReasonFor(route, ".provider");
    expect(reason).toBe(BEFORE_010);

    const { container } = render(<StatTile label="provider" leaf={provider} nullReason={reason} />);
    const figure = container.querySelector<HTMLElement>("[data-figure]");
    expect(figure?.textContent).toBe(UNKNOWN);
    expect(container.textContent).toContain(BEFORE_010);
  });
});

// ---- AC 3: the fixture sweep ---------------------------------------------------------------------

describe("the null sweep", () => {
  it.each(["seeded", "empty"] as const)(
    "over the %s home renders 0 of the recorded nulls as 0, $0.00, - or blank",
    async (which) => {
      home = which;
      await renderOverview();
      let nulls = 0;
      let rendered = 0;
      let asZero = 0;

      for (const panel of PANELS) {
        const frame = panelTitled(panel.title);
        const text = body(which === "seeded" ? panel.seeded : panel.empty);
        for (const path of nullPaths(text)) {
          nulls += 1;
          const figures = [...frame.querySelectorAll<HTMLElement>(`[data-path="${path}"]`)];
          for (const figure of figures) {
            rendered += 1;
            if (LOOKS_MEASURED.includes(figure.textContent ?? "")) asZero += 1;
            expect(figure.textContent, `${panel.route} ${path}`).toBe(UNKNOWN);
            expect(figure.getAttribute("aria-label"), `${panel.route} ${path}`).toMatch(
              /^unknown, .+/,
            );
          }
          evidence["null-sweep"]?.push(
            `${which} ${panel.route} ${path}: ${figures.length === 0 ? "not a tile" : "unknown, with its reason"}`,
          );
        }
      }

      expect(asZero).toBe(0);
      if (which === "seeded") {
        // The seeded home priced its one router decision (FR-115, ticket 174), so `total` is no
        // longer a tile null; the two v5 buckets no OpenRouter agent reported (`agent_cost`,
        // `api_equivalent`) still are (ticket 187), each rendered `unknown` with its own reason.
        expect(rendered).toBe(2);
        const total = panelTitled("Spent").querySelector<HTMLElement>('[data-path=".total"]');
        expect(total?.textContent).toBe(SEEDED_SPEND_TOTAL);
        expect(JSON.parse(body("spend")).total).toBe(SEEDED_SPEND_TOTAL);
      } else {
        expect(rendered).toBeGreaterThan(0);
      }
      evidence["null-sweep"]?.push(
        `${which}: ${nulls} recorded nulls, ${rendered} rendered as tiles, ${asZero} rendered as zero`,
      );
    },
  );

  it("walks every recorded totals body, the empty home's included", () => {
    const names = readdirSync(GATEWAY_FIXTURES).filter((name) => name.endsWith(".body"));
    for (const panel of PANELS) {
      expect(names).toContain(`${panel.seeded}.body`);
      expect(names).toContain(`${panel.empty}.body`);
    }
  });
});

// ---- AC 4: a served "0" is a measurement, and never looks like unknown ----------------------------

describe("a served zero", () => {
  it("reads 0 with its route beneath it, textually and visually distinct from unknown", async () => {
    home = "empty";
    await renderOverview();

    const events = panelTitled("Events and sessions");
    const zero = events.querySelector<HTMLElement>('[data-path=".events"]') as HTMLElement;
    expect(zero.textContent).toBe("0");
    expect(JSON.parse(body("empty.events.totals")).events).toBe("0");

    // The group names no route (D-9) and draws no "Updated" line of its own (FR-163).
    expect(events.querySelector("footer")).toBeNull();
    expect(events.textContent).not.toContain("/v1/events/totals");

    const unknown = panelTitled("Average turn").querySelector<HTMLElement>(
      '[data-path=".average_seconds"]',
    ) as HTMLElement;
    expect(unknown.textContent).toBe(UNKNOWN);

    // Textually: a figure against a word, and only the absence announces a reason.
    expect(zero.textContent).not.toBe(unknown.textContent);
    expect(zero.getAttribute("aria-label")).toBeNull();
    expect(unknown.getAttribute("aria-label")).toBe(`${UNKNOWN}, ${NO_TIMESTAMPS}`);
    expect(zero.parentElement?.textContent).not.toContain(NO_TIMESTAMPS);

    // Visually: the two never share a class, so they never share a treatment.
    expect(zero.className).not.toBe("");
    expect(unknown.className).not.toBe("");
    expect(zero.className).not.toBe(unknown.className);
  });
});

// ---- the Overview's other panel: a list can be empty (`/v1/messages?limit=`, Recent activity) ---

const OTHERS = [
  { route: ACTIVITY_ROUTE, title: "Recent activity", empty: "empty.messages.recent" },
] as const;

describe.each(OTHERS)("the $title panel ($route)", (panel) => {
  it("is loading while its read is in flight", async () => {
    overrides.set(panel.route, () => new Promise<Response>(() => undefined));
    await renderOverview();
    expect(within(panelTitled(panel.title)).getAllByTestId("skeleton-row")).toHaveLength(3);
    evidence.states?.push(`${panel.route} loading: 3 skeleton rows`);
  });

  it("is ok on its recording, every figure its own route's", async () => {
    await renderOverview();
    const figures = figuresIn(panelTitled(panel.title));
    expect(figures.length).toBeGreaterThan(0);
    const routes = new Set(figures.map((figure) => figure.dataset.route));
    expect(routes).toEqual(new Set([panel.route]));
    evidence.states?.push(`${panel.route} ok: ${figures.length} figures`);
  });

  it("is empty and says which route held zero rows", async () => {
    const emptyBody = body(panel.empty);
    overrides.set(panel.route, () => json(emptyBody));
    await renderOverview();
    const frame = panelTitled(panel.title);
    // The noun is the panel's own title, lowercased - never the route that answered (D-9).
    expect(within(frame).getByText(`No ${panel.title.toLowerCase()} recorded yet.`)).toBeTruthy();
    expect(within(frame).getByText("harnessed run")).toBeTruthy();
    expect(within(frame).queryByRole("table")).toBeNull();
    evidence.states?.push(`${panel.route} empty: 0 rows named, CLI twin offered`);
  });

  it("is absent on a 404, an error on a 500, and unreachable on no answer", async () => {
    overrides.set(panel.route, () => json(body("notfound.route"), 404));
    await renderOverview();
    expect(within(panelTitled(panel.title)).getByRole("alert").textContent).toContain(
      "SurfaceNotFound",
    );

    cleanup();
    overrides.set(panel.route, () => json(body("nodatabase.runs.listing"), 500));
    await renderOverview();
    expect(within(panelTitled(panel.title)).getByRole("alert").textContent).toContain(
      "StorageError",
    );

    cleanup();
    overrides.set(panel.route, () => Promise.reject(new TypeError("Failed to fetch")));
    await renderOverview();
    expect(within(panelTitled(panel.title)).getByText(/got no answer/)).toBeTruthy();
    evidence.states?.push(`${panel.route} absent, error, unreachable: each quoted, 0 figures`);
  });

  it("is stale past five minutes", async () => {
    await renderOverview();
    await settle(6 * MINUTE);
    const badges = within(panelTitled(panel.title)).getAllByTestId("stale-badge");
    expect(badges.length).toBeGreaterThan(0);
    evidence.states?.push(`${panel.route} stale: badge after 6 min`);
  });
});

// ---- every figure's source, in the ok state ------------------------------------------------------

describe("an ok tile's source", () => {
  it("opens its own route's body at its own path, and closes back to the chip", async () => {
    await renderOverview();
    for (const panel of PANELS) {
      const frame = panelTitled(panel.title);
      // Every figure is its own trigger now: no separate `SourceChip`.
      const chips = figuresIn(frame);
      expect(chips.length).toBeGreaterThan(0);
      const chip = chips[0] as HTMLElement;
      fireEvent.click(chip);
      const drawer = screen.getByRole("dialog");
      expect(within(drawer).getByRole("heading", { name: "Where this came from" })).toBeTruthy();
      fireEvent.keyDown(document, { key: "Escape" });
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(document.activeElement).toBe(chip);
    }
  });
});

// ---- the watched stream's line: its own states, beside the panels' ------------------------------

async function arrive(frame: string): Promise<void> {
  await act(async () => {
    stream?.enqueue(new TextEncoder().encode(frame));
  });
  await settle();
  await settle();
}

function watchLine(): string {
  return screen.getByTestId("watched-stream").textContent ?? "";
}

/** The recorded `run.finished` frame of the seeded session's stream. */
function recordedFrame(event: string): string {
  const frames = readFileSync(resolve(GATEWAY_FIXTURES, "sessions.stream.sse"), "utf8")
    .split("\n\n")
    .filter((block) => block.includes(`\nevent: ${event}\n`));
  if (frames[0] === undefined) throw new Error(`no recorded ${event}`);
  return `${frames[0]}\n\n`;
}

describe("the watched stream", () => {
  it("says so when there is no session to watch, and opens nothing", async () => {
    await renderOverview();
    expect(watchLine()).toContain("the sessions listing holds no session to watch");
  });

  it("is connecting, then live, and a terminal frame reads every panel again", async () => {
    watching = true;
    await renderOverview();
    expect(watchLine()).toContain(WATCHED);
    expect(watchLine()).toContain("connecting");
    await settle(6 * MINUTE);
    expect(within(panelTitled("Events and sessions")).getByTestId("stale-badge")).toBeTruthy();

    await arrive(recordedFrame("run.finished"));
    expect(watchLine()).toContain("live");
    // The refetch is a fresh read, so the stale mark is gone and the figures are back.
    expect(within(panelTitled("Events and sessions")).queryByTestId("stale-badge")).toBeNull();
    expect(figuresIn(panelTitled("Events and sessions")).length).toBeGreaterThan(0);
  });

  it("reconnects after a drop, and ends on a clean close or a refusal", async () => {
    watching = true;
    await renderOverview();
    await act(async () => {
      stream?.error(new Error("the socket vanished"));
    });
    await settle();
    expect(watchLine()).toContain("reconnecting");
    await settle(1000);
    await settle();
    await act(async () => {
      stream?.close();
    });
    await settle();
    expect(watchLine()).toContain("finished");

    cleanup();
    overrides.set(WATCHED_STREAM, () => json(body("notfound.sessions.stream"), 404));
    await renderOverview();
    expect(watchLine()).toContain("could not connect (404)");
  });

  it("says the listing did not answer, with its status or unreachable", async () => {
    overrides.set("/v1/sessions", () => json(body("nodatabase.runs.listing"), 500));
    await renderOverview();
    expect(watchLine()).toContain("the sessions listing did not answer (500)");

    cleanup();
    overrides.set("/v1/sessions", () => Promise.reject(new TypeError("Failed to fetch")));
    await renderOverview();
    expect(watchLine()).toContain("the sessions listing did not answer (unreachable)");
  });
});

// ---- the error state's other two causes: a body no gateway contract spells ----------------------

describe.each(PANELS)("the $title panel's error on an unreadable body", (panel: Panel) => {
  it("names a 2xx it cannot read, and a proxy's 502, as UnreadableBody, quoted", async () => {
    // Not gateway bodies at all - an intermediary's pages - so no recording holds them.
    overrides.set(panel.route, () =>
      Promise.resolve(new Response("<html>proxy</html>", { status: 200 })),
    );
    await renderOverview();
    const alert = within(panelTitled(panel.title)).getByRole("alert");
    expect(alert.textContent).toContain("UnreadableBody");
    expect(figuresIn(panelTitled(panel.title))).toEqual([]);

    cleanup();
    overrides.set(panel.route, () =>
      Promise.resolve(new Response("<html>bad gateway</html>", { status: 502 })),
    );
    await renderOverview();
    const quoted = within(panelTitled(panel.title)).getByRole("alert").textContent;
    expect(quoted).toContain("UnreadableBody");
    expect(quoted).toContain("<html>bad gateway</html>");
    evidence.states?.push(`${panel.route} error: an unreadable 2xx and a proxy 502, both quoted`);
  });
});

describe("an ok tile's drawer, from the keyboard", () => {
  it("traps Tab at both edges, copies its curl line, and opens instantly under reduced motion", async () => {
    const written: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: { writeText: (line: string) => Promise.resolve(void written.push(line)) },
    });
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
    await renderOverview();
    const chip = figuresIn(panelTitled("Average turn"))[0] as HTMLElement;
    fireEvent.keyDown(chip, { key: "Enter" });

    const drawer = screen.getByRole("dialog");
    expect(drawer.dataset.motion).toBe("instant");
    const controls = within(drawer).getAllByRole("button");
    const first = controls[0] as HTMLElement;
    const last = controls.at(-1) as HTMLElement;
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(drawer, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(drawer, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(drawer, { key: "Tab" });
    fireEvent.keyDown(drawer, { key: "a" });

    // No more `curl` line (D-9): the drawer's raw access is the CLI `--json` twin instead.
    const twin = cliTwinFor("/v1/runs/turn_seconds");
    expect(within(drawer).getByTestId("cli-twin").textContent).toBe(twin);
    fireEvent.click(within(drawer).getByRole("button", { name: "Copy" }));
    expect(written).toEqual([twin]);
    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("the watched stream when it will not open", () => {
  it("treats a 5xx and a rejection as drops, and a frame split across reads as one", async () => {
    watching = true;
    overrides.set(WATCHED_STREAM, () => json(body("nodatabase.runs.listing"), 500));
    await renderOverview();
    expect(watchLine()).toContain("reconnecting");

    cleanup();
    overrides.set(WATCHED_STREAM, () => Promise.reject("the stream vanished"));
    await renderOverview();
    expect(watchLine()).toContain("reconnecting");
    await settle(1000);
    await settle(2000);
    expect(watchLine()).toContain("reconnecting");

    cleanup();
    overrides.delete(WATCHED_STREAM);
    await renderOverview();
    const frame = recordedFrame("run.finished");
    const cut = frame.indexOf("data:");
    await arrive(frame.slice(0, cut));
    expect(watchLine()).toContain("connecting");
    await arrive(frame.slice(cut));
    expect(watchLine()).toContain("live");
  });
});

// ---- ticket 252: the tasks reads ------------------------------------------------------------------

describe("the Tasks tile ($/v1/tasks/totals)", () => {
  it("is loading, then ok with every figure its own route's", async () => {
    overrides.set("/v1/tasks/totals", () => new Promise<Response>(() => undefined));
    await renderOverview();
    expect(within(panelTitled("Tasks")).getAllByTestId("skeleton-row")).toHaveLength(3);

    cleanup();
    overrides.delete("/v1/tasks/totals");
    await renderOverview();
    const figures = figuresIn(panelTitled("Tasks"));
    expect(figures).toHaveLength(4);
    expect(new Set(figures.map((figure) => figure.dataset.route))).toEqual(new Set(["/v1/tasks/totals"]));
  });

  it("renders a served zero as 0, not as empty or unknown", async () => {
    overrides.set("/v1/tasks/totals", () => json(body("empty.tasks.totals")));
    await renderOverview();
    const figures = figuresIn(panelTitled("Tasks"));
    expect(figures.map((figure) => figure.textContent)).toEqual(["0", "0", "0", "0"]);
  });
});
