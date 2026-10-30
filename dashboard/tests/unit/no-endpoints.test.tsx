// @vitest-environment jsdom
/**
 * FR-126 (`D-9`, decision 68, MUST rule 31): no endpoint string is drawn anywhere, and a scan
 * proves it. Two scans share one predicate (`tests/support/endpoint-scan.ts`): the DOM scan over
 * what a viewer receives, and the static scan over the rendered string literals of `app/` and
 * `components/`. Each is shown able to fail on a seeded "GET /v1/runs" in a `title` (MUST rule 12).
 * The e2e a11y and read-only walks repeat the DOM scan on every page of the built export.
 *
 * The recorded gateway bodies stand in for the gateway (decision 33): one answer per recorded
 * route, nothing invented. Evidence goes to `evidence/191/` as text.
 */
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterAll, afterEach, describe, expect, it, vi } from "vitest";

import type { ReactNode } from "react";

vi.mock("next/navigation", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  useSearchParams: (await import("./support/query")).useSearchParams,
  useRouter: () => ({ push: () => undefined, replace: () => undefined }),
}));

import ConsolePage from "../../app/console/page";
import EvalsPage from "../../app/evals/page";
import ImprovePage from "../../app/improve/page";
import MemoryPage from "../../app/memory/page";
import Home from "../../app/page";
import TracesPage from "../../app/traces/page";
import ModelsPage from "../../app/models/page";
import { AbsentState } from "../../components/AbsentState";
import { AppShell } from "../../components/AppShell";
import { EmptyState } from "../../components/EmptyState";
import { GatewayStatusPill } from "../../components/GatewayStatusPill";
import { StreamPanel } from "../../components/StreamPanel";
import { UnreachableState } from "../../components/UnreachableState";
import { CLI_TWINS, cliTwinFor } from "../../lib/cli-twins";
import { NULL_REASONS, exactReasonFor } from "../../lib/null-reasons";
import { parseWire } from "../../lib/wire";

import { pageAt, query } from "./support/query";
import { Leaky } from "../support/endpoint.fixture";
import {
  collectStrings,
  endpointFindings,
  scanStrings,
  sourceFiles,
  staticFindings,
} from "../support/endpoint-scan";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(DASHBOARD, "../evidence/191/no-endpoints.txt");
const FIXTURE_SOURCE = resolve(DASHBOARD, "tests/support/endpoint.fixture.tsx");

const log: string[] = [];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

afterAll(() => {
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
});

// ---- The recorded gateway ----------------------------------------------------------------------

/** Every recorded 200, keyed on its route. The first recording of a route wins. */
function recordedReads(): Map<string, string> {
  const reads = new Map<string, string>();
  for (const file of readdirSync(FIXTURES).filter((name) => name.endsWith(".meta.json")).sort()) {
    const meta = JSON.parse(readFileSync(resolve(FIXTURES, file), "utf8")) as { route: string; status: number };
    if (meta.status !== 200 || reads.has(meta.route)) continue;
    const stem = resolve(FIXTURES, file.replace(/\.meta\.json$/, ""));
    const path = [`${stem}.body`, `${stem}.sse`].find((candidate) => existsSync(candidate));
    if (path !== undefined) reads.set(meta.route, readFileSync(path, "utf8"));
  }
  return reads;
}

function answer(status: number, text: string): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => null },
    body: null,
    text: async () => text,
  } as unknown as Response;
}

function stubGateway(): void {
  const reads = recordedReads();
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const body = reads.get(url);
      return body === undefined ? answer(404, '{"error":"NotFound","message":"not here"}') : answer(200, body);
    }),
  );
}

async function settle(): Promise<void> {
  await act(async () => {});
  await act(async () => {});
}

/** Open every figure's drawer in turn, and every Details disclosure inside it, scanning each. */
async function scanEveryDrawer(): Promise<{ figures: number; findings: string[] }> {
  const findings: string[] = [];
  const figures = Array.from(document.querySelectorAll<HTMLElement>("button[data-figure]"));
  for (const figure of figures) {
    fireEvent.click(figure);
    const drawer = screen.queryByRole("dialog");
    if (drawer === null) continue;
    for (const disclosure of Array.from(drawer.querySelectorAll<HTMLElement>("button[aria-expanded='false']"))) {
      fireEvent.click(disclosure);
    }
    findings.push(...scanStrings(collectStrings(document)));
    fireEvent.keyDown(document, { key: "Escape" });
  }
  return { figures: figures.length, findings };
}

// ---- The DOM scan ------------------------------------------------------------------------------

describe("the DOM scan", () => {
  // Slow step: scanEveryDrawer opens every figure drawer and disclosure in jsdom; 30 s covers a loaded host (ticket 228).
  it("finds 0 endpoint strings on the Overview, drawers and disclosures opened", { timeout: 30_000 }, async () => {
    stubGateway();
    render(
      <AppShell>
        <Home />
      </AppShell>,
    );
    await settle();
    expect(scanStrings(collectStrings(document))).toEqual([]);
    const opened = await scanEveryDrawer();
    expect(opened.figures).toBeGreaterThan(0);
    expect(opened.findings).toEqual([]);
    log.push(`/: ${opened.figures} figures opened, 0 endpoint strings`);
  });

  // Slow step: scanEveryDrawer opens every figure drawer and disclosure in jsdom; 30 s covers a loaded host (ticket 228).
  it("finds 0 endpoint strings on Models, drawers and disclosures opened", { timeout: 30_000 }, async () => {
    stubGateway();
    render(<AppShell>{ModelsPage()}</AppShell>);
    await settle();
    expect(scanStrings(collectStrings(document))).toEqual([]);
    const opened = await scanEveryDrawer();
    expect(opened.figures).toBeGreaterThan(0);
    expect(opened.findings).toEqual([]);
    log.push(`/models: ${opened.figures} figures opened, 0 endpoint strings`);
  });

  // FR-174 (ticket 303): no page draws a raw ISO-8601 stamp as visible text; the exact stamp stays in the drawer.
  const ISO_STAMP = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

  it("finds 0 raw ISO-8601 stamps in the visible text of Overview, Models and Evals", async () => {
    stubGateway();
    for (const page of [<Home key="home" />, ModelsPage(), <EvalsPage key="evals" />]) {
      render(<AppShell>{page}</AppShell>);
      await settle();
      expect(document.body.textContent ?? "").not.toMatch(ISO_STAMP);
      cleanup();
    }
    log.push("visible text: 0 ISO-8601 stamps");
  });

  it("fires on a seeded 'GET /v1/runs' in a title", () => {
    render(<Leaky leaf={{ route: "/v1/runs", path: ".a" }} />);
    const found = scanStrings(collectStrings(document));
    expect(found.length).toBeGreaterThan(0);
    expect(found.join("\n")).toContain("method prefix");
    expect(found.join("\n")).toContain("gateway route");
    log.push(`seeded title: DOM scan reported ${found.length}`);
  });

  it("names every shape it forbids", () => {
    const samples: Record<string, string> = {
      "gateway route": "/v1/runs",
      "control route": "/control/v1/runs",
      "method prefix": "POST /runs",
      "host and port": "localhost:8756",
      "byte count": "1,204 bytes",
      curl: "Curl it",
      "route to path": "route -> $.spend",
      "json path caption": "$.turns",
    };
    for (const [name, sample] of Object.entries(samples)) expect(endpointFindings(sample)).toContain(name);
    expect(endpointFindings("harnessed runs totals --json")).toEqual([]);
  });
});

// ---- The static scan ---------------------------------------------------------------------------

describe("the static scan", () => {
  // Slow by nature: reads and scans every source file under app/ and components/; 5.5 s on the operator's PC (ticket 228 follow-up).
  it("finds 0 in the rendered literals of app/ and components/", { timeout: 30_000 }, () => {
    const files = sourceFiles(DASHBOARD, ["app", "components"]);
    expect(files.length).toBeGreaterThan(50);
    expect(staticFindings(files)).toEqual([]);
    log.push(`static: ${files.length} files, 0 endpoint strings`);
  });

  it("fires on the seeded component", () => {
    const found = staticFindings([FIXTURE_SOURCE]);
    expect(found.length).toBeGreaterThan(0);
    expect(found.join("\n")).toContain("route to path");
    log.push(`seeded title: static scan reported ${found.length}`);
  });
});

// ---- The states --------------------------------------------------------------------------------

describe("the state components", () => {
  it("draw no host, port, URL, read name, cursor or seq", async () => {
    stubGateway();
    render(<GatewayStatusPill />);
    await settle();
    const drawn = [collectStrings(document)];
    cleanup();

    render(<UnreachableState cause="ECONNREFUSED" />);
    drawn.push(collectStrings(document));
    cleanup();

    render(<EmptyState title="Runs" emptyText="Nothing ran." cliTwin="harnessed run" />);
    drawn.push(collectStrings(document));
    cleanup();

    render(<AbsentState error="NotFound" message="no such run" absentText="It was never recorded." />);
    drawn.push(collectStrings(document));
    cleanup();

    const body = '{"session_id":"run_1","events":[{"seq":7,"name":"loop.turn"}]}';
    const wire = parseWire(body, "/v1/sessions/run_1/events");
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("Failed to fetch"))));
    render(<StreamPanel sessionId="run_1" afterSeq={wire.at(".events[0].seq")} />);
    await settle();
    const stream = collectStrings(document);
    drawn.push(stream);

    expect(scanStrings(drawn.flat())).toEqual([]);
    const text = stream.join(" ");
    expect(text).not.toMatch(/cursor|seq\b|\b7\b/i);
    log.push("GatewayStatusPill, UnreachableState, EmptyState, AbsentState, StreamPanel: 0 endpoint strings");
  });
});

// ---- The twin walk -----------------------------------------------------------------------------

/**
 * Reads the gateway serves that no CLI command prints. They get no drawer twin (`cli-twins.ts`:
 * "a route with no twin gets nothing rather than a plausible guess"). Named here so a new read
 * that lacks one is a failure and this list cannot grow silently.
 */
const NO_CLI_SURFACE = new Set<string>([]);

/** A recorded route as a template: an id segment (`run_...`, `mem_...`, `imp_...`) is `{id}`. */
function templateOf(route: string): string {
  return route.split("?")[0]!.replace(/\/(?:run|mem|imp|eval)_[0-9A-Z]+/g, "/{id}");
}

describe("lib/cli-twins.ts", () => {
  it("gives every recorded read a --json twin but the named few, and no twin names a route", () => {
    const missing = new Set<string>();
    for (const route of recordedReads().keys()) {
      if (cliTwinFor(route) === undefined) missing.add(templateOf(route));
    }
    expect([...missing].sort()).toEqual([...NO_CLI_SURFACE].sort());

    for (const v5 of [
      "/v1/sessions/run_1/decisions",
      "/v1/sessions/run_1/messages",
      "/v1/messages",
      "/v1/router",
      "/v1/agents",
      // The eight reads ticket 232 gave a twin (FR-126 restored): none is left without one.
      "/v1/health",
      "/v1/runs",
      "/v1/promotions",
      "/v1/runs/run_1/trace",
      "/v1/sessions/run_1/events",
      "/v1/sessions/run_1/stream",
      "/v1/memory/facts/mem_1",
      "/v1/improve/cycles/imp_1",
    ]) {
      expect(cliTwinFor(v5), v5).toBeDefined();
    }

    for (const { command } of CLI_TWINS) {
      expect(command.startsWith("harnessed "), command).toBe(true);
      expect(endpointFindings(command), command).toEqual([]);
    }
    log.push(`twin walk: ${CLI_TWINS.length} twins, every command a harnessed line; no CLI surface: ${[...NO_CLI_SURFACE].join(", ")}`);
  });
});

// ---- The visible-text scan (ticket 232, G-57, V6-1, V6-10) ---------------------------------------

/** Every name `events/names.py` freezes, read as text: a quoted dotted name in its `NAMES` set. */
function frozenEventNames(): string[] {
  const source = readFileSync(resolve(DASHBOARD, "../src/harnessed/events/names.py"), "utf8");
  return [...source.matchAll(/^\s+"([a-z_]+(?:\.[a-z_]+)+)",/gm)].map((match) => match[1] as string);
}

/** What an operator must never read as build jargon or stream text. */
function jargonFindings(text: string, names: readonly string[]): string[] {
  const found: string[] = [];
  if (/migration\s+\d+/i.test(text)) found.push("migration number");
  if (/ticket\s+\d+/i.test(text)) found.push("ticket number");
  if (/\bstream\b/i.test(text)) found.push('the word "stream"');
  for (const name of names) if (text.includes(name)) found.push(`event name ${name}`);
  return found;
}

/**
 * The visible text of the page: everything drawn except a served value. A served value is a
 * `Value` figure (`data-figure`): messages, replies and fact text arrive through those and are the
 * gateway's words, not the dashboard's, so they are excluded; every label, sentence, reason and
 * status the dashboard writes itself is read.
 */
function visibleText(root: HTMLElement): string {
  const clone = root.cloneNode(true) as HTMLElement;
  for (const served of Array.from(clone.querySelectorAll("[data-figure]"))) served.remove();
  return clone.textContent ?? "";
}

type Fixture = { route: string; status: number; text: string };

/** Every recorded answer, by state: the first 200 per route, the empties, and the 404s. */
function recordedStates(): { ok: Map<string, string>; empty: Map<string, string> } {
  const ok = new Map<string, string>();
  const empty = new Map<string, string>();
  for (const file of readdirSync(FIXTURES).filter((name) => name.endsWith(".meta.json")).sort()) {
    const meta = JSON.parse(readFileSync(resolve(FIXTURES, file), "utf8")) as { route: string; status: number };
    if (meta.status !== 200) continue;
    const stem = resolve(FIXTURES, file.replace(/\.meta\.json$/, ""));
    const path = [`${stem}.body`, `${stem}.sse`].find((candidate) => existsSync(candidate));
    if (path === undefined) continue;
    const target = file.startsWith("empty.") ? empty : ok;
    if (!target.has(meta.route)) target.set(meta.route, readFileSync(path, "utf8"));
  }
  return { ok, empty };
}

const PAGES: { name: string; page: () => ReactNode; params: () => Record<string, string> }[] = [
  { name: "/", page: Home as () => ReactNode, params: () => ({}) },
  { name: "/models", page: ModelsPage as () => ReactNode, params: () => ({}) },
  { name: "/console", page: ConsolePage, params: () => ({}) },
  { name: "/console?session", page: ConsolePage, params: () => ({ session: firstSession() }) },
  { name: "/traces", page: TracesPage, params: () => ({}) },
  { name: "/traces?session", page: TracesPage, params: () => ({ session: firstSession() }) },
  { name: "/evals", page: EvalsPage, params: () => ({}) },
  { name: "/improve", page: ImprovePage, params: () => ({}) },
  { name: "/memory", page: MemoryPage, params: () => ({}) },
  { name: "/memory?q", page: MemoryPage, params: () => ({ q: "ping" }) },
];

/** The recorded run's session id, so a page that opens one asks for a route that was recorded. */
function firstSession(): string {
  for (const route of recordedStates().ok.keys()) {
    const found = /^\/v1\/sessions\/(run_[0-9A-Z]+)\/messages$/.exec(route);
    if (found !== null) return found[1] as string;
  }
  throw new Error("the record holds no session messages");
}

type World = { name: string; fetch: (url: string) => Promise<Response> };

function worlds(): World[] {
  const { ok, empty } = recordedStates();
  const body = (read: Map<string, string>) => (url: string) =>
    Promise.resolve(
      read.has(url) ? answer(200, read.get(url) as string) : answer(404, '{"error":"NotFound","message":"not here"}'),
    );
  return [
    { name: "recorded", fetch: body(ok) },
    { name: "empty", fetch: body(new Map([...ok, ...empty])) },
    { name: "absent", fetch: () => Promise.resolve(answer(404, '{"error":"NotFound","message":"not here"}')) },
    { name: "unreachable", fetch: () => Promise.reject(new Error("Failed to fetch")) },
  ];
}

describe("the visible-text scan (G-57)", () => {
  const names = frozenEventNames();

  it("reads the frozen event names from the Python list", () => {
    expect(names.length).toBeGreaterThan(30);
    expect(names).toContain("run.finished");
  });

  it("fires on each shape it forbids", () => {
    for (const sample of ["recorded before migration 010", "ticket 232", "the stream ended", "loop.route.decided"]) {
      expect(jargonFindings(sample, names), sample).not.toEqual([]);
    }
    expect(jargonFindings("recorded before Harnessed measured this", names)).toEqual([]);
  });

  it("finds 0 in the sentence of every documented null reason", () => {
    expect(NULL_REASONS.length).toBeGreaterThan(20);
    for (const { reason } of NULL_REASONS) expect(jargonFindings(reason, names), reason).toEqual([]);
    // The exact reason is kept for the drawer, which is why it may keep the number.
    expect(exactReasonFor("/v1/eval/runs/{id}", ".provider")).toBe("recorded before migration 010");
  });

  // Slow by nature: every page in four states under jsdom; 120 s covers a loaded host.
  it("finds 0 in the visible text of every page in every fixture state", { timeout: 120_000 }, async () => {
    const report: string[] = [];
    const offenders: string[] = [];
    for (const world of worlds()) {
      for (const { name, page, params } of PAGES) {
        vi.stubGlobal("matchMedia", (queryString: string) => ({ matches: true, media: queryString }));
        vi.stubGlobal("fetch", vi.fn((url: string) => world.fetch(url)));
        const { container } = render(<AppShell>{pageAt(page, params())}</AppShell>);
        await settle();
        await settle();
        const found = jargonFindings(visibleText(container), names);
        if (found.length > 0) offenders.push(`${world.name} ${name}: ${[...new Set(found)].join(", ")}`);
        report.push(`${world.name} ${name}: ${found.length} matches`);
        cleanup();
        query.current = {};
      }
    }
    log.push(`visible-text scan:\n${report.join("\n")}`);
    expect(offenders).toEqual([]);
  });
});
