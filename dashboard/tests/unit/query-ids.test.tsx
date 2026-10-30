// @vitest-environment jsdom
/**
 * FR-64 (decision 45): an id rides in the query, and no page has a path segment.
 *
 * Six pages used to be `[segment]` routes. Next 16's export mode needs `generateStaticParams`
 * for a dynamic segment, and the set of ids lives in a database the build cannot read, so each
 * detail now lives on the page that already lists its noun: **the parameter absent renders the
 * listing, present renders the detail**. Three claims, each able to fail on its own:
 *
 *   - the route tree: `app/**` holds **0** `[` segments, and every page the v3 tree served is
 *     still a `page.tsx` (the six details folded into their listings, plus the one listing-less
 *     noun, episodes, which gets the `/memory/facts` no-query prompt as its bare state);
 *   - each of the six pages: bare, it is the listing (or the prompt) and asks the gateway for
 *     the listing route; with its parameter, it is the detail and asks for the id's own route
 *     with the id **verbatim** - a case run key's colons, a pasted id's spaces, nothing split,
 *     trimmed or re-cased (MUST rule 25);
 *   - the href helpers: the one thing FR-60 lets a URL do to an id is URL-encode it, so every
 *     id round-trips through `URLSearchParams` unchanged, including the characters a raw
 *     interpolation would lose (`+`, `&`, `#`, `%`).
 *
 * The per-page suites still prove what each detail renders; this file is only about where the
 * id rides. `tests/unit/links.test.tsx` is the oracle that every id a link carries was spelled
 * by a response field, and it walks the same six pages through their new URLs.
 */
import { readFileSync, readdirSync } from "node:fs";
import { relative, resolve } from "node:path";

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import EvalsPage from "../../app/evals/page";
import { cycleHref, evalRunHref } from "../../app/evals/limit";
import MemoryPage from "../../app/memory/page";
import { NEEDS_QUERY, NEEDS_SESSION, episodeHref, factHref } from "../../app/memory/search";
import TracesPage from "../../app/traces/page";
import { runHref, sessionHref } from "../../app/traces/limit";
import { AppShell } from "../../components/AppShell";
import { pageAt } from "./support/query";

import type { ReactNode } from "react";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const APP = resolve(DASHBOARD, "app");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");

/** The gateway this shell is pointed at. Not the operator's 8756 (decision 30). */

vi.mock("next/navigation", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  useSearchParams: (await import("./support/query")).useSearchParams,
  useRouter: () => ({ push: () => {}, replace: () => {} }),
}));

// ---- The route tree ----------------------------------------------------------------------------

/** Every directory and every `page.tsx` under `app/`, as repo-relative posix paths. */
function tree(directory: string = APP): { directories: string[]; pages: string[] } {
  const directories: string[] = [];
  const pages: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      directories.push(posix(path));
      const below = tree(path);
      directories.push(...below.directories);
      pages.push(...below.pages);
    } else if (entry.name === "page.tsx") {
      pages.push(posix(path));
    }
  }
  return { directories, pages };
}

function posix(path: string): string {
  return relative(APP, path).split("\\").join("/");
}

/**
 * Every page the tree serves after FR-64. The v3 tree held 14 `page.tsx` files; six of them were
 * `[segment]` details that now render from their listing page, and `/memory/episodes` gains the
 * bare page it never had, so the set is these nine and nothing is lost. `/console` (FR-81) is the
 * tenth, and holds no id in its path either; `/models` (FR-89, ticket 147) is the eleventh;
 * `/traces` (ticket 206) is the twelfth, and `runs/` and `sessions/` keep only their forwarders;
 * ticket 207 does the same for `evals/compare/` and `improve/`.
 */
const PAGES = [
  "console/page.tsx",
  "evals/compare/page.tsx",
  "evals/page.tsx",
  "improve/page.tsx",
  "memory/episodes/page.tsx",
  "memory/facts/page.tsx",
  "memory/page.tsx",
  "memory/procedures/page.tsx",
  "models/page.tsx",
  "page.tsx",
  "runs/page.tsx",
  "sessions/page.tsx",
  "traces/page.tsx",
];

describe("no page has a path segment (FR-64)", () => {
  it("holds 0 `[` segments under app/", () => {
    const segments = tree().directories.filter((path) => path.includes("["));
    expect(segments).toEqual([]);
  });

  it("keeps every listing and every detail reachable from a static page", () => {
    expect(tree().pages.sort()).toEqual(PAGES);
  });

  it("reads the URL through `useSearchParams` alone: no second mechanism on any page", () => {
    // An export has no server to read `searchParams` on (decision 44), so every page that reads
    // the URL reads it in the browser through the one hook, and through nothing else (decision 45).
    for (const page of PAGES) {
      const source = readFileSync(resolve(APP, page), "utf8");
      expect(source, `${page} reads params`).not.toContain("params: Promise");
      expect(source, `${page} reads searchParams`).not.toContain("searchParams");
      expect(source, `${page} reads window.location`).not.toContain("window.location");
      expect(source, `${page} reads the query on the server`).not.toContain("async function");
    }
  });
});

// ---- The six pages, bare and with their parameter -------------------------------------------

type Recorded = { code: number; text: string };

/** One recorded response, read from the committed record and never written by a test. */
function recorded(name: string): Recorded {
  const meta = JSON.parse(readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8")) as {
    status: number;
  };
  return { code: meta.status, text: readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8") };
}

const health = recorded("health");

/** A response object with only what `fetch-panel` reads. */
function answer({ code, text }: Recorded): Response {
  return {
    status: code,
    ok: code === 200,
    headers: { get: () => null },
    text: async () => text,
  } as unknown as Response;
}

/** A subscription that opens and owes nothing: this file is about URLs, never about frames. */
function held(): Response {
  return new Response(
    new ReadableStream<Uint8Array>({ pull: () => new Promise<void>(() => {}), cancel: () => {} }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

/** Every gateway URL the page under test asked for, in order. */
let asked: string[] = [];

/** The one `fetch` these pages meet: the recordings for the routes named, a held stream. */
function stubGateway(reads: Record<string, Recorded>): void {
  const answers: Record<string, Recorded> = { ["/v1/health"]: health };
  for (const [route, body] of Object.entries(reads)) answers[route] = body;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      asked.push(url);
      if (url.endsWith("/stream")) return Promise.resolve(held());
      const next = answers[url];
      if (next === undefined) return Promise.reject(new Error(`no answer stubbed for ${url}`));
      return Promise.resolve(answer(next));
    }),
  );
}

async function settle(): Promise<void> {
  await act(async () => {});
  await act(async () => {});
}

function shell(children: ReactNode) {
  return <AppShell>{children}</AppShell>;
}

/** Render a page in the shell, at the query the URL carried. */
async function at(page: () => ReactNode, query: Record<string, string>): Promise<void> {
  render(shell(pageAt(page, query)));
  await settle();
}

/** The gateway routes the page asked for, without `/health` and the stream. */
function routesAsked(): string[] {
  return asked.filter((url) => url !== "/v1/health" && !url.endsWith("/stream"));
}

/**
 * An id no ledger would mint, so nothing but a verbatim pass-through can carry it: a case run
 * key's colons, surrounding spaces, and the characters a query string treats as its own.
 */
const HOSTILE = "  eval_0000000005AVYXNCY6ZK6K3CP9:holdout-003-iso-date:1 +&#%z  ";

const runsListing = recorded("runs.listing");
const sessionRun = recorded("runs.session");
const runTrace = recorded("runs.trace");
const sessionsListing = recorded("sessions.listing");
const sessionEvents = recorded("sessions.events");
const sessionMessages = recorded("sessions.messages");
const evalRunsListing = recorded("eval.runs.listing");
const evalRun = recorded("eval.run.measured");
const promotions = recorded("promotions");
const cycle = recorded("improve.cycle.promoted");
const facts = recorded("memory.facts");
const fact = recorded("memory.fact");
const episodes = recorded("memory.episodes");

/** The ids the recordings answer, so a detail renders the recorded body rather than a 404. */
const SESSION_KEY = "run_0000000002NEHPN093CBQMNNV3";
const EVAL_RUN_ID = "eval_0000000005AVYXNCY6ZK6K3CP9";
const IMPROVEMENT_RUN_ID = "imp_000000000E6Q9ACKRYS2BJ6ZB7";
const MEM_ID = "mem_000000000120K063R1B72GH8GQ";

beforeEach(() => {
  asked = [];
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

type Page = {
  at: string;
  page: () => ReactNode;
  param: string;
  /** What the bare page asks for, and the heading it wears. */
  bare: { reads: Record<string, Recorded>; heading: string; prompt?: string };
  /** What the page with its parameter asks for, keyed on the id, and the heading it wears. */
  detail: {
    reads: (id: string) => Record<string, Recorded>;
    heading: string;
    id: string;
    /** The page keeps its listings under the detail (`/evals`, ticket 207). */
    alongside?: boolean;
  };
};

/** What `/evals` asks for with no id: the work suites, the eval runs and the promotions, and no comparison. */
const EVALS_BARE: Record<string, Recorded> = {
  "/v1/eval/suites": recorded("eval.suites.listing"),
  "/v1/eval/runs?limit=20": evalRunsListing,
  "/v1/promotions?limit=20": promotions,
};

/** What `/memory` asks for with no parameter: the figures row and the active procedures, and the
 * session to watch and the agents its diagram draws (ticket 209). */
const MEMORY_BARE: Record<string, Recorded> = {
  "/v1/agents": recorded("agents"),
  "/v1/sessions": sessionsListing,
  "/v1/events/gate": recorded("events.gate"),
  "/v1/memory/totals": recorded("memory.totals"),
  "/v1/memory/consolidation": recorded("memory.consolidation"),
  "/v1/memory/procedures": recorded("memory.procedures"),
};

/** What `/traces` asks for with no id: the schedules and tasks above the two listings (ticket 254). */
const TRACES_BARE: Record<string, Recorded> = {
  "/v1/schedules": recorded("tasked.schedules.listing"),
  "/v1/tasks?limit=20": recorded("tasked.tasks.listing"),
  "/v1/sessions?limit=20": sessionsListing,
  "/v1/runs?limit=20": runsListing,
};

const SIX: readonly Page[] = [
  {
    at: "/traces",
    page: TracesPage,
    param: "key",
    bare: {
      reads: TRACES_BARE,
      heading: "Traces",
    },
    detail: {
      reads: (id) => ({ [`/v1/runs/${id}`]: sessionRun, [`/v1/runs/${id}/trace`]: runTrace }),
      heading: "Run",
      id: SESSION_KEY,
    },
  },
  {
    at: "/evals",
    page: EvalsPage,
    param: "id",
    bare: { reads: EVALS_BARE, heading: "Evals" },
    detail: {
      reads: (id) => ({ ...EVALS_BARE, [`/v1/eval/runs/${id}`]: evalRun }),
      heading: "Evals",
      id: EVAL_RUN_ID,
      alongside: true,
    },
  },
  {
    at: "/evals (cycle)",
    page: EvalsPage,
    param: "cycle",
    bare: { reads: EVALS_BARE, heading: "Evals" },
    detail: {
      reads: (id) => ({ ...EVALS_BARE, [`/v1/improve/cycles/${id}`]: cycle }),
      heading: "Evals",
      id: IMPROVEMENT_RUN_ID,
      alongside: true,
    },
  },
  {
    at: "/memory",
    page: MemoryPage,
    param: "id",
    bare: { reads: MEMORY_BARE, heading: "Memory", prompt: NEEDS_QUERY },
    detail: {
      reads: (id) => ({ ...MEMORY_BARE, [`/v1/memory/facts/${id}`]: fact }),
      heading: "Memory",
      id: MEM_ID,
      alongside: true,
    },
  },
  {
    at: "/memory (session)",
    page: MemoryPage,
    param: "session",
    bare: { reads: MEMORY_BARE, heading: "Memory", prompt: NEEDS_SESSION },
    detail: {
      reads: (id) => ({ ...MEMORY_BARE, [`/v1/memory/episodes/${id}`]: episodes }),
      heading: "Memory",
      id: SESSION_KEY,
      alongside: true,
    },
  },
  {
    at: "/traces",
    page: TracesPage,
    param: "session",
    bare: {
      reads: TRACES_BARE,
      heading: "Traces",
    },
    detail: {
      reads: (id) => ({
        [`/v1/sessions/${id}/events`]: sessionEvents,
        [`/v1/sessions/${id}/messages`]: sessionMessages,
      }),
      heading: "Session",
      id: SESSION_KEY,
    },
  },
];

describe("the parameter absent renders the listing, present renders the detail (FR-64)", () => {
  for (const page of SIX) {
    it(`${page.at} bare is the listing, and asks for nothing keyed on an id`, async () => {
      stubGateway(page.bare.reads);
      await at(page.page, {});

      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(page.bare.heading);
      expect(routesAsked().sort()).toEqual(Object.keys(page.bare.reads).sort());
      if (page.bare.prompt !== undefined) {
        expect(screen.getByText(page.bare.prompt)).toBeTruthy();
      }
    });

    it(`${page.at}?${page.param}= is the detail, at the id the URL carried`, async () => {
      stubGateway(page.detail.reads(page.detail.id));
      await at(page.page, { [page.param]: page.detail.id });

      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(page.detail.heading);
      expect(routesAsked().sort()).toEqual(Object.keys(page.detail.reads(page.detail.id)).sort());
      // The listing route was not asked for: a detail is not a listing with a row picked out.
      for (const route of Object.keys(page.bare.reads)) {
        if (page.detail.alongside === true) expect(routesAsked()).toContain(route);
        else expect(routesAsked()).not.toContain(route);
      }
    });

    it(`${page.at}?${page.param}= sends a hostile id to the gateway unedited`, async () => {
      stubGateway({});
      await at(page.page, { [page.param]: HOSTILE });

      // Every route asked for spells the id whole - colons, spaces and all - and nothing else
      // of it: no trimmed, split or re-cased variant reached the wire (MUST rules 15, 25).
      const routes = routesAsked();
      expect(routes.length).toBeGreaterThan(0);
      const keyed = routes.filter((route) => !Object.keys(page.bare.reads).includes(route));
      expect(keyed.length).toBeGreaterThan(0);
      if (page.detail.alongside !== true) expect(keyed).toEqual(routes);
      for (const route of keyed) expect(route).toContain(HOSTILE);
      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(page.detail.heading);
    });
  }

  it("/memory?q= keeps the search it had, and ?id= takes precedence over none of it", async () => {
    stubGateway({ ...MEMORY_BARE, "/v1/memory/facts?q=gateway": facts });
    await at(MemoryPage, { q: "gateway" });
    expect(routesAsked()).toContain("/v1/memory/facts?q=gateway");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Memory");
  });
});

// ---- The href helpers: URL-encoded, and nothing else, on the way into a query ---------------

/** Every query href helper, the page it opens and the parameter it spells. */
const HELPERS: readonly { href: (id: string) => string; path: string; param: string }[] = [
  { href: runHref, path: "/traces", param: "key" },
  { href: evalRunHref, path: "/evals", param: "id" },
  { href: cycleHref, path: "/evals", param: "cycle" },
  { href: factHref, path: "/memory", param: "id" },
  { href: episodeHref, path: "/memory", param: "session" },
  { href: sessionHref, path: "/traces", param: "session" },
];

describe("an id rides in the query exactly as it was given (FR-60, FR-64)", () => {
  for (const helper of HELPERS) {
    it(`${helper.path}?${helper.param}= round-trips a hostile id through the URL`, () => {
      const href = helper.href(HOSTILE);
      const url = new URL(href, "http://dashboard.invalid");
      expect(url.pathname).toBe(helper.path);
      expect([...url.searchParams.keys()]).toEqual([helper.param]);
      expect(url.searchParams.get(helper.param)).toBe(HOSTILE);
      expect(href).not.toContain("[");
    });
  }
});
