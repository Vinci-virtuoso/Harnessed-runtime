// @vitest-environment jsdom
/**
 * FR-60 (MUST rule 25): across every page, **0** links and **0** lookups build an id.
 *
 * The screen tickets each proved their own page renders what the gateway sent. This one asks a
 * single property of all thirteen at once, because "no link anywhere derives an id" is not a
 * claim any one page can make: a walk that misses a page misses the defect. So every page is
 * rendered against its recorded responses, every internal link is collected, and every variable
 * part of every href is compared, character for character, with the string leaves of the bodies
 * that page rendered (`tests/support/links.ts`).
 *
 * Four claims, each able to fail on its own:
 *   - the page walk: every id segment and every id query value is a leaf, verbatim;
 *   - `[GAP G-4]`: a `case_run` key holds an eval run id and three colons, and `/runs` links it
 *     whole to its own page and nowhere else - no part of it becomes a link;
 *   - the four `IdLookup`s: what is typed arrives in the target URL as typed, spaces and all,
 *     and `Open` is disabled while the field is blank;
 *   - the seeded failure: a test-only component links a case run row to the eval run id it got
 *     by splitting the key, and the walk reports it (MUST rule 12).
 *
 * Nothing here edits a recorded fixture (MUST rule 24), and the route words a walk treats as
 * static are read off the `app/` directory rather than listed here, so a route added later is
 * either a directory this walk already knows or an id it reports.
 */
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Home from "../../app/page";
import CompareForward from "../../app/evals/compare/page";
import EvalsPage from "../../app/evals/page";
import ImproveForward from "../../app/improve/page";
import EpisodesForward from "../../app/memory/episodes/page";
import FactsForward from "../../app/memory/facts/page";
import MemoryPage from "../../app/memory/page";
import ProceduresForward from "../../app/memory/procedures/page";
import RunsForward from "../../app/runs/page";
import SessionsForward from "../../app/sessions/page";
import { runHref, sessionHref } from "../../app/traces/limit";
import TracesPage from "../../app/traces/page";
import ModelsPage from "../../app/models/page";
import { AppShell, PAGES } from "../../components/AppShell";
import { pageAt } from "./support/query";

import {
  builtIds,
  idCarriedBy,
  internalLinks,
  leavesOf,
  queryParams,
  stringLeaves,
  type BuiltId,
  type PageLink,
} from "../support/links";

import type { ReactNode } from "react";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const APP = resolve(DASHBOARD, "app");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(DASHBOARD, "../evidence/113/links.txt");

/** The gateway this walk's shell is pointed at. Not the operator's 8756 (decision 30). */

/** Where a limit choice or a lookup sent the operator. */
let pushed: string[] = [];

vi.mock("next/navigation", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  useSearchParams: (await import("./support/query")).useSearchParams,
  useRouter: () => ({
    push: (href: string) => {
      pushed.push(href);
    },
    replace: (href: string) => {
      pushed.push(href);
    },
  }),
}));

// ---- The recorded responses these pages are walked against -------------------------------------

type Recorded = { route: string; code: number; text: string };

/** One recorded response, read from the committed record and never written by a test. */
function recorded(name: string): Recorded {
  const meta = JSON.parse(readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8")) as {
    route: string;
    status: number;
  };
  return {
    route: meta.route,
    code: meta.status,
    text: readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8"),
  };
}

const health = recorded("health");
const gate = recorded("events.gate");
const totals = recorded("memory.totals");
const runsListing = recorded("runs.listing");
const sessionRun = recorded("runs.session");
const runTrace = recorded("runs.trace");
const caseRun = recorded("runs.case-run");
const noCaseTrace = recorded("notfound.runs.trace");
const sessionsListing = recorded("sessions.listing");
const sessionEvents = recorded("sessions.events");
const evalRunsListing = recorded("eval.runs.listing");
const evalRun = recorded("eval.run.measured");
const comparison = recorded("eval.compare.regression");
const promotions = recorded("promotions");
const cycle = recorded("improve.cycle.promoted");
const procedures = recorded("memory.procedures");
const facts = recorded("memory.facts");
const fact = recorded("memory.fact");
const episodes = recorded("memory.episodes");
const providers = recorded("providers");
const prices = recorded("prices");

/** The three ids the walk spells into a page URL, taken from the recordings that answer them. */
const SESSION_KEY = "run_0000000002NEHPN093CBQMNNV3";
const CASE_RUN_KEY = "eval_000000000HB774KP7M6WX8HT48:holdout-003-iso-date:1";
const EVAL_RUN_ID = "eval_0000000005AVYXNCY6ZK6K3CP9";
const IMPROVEMENT_RUN_ID = "imp_000000000E6Q9ACKRYS2BJ6ZB7";
const MEM_ID = "mem_000000000120K063R1B72GH8GQ";
const SEARCH = "gateway";

/** The two eval run ids the recorded listing and the recorded comparison both spell. */
const BEFORE = "eval_recorded_before";
const AFTER = "eval_recorded_after";

// ---- One answer per route, so nothing here is a network ----------------------------------------

/** A response object with only what `fetch-panel` reads. */
function answer({ code, text }: Recorded): Response {
  return {
    status: code,
    ok: code === 200,
    headers: { get: () => null },
    text: async () => text,
  } as unknown as Response;
}

/** A subscription that opens and owes nothing: this walk is about links, never about frames. */
function held(): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      pull: () => new Promise<void>(() => {}),
      cancel: () => {},
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

/** The routes of the run in hand, keyed by the proxy URL that asks for them. */
let answers: Record<string, Recorded> = {};

/** The one `fetch` these pages meet: the recorded table, a held subscription, nothing else. */
function gateway(input: RequestInfo | URL): Promise<Response> {
  const url = String(input);
  if (url.endsWith("/stream")) return Promise.resolve(held());
  const next = answers[url];
  if (next === undefined) return Promise.reject(new Error(`no answer stubbed for ${url}`));
  return Promise.resolve(answer(next));
}

/** The proxy URL a gateway route is read through. */
function proxied(route: string): string {
  return route;
}

/** Let React settle its reads. Every state change after this is a render, never a request. */
async function settle(): Promise<void> {
  await act(async () => {});
  await act(async () => {});
  await act(async () => {});
}

/** The shell as `app/layout.tsx` mounts it, around whatever a page renders. */
function shell(children: ReactNode) {
  return <AppShell>{children}</AppShell>;
}

// ---- The walk: every page, its responses, and the links it rendered -----------------------------

/** One page of the walk: where an operator is, what the gateway answered, and how it mounts. */
type Walk = {
  /** The page URL, which is what a failure names. */
  at: string;
  /** The gateway routes this page reads, each to the recording that answers it. */
  reads: Record<string, Recorded>;
  page: () => ReactNode;
  /** What an operator does before the links are collected, where a link needs a choice first. */
  after?: () => void;
};

/** What `/evals` reads with no id in the URL: the eval runs and the promotions. */
const EVALS_READS: Record<string, Recorded> = {
  "/v1/eval/runs?limit=20": evalRunsListing,
  "/v1/promotions?limit=20": promotions,
};

const WALKS: readonly Walk[] = [
  {
    at: "/",
    reads: {
      "/v1/sessions": sessionsListing,
      "/v1/tasks/totals": recorded("tasked.tasks.totals"),
      "/v1/tasks?state=needs_human": recorded("tasked.tasks.needs-human"),
    },
    page: () => Home(),
  },
  {
    at: "/traces",
    reads: { "/v1/sessions?limit=20": sessionsListing, "/v1/runs?limit=20": runsListing },
    page: () => pageAt(TracesPage, {}),
  },
  {
    at: `/traces?key=${SESSION_KEY}`,
    reads: {
      [`/v1/runs/${SESSION_KEY}`]: sessionRun,
      [`/v1/runs/${SESSION_KEY}/trace`]: runTrace,
    },
    page: () => pageAt(TracesPage, { key: SESSION_KEY }),
  },
  {
    at: `/traces?key=${CASE_RUN_KEY}`,
    reads: {
      [`/v1/runs/${CASE_RUN_KEY}`]: caseRun,
      [`/v1/runs/${CASE_RUN_KEY}/trace`]: noCaseTrace,
    },
    page: () => pageAt(TracesPage, { key: CASE_RUN_KEY }),
  },
  {
    at: `/traces?session=${SESSION_KEY}`,
    reads: { [`/v1/sessions/${SESSION_KEY}/events`]: sessionEvents },
    page: () => pageAt(TracesPage, { session: SESSION_KEY }),
  },
  // The two forwarders: not in the nav, and each one link to where it lands.
  { at: "/runs", reads: { [`/v1/runs/${SESSION_KEY}`]: sessionRun }, page: () => pageAt(RunsForward, { key: SESSION_KEY }) },
  { at: "/sessions", reads: { [`/v1/sessions/${SESSION_KEY}/events`]: sessionEvents }, page: () => pageAt(SessionsForward, { session: SESSION_KEY }) },
  {
    at: "/evals",
    reads: EVALS_READS,
    page: () => pageAt(EvalsPage, {}),
    // Picking a pair is what puts the compare link on the page, and the link carries both ids.
    after: () => {
      fireEvent.click(screen.getByRole("radio", { name: `before: ${BEFORE}` }));
      fireEvent.click(screen.getByRole("radio", { name: `after: ${AFTER}` }));
    },
  },
  {
    at: `/evals?id=${EVAL_RUN_ID}`,
    reads: { ...EVALS_READS, [`/v1/eval/runs/${EVAL_RUN_ID}`]: evalRun },
    page: () => pageAt(EvalsPage, { id: EVAL_RUN_ID }),
  },
  {
    at: `/evals?before=${BEFORE}&after=${AFTER}`,
    reads: { ...EVALS_READS, [`/v1/eval/compare?before=${BEFORE}&after=${AFTER}`]: comparison },
    page: () => pageAt(EvalsPage, { before: BEFORE, after: AFTER }),
  },
  {
    at: `/evals?cycle=${IMPROVEMENT_RUN_ID}`,
    reads: { ...EVALS_READS, [`/v1/improve/cycles/${IMPROVEMENT_RUN_ID}`]: cycle },
    page: () => pageAt(EvalsPage, { cycle: IMPROVEMENT_RUN_ID }),
  },
  // The two forwarders of ticket 207: not in the nav, and each one link to where it lands.
  {
    at: "/evals/compare",
    reads: { [`/v1/eval/compare?before=${BEFORE}&after=${AFTER}`]: comparison },
    page: () => pageAt(CompareForward, { before: BEFORE, after: AFTER }),
  },
  {
    at: "/improve",
    reads: { [`/v1/improve/cycles/${IMPROVEMENT_RUN_ID}`]: cycle },
    page: () => pageAt(ImproveForward, { id: IMPROVEMENT_RUN_ID }),
  },
  {
    at: "/memory",
    reads: { "/v1/memory/procedures": procedures, "/v1/events/gate": gate, "/v1/memory/totals": totals },
    page: () => pageAt(MemoryPage, {}),
  },
  {
    at: `/memory?q=${SEARCH}`,
    reads: { [`/v1/memory/facts?q=${SEARCH}`]: facts, "/v1/memory/procedures": procedures, "/v1/events/gate": gate, "/v1/memory/totals": totals },
    page: () => pageAt(MemoryPage, { q: SEARCH }),
  },
  {
    at: `/memory?id=${MEM_ID}`,
    reads: { [`/v1/memory/facts/${MEM_ID}`]: fact, "/v1/memory/procedures": procedures, "/v1/events/gate": gate, "/v1/memory/totals": totals },
    page: () => pageAt(MemoryPage, { id: MEM_ID }),
  },
  {
    at: `/memory?session=${SESSION_KEY}`,
    reads: { [`/v1/memory/episodes/${SESSION_KEY}`]: episodes, "/v1/memory/procedures": procedures, "/v1/events/gate": gate, "/v1/memory/totals": totals },
    page: () => pageAt(MemoryPage, { session: SESSION_KEY }),
  },
  // The three forwarders of ticket 208: not in the nav, and each one link to where it lands.
  { at: "/memory/procedures", reads: {}, page: () => pageAt(ProceduresForward, {}) },
  { at: `/memory/facts?q=${SEARCH}`, reads: {}, page: () => pageAt(FactsForward, { q: SEARCH }) },
  { at: `/memory/episodes?session=${SESSION_KEY}`, reads: { [`/v1/memory/episodes/${SESSION_KEY}`]: episodes }, page: () => pageAt(EpisodesForward, { session: SESSION_KEY }) },
  {
    at: "/models",
    reads: { "/v1/providers": providers, "/v1/prices": prices },
    page: () => ModelsPage(),
  },
];

/** Render one walk's page inside the shell, with its recorded routes answering. */
async function walked(walk: Walk): Promise<{ container: HTMLElement; links: PageLink[] }> {
  answers = { [proxied("/v1/health")]: health };
  for (const [route, body] of Object.entries(walk.reads)) answers[proxied(route)] = body;

  const page = walk.page();
  const { container } = render(shell(page));
  await settle();
  if (walk.after !== undefined) {
    walk.after();
    await settle();
  }
  return { container, links: internalLinks(walk.at, container) };
}

/** The string leaves of the bodies one walk rendered: the only characters its links may spell. */
function leavesFor(walk: Walk): Set<string> {
  const served = Object.values(walk.reads).filter((body) => body.code === 200);
  return leavesOf(served.map((body) => body.text));
}

// ---- The route words, read off the route tree rather than listed here ---------------------------

/** Every literal directory name under `app/`. A dynamic segment (`[id]`) is not one of them. */
function routeWords(directory: string = APP): Set<string> {
  const found = new Set<string>();
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    if (!entry.name.startsWith("[")) found.add(entry.name);
    for (const word of routeWords(resolve(directory, entry.name))) found.add(word);
  }
  return found;
}

const STATIC_SEGMENTS = routeWords();

/**
 * The query parameters that carry an id, and so must be a response's characters verbatim: the
 * two sides of a comparison, and the four names decision 45 moved out of a path segment
 * (`?key=` on `/traces`, `?id=` on `/evals` and `/memory/facts`, `?cycle=` on `/evals`, `?session=` on
 * `/traces` and `/memory/episodes`).
 */
const ID_PARAMS = new Set(["before", "after", "key", "id", "session", "cycle"]);

/** The query parameters that carry an operator's own choice: a row cap and a search. */
const CONTROL_PARAMS = new Set(["limit", "q"]);

/**
 * The nav, which is on every page. Its links change area rather than follow an id, so a claim
 * about what one page links *out* to has to put them aside; they are still walked with the rest.
 */
const NAV_HREFS = new Set(PAGES.map((page) => page.href));

/** The links of a page that are not the nav's: the ones that carry a figure's id somewhere. */
function beyondNav(links: readonly PageLink[]): PageLink[] {
  return links.filter((link) => !NAV_HREFS.has(link.href));
}

const VOCABULARY = {
  staticSegments: STATIC_SEGMENTS,
  idParams: ID_PARAMS,
  controlParams: CONTROL_PARAMS,
};

/** The budget for walking every page in one test; the per-page tests keep the 5 s default. */
const WALK_TIMEOUT_MS = 30_000;

/** The lines this run writes to `evidence/113/links.txt`. */
const log: string[] = [];

beforeEach(() => {
  pushed = [];
  answers = {};
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
  vi.stubGlobal("fetch", vi.fn(gateway));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

afterAll(() => {
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
});

// ---- The walk ----------------------------------------------------------------------------------

describe("no link points at a moved memory page (ticket 208)", () => {
  for (const walk of WALKS.filter((w) => w.at.startsWith("/memory") || w.at === "/")) {
    it(`${walk.at} has no href under /memory/`, async () => {
      const { links } = await walked(walk);
      const hrefs = links.map((link) => link.href);
      expect(hrefs.filter((href) => href.startsWith("/memory/"))).toEqual([]);
    });
  }
});

describe("every page's links carry ids the gateway spelled (FR-60)", () => {
  it("reads a route tree, so the walk below is not calling every segment static", () => {
    expect([...STATIC_SEGMENTS].sort()).toEqual([
      "compare",
      "console",
      "episodes",
      "evals",
      "facts",
      "improve",
      "memory",
      "models",
      "procedures",
      "runs",
      "sessions",
      "traces",
    ]);
  });

  for (const walk of WALKS) {
    it(`${walk.at} builds no id into a link`, async () => {
      const { links } = await walked(walk);
      const leaves = leavesFor(walk);

      expect(builtIds(links, leaves, VOCABULARY)).toEqual([]);
      for (const name of queryParams(links)) {
        expect([...ID_PARAMS, ...CONTROL_PARAMS], `?${name}= is named nowhere`).toContain(name);
      }

      log.push(
        `${walk.at}: ${links.length} links, ${leaves.size} string leaves, 0 built ids`,
        ...links.map((link) => `    ${link.href}`),
      );
    });
  }

  // Nineteen pages rendered in series is about 2 s alone and past vitest's 5 s default when the
  // harness runs every file at once, so this one test carries its own budget (ticket 150 traced
  // the timeout to here). The budget is time, not scope: every page is still walked.
  it(
    "walked every page, and enough links for the walk to mean something",
    async () => {
      const seen: PageLink[] = [];
      for (const walk of WALKS) {
        seen.push(...(await walked(walk)).links);
        cleanup();
      }

      // The nav is on every page, so a walk that rendered nothing would still find links; the id
      // links are the ones this check is about, and there must be many.
      const withIds = beyondNav(seen);
      expect(WALKS).toHaveLength(21);
      expect(withIds.length).toBeGreaterThan(30);
      log.push(`${WALKS.length} pages walked, ${seen.length} links, ${withIds.length} carrying ids`);
    },
    WALK_TIMEOUT_MS,
  );
});

// ---- `[GAP G-4]`: a case run key is linked whole or not at all ----------------------------------

describe("a case run key is never taken apart (`[GAP G-4]`, FR-60)", () => {
  it("links the whole key on /traces and derives nothing from any part of it", async () => {
    const walk = WALKS.find((candidate) => candidate.at === "/traces") as Walk;
    const { links } = await walked(walk);
    const hrefs = links.map((link) => link.href);

    // The test does the splitting the page must not do, and then hunts for the pieces.
    const parts = CASE_RUN_KEY.split(":");
    expect(parts).toHaveLength(3);
    expect(hrefs).toContain(runHref(CASE_RUN_KEY));

    // The only href allowed to spell any part of a key is one of the listing's own whole keys.
    // `/traces` also lists sessions, whose own whole ids may contain a part by chance (a `1`).
    const wholeKeys = [...stringLeaves(runsListing.text), ...stringLeaves(sessionsListing.text)];
    const whole = new Set(wholeKeys.flatMap((key) => [runHref(key), sessionHref(key)]));
    const derived = hrefs.filter(
      (href) => parts.some((part) => decodeURIComponent(href).includes(part)) && !whole.has(href),
    );
    expect(derived).toEqual([]);
    // And nothing on this page offers the eval run at all: that is what `[GAP G-4]` records.
    const out = beyondNav(links).map((link) => link.href);
    expect(out.filter((href) => href.startsWith("/evals"))).toEqual([]);

    log.push(`/runs: case run key linked whole, 0 of its ${parts.length} parts derived`);
  });

  it("gives the case run's own page no link out, because it has nowhere to go", async () => {
    const walk = WALKS.find((candidate) => candidate.at === `/traces?key=${CASE_RUN_KEY}`) as Walk;
    const { links } = await walked(walk);
    expect(beyondNav(links)).toEqual([]);
    expect(screen.getByTestId("run-kind-note")).toBeTruthy();
    log.push(`/runs?key=<case run key>: 0 links beyond the nav`);
  });

  it("does link a session run to its own events, at the key the response sent", async () => {
    const walk = WALKS.find((candidate) => candidate.at === `/traces?key=${SESSION_KEY}`) as Walk;
    const { links } = await walked(walk);

    expect(links.map((link) => link.href)).toContain(sessionHref(SESSION_KEY));
    log.push(`/runs?key=<session key>: events linked at the key the response sent`);
  });
});

// ---- The lookups: what is typed is what opens ---------------------------------------------------

/** An id with the two things a lookup must not tidy: surrounding spaces, and a key's own colons. */
const SPACED = `  ${CASE_RUN_KEY}  `;

/** An id no ledger would mint, so nothing but a verbatim pass-through can carry it. */
const UNUSUAL = "RUN_0000000002nehpn093cbqmnnv3:'x'.(1)~";

/** The four fields an operator types an id into, and where each one opens. */
type Lookup = { at: string; label: string; which: number; prefix: string };

const LOOKUPS: readonly Lookup[] = [
  { at: "/traces", label: "session id", which: 0, prefix: "/traces?session=" },
  { at: "/evals", label: "improvement run id", which: 0, prefix: "/evals?cycle=" },
  { at: "/memory", label: "session id", which: 0, prefix: "/memory?session=" },
  { at: "/evals", label: "eval run id", which: 0, prefix: "/evals?before=" },
  { at: "/evals", label: "eval run id", which: 1, prefix: "/evals?after=" },
];

/** The field and the button of one lookup, found the way an operator finds them. */
function lookupAt(lookup: Lookup): { field: HTMLElement; open: HTMLElement } {
  const field = screen.getAllByLabelText(lookup.label)[lookup.which] as HTMLElement;
  const form = field.closest("form") as HTMLElement;
  return { field, open: within(form).getByRole("button", { name: "Open" }) };
}

describe("an IdLookup opens what was typed, as it was typed (FR-60)", () => {
  for (const lookup of LOOKUPS) {
    for (const typed of [SPACED, UNUSUAL]) {
      it(`${lookup.prefix} carries ${JSON.stringify(typed)} unedited`, async () => {
        const walk = WALKS.find((candidate) => candidate.at === lookup.at) as Walk;
        await walked(walk);
        const { field, open } = lookupAt(lookup);

        fireEvent.change(field, { target: { value: typed } });
        fireEvent.click(open);

        expect(pushed).toHaveLength(1);
        expect(idCarriedBy(pushed[0] as string, lookup.prefix)).toBe(typed);
        log.push(`${lookup.at} -> ${pushed[0] as string}`);
      });
    }

    it(`${lookup.prefix} cannot be opened while its field is blank`, async () => {
      const walk = WALKS.find((candidate) => candidate.at === lookup.at) as Walk;
      await walked(walk);
      const { field, open } = lookupAt(lookup);

      expect((open as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(open);
      expect(pushed).toEqual([]);

      fireEvent.change(field, { target: { value: "x" } });
      expect((open as HTMLButtonElement).disabled).toBe(false);
      fireEvent.change(field, { target: { value: "" } });
      expect((open as HTMLButtonElement).disabled).toBe(true);
    });
  }

  it("asks the gateway nothing: existence is the target page's 404 to report", async () => {
    const walk = WALKS.find((candidate) => candidate.at === "/traces") as Walk;
    await walked(walk);
    const spy = globalThis.fetch as ReturnType<typeof vi.fn>;
    const before = spy.mock.calls.length;
    const { field, open } = lookupAt(LOOKUPS[0] as Lookup);

    fireEvent.change(field, { target: { value: SPACED } });
    fireEvent.click(open);

    expect(spy.mock.calls.length).toBe(before);
    log.push("a lookup makes no request: the 404 belongs to the target page");
  });
});

// ---- The seeded failure: the walk can fire ------------------------------------------------------

/**
 * A page-local link built the way MUST rule 25 forbids: the eval run id is not a field of the
 * row, it is the front of the case run key with the colons cut off. It exists here and nowhere
 * under `app/`, so `npm run lint` and the page walk both stay green while this stays able to fail.
 */
function SplitsAnId({ runKey }: { runKey: string }) {
  const [evalRunId] = runKey.split(":");
  return (
    <p>
      <a href={`/evals/${evalRunId}`}>the eval run this case belongs to</a>
    </p>
  );
}

describe("the check is able to fail (MUST rule 12)", () => {
  it("reports a seeded link whose id was split out of a case run key", () => {
    const leaves = leavesOf([runsListing.text]);
    const { container } = render(<SplitsAnId runKey={CASE_RUN_KEY} />);
    const links = internalLinks("/runs (seeded)", container);

    const found: BuiltId[] = builtIds(links, leaves, VOCABULARY);
    expect(found).toEqual([
      {
        at: "/runs (seeded)",
        href: "/evals/eval_000000000HB774KP7M6WX8HT48",
        part: "eval_000000000HB774KP7M6WX8HT48",
        where: "path segment",
      },
    ]);
    log.push(`seeded split link ${links[0]?.href}: 1 built id reported`);
  });

  it("passes the same row's real link, which is the key whole", () => {
    const leaves = leavesOf([runsListing.text]);
    const links: PageLink[] = [
      { at: "/runs (seeded)", href: `/runs/${CASE_RUN_KEY}`, text: CASE_RUN_KEY },
    ];

    expect(builtIds(links, leaves, VOCABULARY)).toEqual([]);
  });

  it("reports a trimmed id, a re-cased id and a query parameter nobody named", () => {
    const leaves = leavesOf([evalRunsListing.text]);
    const links: PageLink[] = [
      { at: "/evals (seeded)", href: `/evals/${BEFORE.trim().slice(1)}`, text: "trimmed" },
      { at: "/evals (seeded)", href: `/evals/${BEFORE.toUpperCase()}`, text: "re-cased" },
      { at: "/evals (seeded)", href: `/evals?both=${BEFORE}`, text: "unnamed" },
      { at: "/evals (seeded)", href: `/evals?before=${BEFORE}x`, text: "one character" },
    ];

    expect(builtIds(links, leaves, VOCABULARY).map((found) => found.where)).toEqual([
      "path segment",
      "path segment",
      "unnamed query parameter",
      "query ?before=",
    ]);
  });
});

describe("the Overview's Open links carry the served task session id (FR-153, MUST rule 25)", () => {
  it("links /console?session=<the served id> for every needs-human row", async () => {
    const walk = WALKS.find((w) => w.at === "/");
    if (walk === undefined) throw new Error("the Overview is not in the walk");
    const { links } = await walked(walk);
    const served = (
      JSON.parse(recorded("tasked.tasks.needs-human").text) as { tasks: { session_id: string }[] }
    ).tasks;
    expect(served.length).toBeGreaterThan(0);
    const hrefs = links.map((link) => link.href);
    for (const task of served) {
      expect(hrefs).toContain(`/console?session=${encodeURIComponent(task.session_id)}`);
    }
  });
});
