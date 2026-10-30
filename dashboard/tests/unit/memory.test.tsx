// @vitest-environment jsdom
/**
 * FR-58 (UJ-10): the operator inspects what the harness remembers.
 *
 * Four pages over three stores. `/memory/procedures` is the active set - the procedures that
 * actually reach a system prompt - as a table in the gateway's own order. `/memory/facts` is the
 * one search box in the dashboard: with no `q` it is a prompt and sends nothing, with a `q` it is
 * the gateway's own ordering and nothing else. `/memory/facts?id=` is one fact with its
 * `status`, archived or not. `/memory/episodes?session=` is the pair of sentences decision 24
 * exists for: a 404 and a 200 with zero rows are different facts and are never said the same way.
 *
 * The claim this file exists to make falsifiable is that each store says exactly what it holds and
 * how it was asked for. An empty search box is a prompt and never "0 facts" (`[GAP G-14]`); a
 * search with no hit names the query it found nothing for; and the two episode absences are two
 * sentences. Two oracles say whether a figure is the response's: `tests/support/figures.ts`
 * reports any figure missing half its provenance, and `tests/support/source-at.ts` re-reads the
 * raw bytes at each figure's cited path and reports any character that differs.
 *
 * The recorded memory states are the inputs (decision 33): the procedures set, an empty one, a
 * facts search, an empty one, one fact, a 404 fact, an episode listing and a 404 episodes lookup.
 * Nothing here edits a recorded fixture (MUST rule 24). Four bodies are written in this file, for
 * the four claims no recording can falsify: a second fact, because no recording holds two and "in
 * gateway order" needs two; an archived fact, because every recorded fact is active; a fact whose
 * text carries a seeded `[redacted:...]` value, because the corpus is recorded from a database no
 * secret was ever written to; and an empty episodes listing, because the recorded session wrote
 * one. Each is the shape the gateway serves, and each is marked where it is written.
 *
 * Every page is rendered inside `AppShell`, which is how `app/layout.tsx` mounts it, so the walk
 * sees what the operator sees.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LOOKUP_NOTE, LOOKUP_TITLE } from "../../app/memory/EpisodeLookup";
import { SEARCH_BUTTON, SEARCH_LABEL } from "../../app/memory/FactSearch";
import { ORDER_NOTE, PANEL_TITLE as FACTS_TITLE } from "../../app/memory/FactsPanel";
import {
  NO_LINKS,
  NO_TAGS,
  PANEL_TITLE as FACT_TITLE,
} from "../../app/memory/FactPanel";
import { PANEL_TITLE as EPISODES_TITLE } from "../../app/memory/EpisodesPanel";
import {
  ACTIVE_ONLY,
  PANEL_TITLE as PROCEDURES_TITLE,
} from "../../app/memory/ProceduresPanel";
import { EpisodeLookup } from "../../app/memory/EpisodeLookup";
import { EpisodesPanel } from "../../app/memory/EpisodesPanel";
import { FactPanel } from "../../app/memory/FactPanel";
import { FactSearch } from "../../app/memory/FactSearch";
import { FactsPanel } from "../../app/memory/FactsPanel";
import { ProceduresPanel } from "../../app/memory/ProceduresPanel";
import MemoryPage from "../../app/memory/page";
import FactsForward from "../../app/memory/facts/page";
import EpisodesForward from "../../app/memory/episodes/page";
import ProceduresForward from "../../app/memory/procedures/page";
import { useSearchParams } from "next/navigation";
import {
  ADD_COMMAND,
  NEEDS_QUERY,
  NEEDS_SESSION,
  PROCEDURES_ROUTE,
  PROMOTE_COMMAND,
  RUN_COMMAND,
  episodeHref,
  episodesRoute,
  factHref,
  factRoute,
  factsHref,
  factsRoute,
  noMatch,
} from "../../app/memory/search";
import { AppShell } from "../../components/AppShell";
import { GATEWAY_ORIGIN } from "../../lib/fetch-panel";
import { absentTextFor, emptyTextFor } from "../../lib/null-reasons";
import { pageAt } from "./support/query";

import { figuresWithoutSource } from "../support/figures";
import { divergences, renderedFigures, sourceCharactersByPath } from "../support/source-at";

import type { ReactNode } from "react";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const REPO = resolve(DASHBOARD, "..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(REPO, "evidence/110/memory.txt");
const PROCEDURES_HTML = resolve(REPO, "evidence/110/procedures.html");
const PROMPT_HTML = resolve(REPO, "evidence/110/facts-prompt.html");
const EPISODES_404_HTML = resolve(REPO, "evidence/110/episodes-404.html");

/** The gateway this test's shell is pointed at. Not the operator's 8756 (decision 30). */
const GATEWAY = GATEWAY_ORIGIN;

/** Where an `IdLookup` or the search box sent the operator. */
const pushed: string[] = [];

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

/** The lines this run writes to `evidence/110/memory.txt`. */
const log: string[] = [];

type Recorded = { name: string; route: string; code: number; text: string };

/** One recorded response, read from the committed record and never written by a test. */
function recorded(name: string): Recorded {
  const meta = JSON.parse(readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8")) as {
    route: string;
    status: number;
  };
  return {
    name,
    route: meta.route,
    code: meta.status,
    text: readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8"),
  };
}

const procedures = recorded("memory.procedures");
const noProcedures = recorded("empty.memory.procedures");
const facts = recorded("memory.facts");
const noFacts = recorded("empty.memory.facts");
const fact = recorded("memory.fact");
const unknownFact = recorded("notfound.memory.fact");
const episodes = recorded("memory.episodes");
const unknownSession = recorded("notfound.memory.episodes");
const health = recorded("health");
const gate = recorded("events.gate");
const totals = recorded("memory.totals");

/** The ids and the query the recordings hold, taken from the routes they answered. */
const QUERY = "gateway";
const MEM_ID = "mem_000000000120K063R1B72GH8GQ";
const UNKNOWN_MEM_ID = "mem_00000000000000000000000000";
const SESSION_ID = "run_0000000002NEHPN093CBQMNNV3";
const UNKNOWN_SESSION_ID = "run_00000000000000000000000000";

/**
 * A second fact, written here because no recording holds two and the claim under test is that the
 * results keep the gateway's own order - best match first, as the search ranked them. It is not a
 * recorded fixture and nothing here edits one (MUST rule 24).
 */
const TWO_FACTS = [
  '{"facts":[',
  '{"created_at":"2026-01-02T12:00:00Z","links":[],"mem_id":"mem_best","source":"operator",',
  '"status":"active","tags":[],"text":"The gateway answers GET and nothing else."},',
  '{"created_at":"2026-01-01T12:00:00Z","links":[],"mem_id":"mem_next","source":"run",',
  '"status":"active","tags":[],"text":"The gateway is read-only."}',
  "]}",
].join("");

/**
 * One archived fact, written here because every recorded fact is active and the criterion is that
 * a fact is readable by id **archived or not**, with its `status` as sent.
 */
const ARCHIVED_FACT = [
  '{"created_at":"2026-01-01T12:00:00Z","links":[],"mem_id":"mem_archived","source":"operator",',
  '"status":"archived","tags":["stale"],"text":"The gateway once answered POST."}',
].join("");

/** The archived fact's id. */
const ARCHIVED_ID = "mem_archived";

/**
 * A fact whose text holds a seeded redaction value. `memory/redact.py` replaced a secret with
 * `[redacted:<kind>]` on write, so this is what the store holds and what the gateway serves; the
 * dashboard adds no redaction pass and must not weaken one (PRD, Security), so the value is a
 * figure like any other and renders character for character. No recording carries one, because
 * the corpus is recorded from a database no secret was ever written to.
 */
const SEEDED_REDACTION = "[redacted:anthropic_key]";
const REDACTED_ID = "mem_secret";
const REDACTED_FACT = [
  '{"created_at":"2026-01-01T12:00:00Z","links":[],"mem_id":"mem_secret","source":"operator",',
  `"status":"active","tags":[],"text":"The provider credential is ${SEEDED_REDACTION} on write."}`,
].join("");

/** An episodes listing the session wrote nothing into: a 200 that is not a 404 (`D-3`). */
const NO_EPISODES = '{"episodes":[]}';

// ---- One answer per route, so nothing here is a network ----------------------------------------

type Answer = { status: number; text: string; headers?: Record<string, string> };

/** A response object with only what `fetch-panel` reads. */
function answer({ status, text, headers = {} }: Answer): Response {
  return {
    status,
    ok: status === 200,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    text: async () => text,
  } as unknown as Response;
}


type Route = Answer | "rejects" | "rejects silently" | "rejects with no error";

/** Stub `fetch` with one answer per gateway route, and hand back the spy so a test can read the URLs. */
function answersWith(routes: Record<string, Route>): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async (url: string) => {
    const next = routes[url];
    if (next === undefined) throw new Error(`no answer stubbed for ${url}`);
    if (next === "rejects") throw new Error("ECONNREFUSED");
    // A browser that refuses a fetch without a message: the class name is all there is to report.
    if (next === "rejects silently") throw new TypeError("");
    // And a refusal that is not an `Error` at all: reported as itself, never as an invented code.
    if (next === "rejects with no error") throw "the browser refused the request";
    return answer(next);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** The recorded `/health` the shell's pill polls, so no test here is about the pill. */
const HEALTH_URL = "/v1/health";
const HEALTH_OK: Answer = { status: health.code, text: health.text };

/** The proxy URL a gateway route is read through. */
function proxied(route: string): string {
  return route;
}

/** Let React settle a read. Every state change after this is a render, never a request. */
async function settle(): Promise<void> {
  await act(async () => {});
}

/** The shell as `app/layout.tsx` mounts it, around whatever a page renders. */
function shell(children: ReactNode) {
  return <AppShell>{children}</AppShell>;
}

/*
 * The three old pages' panels, each as the old page composed them: a merged page is one h1 and one
 * listing of each kind, so the per-panel assertions below keep their old page-unique queries and
 * the merged page has its own scope at the end of this file (ticket 208).
 */
function ProceduresPage() {
  return (
    <div>
      <ProceduresPanel />
      <EpisodeLookup />
    </div>
  );
}

function FactsPage() {
  const query = useSearchParams();
  const q = query.get("q") ?? undefined;
  const id = query.get("id") ?? undefined;
  if (id !== undefined) {
    return (
      <div>
        <FactPanel memId={id} />
        <EpisodeLookup />
      </div>
    );
  }
  return (
    <div>
      <FactSearch q={q} />
      {q === undefined ? <p data-testid="facts-prompt">{NEEDS_QUERY}</p> : <FactsPanel q={q} />}
      <EpisodeLookup />
    </div>
  );
}

function EpisodesPage() {
  const session = useSearchParams().get("session") ?? undefined;
  return (
    <div>
      {session === undefined ? (
        <p data-testid="episodes-prompt">{NEEDS_SESSION}</p>
      ) : (
        <EpisodesPanel sessionId={session} />
      )}
      <EpisodeLookup />
    </div>
  );
}

/** `/memory/procedures`, which reads one route and carries nothing in its URL. */
async function renderProcedures() {
  const page = pageAt(ProceduresPage);
  const rendered = render(shell(page));
  await settle();
  return rendered;
}

/** `/memory/facts` at whatever the URL carries, rendered as the server renders it. */
async function renderFacts(searchParams: { q?: string } = {}) {
  const page = pageAt(FactsPage, searchParams);
  const rendered = render(shell(page));
  await settle();
  return rendered;
}

/** `/memory/facts?id=`, at the id the URL carried (FR-64). */
async function renderFact(memId: string) {
  const page = pageAt(FactsPage, { id: memId });
  const rendered = render(shell(page));
  await settle();
  return rendered;
}

/** `/memory/episodes?session=`, at the session id the URL carried (FR-64). */
async function renderEpisodes(sessionId: string) {
  const page = pageAt(EpisodesPage, { session: sessionId });
  const rendered = render(shell(page));
  await settle();
  return rendered;
}

/** One panel, so the shell's own regions never answer for it. */
function panel(title: string): HTMLElement {
  return screen.getByRole("region", { name: title });
}

/**
 * The figure at a route and path, found by its provenance pair rather than by its `title`:
 * `SourceChip` carries the same `route -> path` title, and only the figure carries `data-path`.
 */
function figureAt(scope: HTMLElement, route: string, path: string): HTMLElement {
  const found = scope.querySelector<HTMLElement>(`[data-route="${route}"][data-path="${path}"]`);
  if (found === null) throw new Error(`no figure for ${route} -> ${path}`);
  return found;
}

/**
 * Every control on a page that is not one of the read-only four (`D-8`): refetch, open a source,
 * copy, navigate. The shell's own status pill opens its poll's response and is one of them.
 */
function writingControls(container: ParentNode, allowed: Set<string>): string[] {
  return [...container.querySelectorAll("button")]
    .filter((button) => button.getAttribute("data-testid") !== "gateway-status-pill")
    // A figure is its own source-opening trigger now (`Value` is a `button`): it is read-only by
    // construction, so it is excluded by the hook that marks it rather than by its own text.
    .filter((button) => !button.hasAttribute("data-figure"))
    // The theme toggle keeps a choice in this browser (ticket 251); it writes nothing to the gateway.
    .filter((button) => !/^Switch to (light|dark) theme$/.test(button.getAttribute("aria-label") ?? ""))
    .map((button) => button.getAttribute("aria-label") ?? button.textContent ?? "")
    .filter((name) => !allowed.has(name));
}

/** Every figure of a tree, checked against the bytes of the bodies it claims to come from. */
function faithful(container: ParentNode, bodies: Record<string, string>): void {
  const figures = renderedFigures(container);
  expect(figures.length).toBeGreaterThan(0);
  expect(figuresWithoutSource(container)).toEqual([]);
  expect(divergences(figures, new Map(Object.entries(bodies)))).toEqual([]);
}

/** Write one page's rendered DOM where the ticket's evidence manifest says it goes. */
function capture(path: string, container: ParentNode): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, (container as HTMLElement).innerHTML, "utf8");
}

/** The active procedures, as the gateway sent them. */
type SentProcedure = { proc_id: string; text: string; source: string; created_at: string };

function sentProcedures(text: string): SentProcedure[] {
  return (JSON.parse(text) as { procedures: SentProcedure[] }).procedures;
}

/** One fact, as the gateway sent it. */
type SentFact = {
  mem_id: string;
  text: string;
  source: string;
  status: string;
  created_at: string;
  tags: string[];
  links: string[];
};

function sentFacts(text: string): SentFact[] {
  return (JSON.parse(text) as { facts: SentFact[] }).facts;
}

/** One episode, as the gateway sent it. */
type SentEpisode = {
  episode_id: string;
  run_id: string;
  kind: string;
  text: string;
  created_at: string;
};

function sentEpisodes(text: string): SentEpisode[] {
  return (JSON.parse(text) as { episodes: SentEpisode[] }).episodes;
}

beforeEach(() => {
  pushed.length = 0;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

afterAll(() => {
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
});

// ---- The active set -------------------------------------------------------------------------

describe("/memory/procedures shows the active set as sent (FR-58, UJ-10)", () => {
  it("shows proc_id, text, source and created_at in the gateway's order", async () => {
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
    });
    const { container } = await renderProcedures();

    // The route this page builds is the route the gateway recorded itself answering.
    expect(PROCEDURES_ROUTE).toBe(procedures.route);
    const asked = spy.mock.calls.map((call) => call[0] as string);
    expect(asked.filter((url) => url !== HEALTH_URL)).toEqual([proxied(PROCEDURES_ROUTE)]);

    const rows = sentProcedures(procedures.text);
    expect(rows).toHaveLength(1);
    const chars = sourceCharactersByPath(procedures.text);
    const scope = panel(PROCEDURES_TITLE);

    for (const field of ["proc_id", "text", "source", "created_at"]) {
      const path = `.procedures[0].${field}`;
      expect(figureAt(scope, PROCEDURES_ROUTE, path).textContent).toBe(chars.get(path));
    }
    expect(figureAt(scope, PROCEDURES_ROUTE, ".procedures[0].source").textContent).toBe(
      "promotion of cand_4820460a275e0bc8",
    );
    // The set is the active one and the page says so rather than implying a history (`[GAP G-16]`).
    expect(within(scope).getByTestId("procedures-note").textContent).toBe(ACTIVE_ONLY);

    faithful(container, { [PROCEDURES_ROUTE]: procedures.text });
    capture(PROCEDURES_HTML, container);
    log.push("/memory/procedures: proc_id, text, source and created_at, in the gateway's order");
  });

  it("says the store holds no active procedure yet, and what would put one there", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(PROCEDURES_ROUTE)]: { status: noProcedures.code, text: noProcedures.text },
    });
    const { container } = await renderProcedures();

    expect(sentProcedures(noProcedures.text)).toEqual([]);
    const shown = panel(PROCEDURES_TITLE).textContent ?? "";
    expect(shown).toContain(`No ${PROCEDURES_TITLE.toLowerCase()} recorded yet.`);
    expect(shown).not.toContain(PROCEDURES_ROUTE);
    expect(shown).toContain(PROMOTE_COMMAND);
    // Zero rows is not an error and not an absence: nothing went wrong and no figure was rendered.
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
    log.push("/memory/procedures: an empty active set is `EmptyState` naming the promote command");
  });
});

// ---- The search box that asks for nothing until it is asked ------------------------------------

describe("/memory/facts is a prompt before it is a result (FR-58, `[GAP G-14]`)", () => {
  it("shows the prompt state, sends no request and never says 0 facts", async () => {
    const spy = answersWith({ [HEALTH_URL]: HEALTH_OK });
    const { container } = await renderFacts();

    const asked = spy.mock.calls.map((call) => call[0] as string);
    // The only request on the page is the shell pill's own poll: no facts route is read at all.
    expect(asked.filter((url) => url !== HEALTH_URL)).toEqual([]);
    expect(asked.some((url) => url.includes("/memory/facts"))).toBe(false);

    const shown = container.textContent ?? "";
    expect(screen.getByTestId("facts-prompt").textContent).toBe(NEEDS_QUERY);
    expect(NEEDS_QUERY).toBe("type a search; the gateway lists nothing without one");
    expect(shown).not.toContain("0 facts");
    expect(shown).not.toContain("0 rows");
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    expect(screen.queryByRole("region", { name: FACTS_TITLE })).toBeNull();

    capture(PROMPT_HTML, container);
    log.push("/memory/facts: no `q` is the prompt state, 0 requests and never `0 facts`");
  });

  it("puts the submitted query in the URL, so a search survives a reload", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    await renderFacts();

    const field = screen.getByLabelText(SEARCH_LABEL) as HTMLInputElement;
    const search = screen.getByRole("button", { name: SEARCH_BUTTON }) as HTMLButtonElement;
    const form = field.closest("form") as HTMLFormElement;

    // A blank box can search for nothing, so the one submission that would go nowhere cannot.
    expect(search.disabled).toBe(true);
    fireEvent.submit(form);
    expect(pushed).toEqual([]);

    fireEvent.change(field, { target: { value: QUERY } });
    expect(search.disabled).toBe(false);
    fireEvent.submit(form);
    expect(pushed).toEqual([factsHref(QUERY)]);
    expect(factsHref(QUERY)).toBe("/memory?q=gateway");
    log.push("/memory/facts: a submitted query is a navigation to `?q=`, and lives nowhere else");
  });

  it("keeps the query the URL carried in the box, and reads it back to the gateway", async () => {
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(QUERY))]: { status: facts.code, text: facts.text },
    });
    await renderFacts({ q: QUERY });

    expect(factsRoute(QUERY)).toBe(facts.route);
    expect(spy.mock.calls.map((call) => call[0])).toContain(proxied(factsRoute(QUERY)));
    expect((screen.getByLabelText(SEARCH_LABEL) as HTMLInputElement).value).toBe(QUERY);
    log.push("/memory/facts: `?q=` is read back into the box and sent to the recorded route");
  });
});

// ---- What a search found ------------------------------------------------------------------------

describe("/memory/facts renders what matched, in the gateway's order (FR-58)", () => {
  it("shows the fact as sent and links the row by its own mem_id", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(QUERY))]: { status: facts.code, text: facts.text },
    });
    const { container } = await renderFacts({ q: QUERY });

    const route = factsRoute(QUERY);
    const rows = sentFacts(facts.text);
    expect(rows).toHaveLength(1);
    const chars = sourceCharactersByPath(facts.text);
    const scope = panel(FACTS_TITLE);

    for (const field of ["mem_id", "text", "source", "status", "created_at"]) {
      const path = `.facts[0].${field}`;
      expect(figureAt(scope, route, path).textContent).toBe(chars.get(path));
    }
    // The row's link is the id the response sent, verbatim: nothing here builds one (MUST rule 25).
    const link = within(scope).getByRole("link", { name: MEM_ID }) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(factHref(MEM_ID));
    expect(factHref(MEM_ID)).toBe(`/memory?id=${MEM_ID}`);
    expect(within(scope).getByTestId("facts-note").textContent).toBe(ORDER_NOTE);
    expect(ORDER_NOTE).toContain("best match first");

    faithful(container, { [route]: facts.text });
    log.push("/memory/facts: the matched fact as sent, its row linked by its own `mem_id`");
  });

  it("keeps two results in the order the gateway ranked them", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(QUERY))]: { status: facts.code, text: TWO_FACTS },
    });
    const { container } = await renderFacts({ q: QUERY });

    const route = factsRoute(QUERY);
    const sent = sentFacts(TWO_FACTS);
    expect(sent).toHaveLength(2);
    const scope = panel(FACTS_TITLE);
    const rows = within(within(scope).getByRole("table")).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);

    for (const [index, row] of rows.entries()) {
      const shown = figureAt(row as HTMLElement, route, `.facts[${index}].mem_id`);
      expect(shown.textContent).toBe(sent[index]?.mem_id);
    }
    // The best match is first because the gateway ranked it first, and nothing here reorders.
    expect(sent[0]?.mem_id).toBe("mem_best");
    faithful(container, { [route]: TWO_FACTS });
    log.push("/memory/facts: two results render in the gateway's rank order, best match first");
  });

  it("names the query it found nothing for, rather than reporting an empty store", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(QUERY))]: { status: noFacts.code, text: noFacts.text },
    });
    const { container } = await renderFacts({ q: QUERY });

    expect(sentFacts(noFacts.text)).toEqual([]);
    const shown = panel(FACTS_TITLE).textContent ?? "";
    expect(shown).toContain(noMatch(QUERY));
    expect(noMatch(QUERY)).toBe(`no active fact matches ${QUERY}`);
    expect(shown).toContain(ADD_COMMAND);
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    log.push("/memory/facts: a query with 0 hits reads `no active fact matches <q>`");
  });

  it("passes a query the gateway may refuse straight through, unedited", async () => {
    const odd = " gateway OR ";
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(odd))]: { status: noFacts.code, text: noFacts.text },
    });
    await renderFacts({ q: odd });

    // Whether an FTS expression is servable is the gateway's answer to give, so its characters go.
    expect(spy.mock.calls.map((call) => call[0])).toContain(proxied(factsRoute(odd)));
    expect(panel(FACTS_TITLE).textContent).toContain(noMatch(odd));
    log.push("/memory/facts: the query reaches the gateway as typed - no trim, no re-casing");
  });
});

// ---- One fact, archived or not --------------------------------------------------------------------

describe("/memory/facts?id= shows one fact with its status (FR-58)", () => {
  it("reads the recorded fact at the id the URL carried and shows its status as sent", async () => {
    const route = factRoute(MEM_ID);
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: fact.code, text: fact.text },
    });
    const { container } = await renderFact(MEM_ID);

    expect(route).toBe(fact.route);
    expect(spy.mock.calls.map((call) => call[0])).toContain(proxied(route));

    const body = JSON.parse(fact.text) as SentFact;
    const chars = sourceCharactersByPath(fact.text);
    const scope = panel(FACT_TITLE);
    for (const field of ["mem_id", "text", "source", "status", "created_at"]) {
      expect(figureAt(scope, route, `.${field}`).textContent).toBe(chars.get(`.${field}`));
    }
    expect(body.status).toBe("active");
    expect(figureAt(scope, route, ".status").textContent).toBe("active");
    // The fact carries neither a tag nor a link, which the panel says rather than drawing blanks.
    expect(body.tags).toEqual([]);
    expect(within(scope).getByTestId("fact-tags-empty").textContent).toBe(NO_TAGS);
    expect(within(scope).getByTestId("fact-links-empty").textContent).toBe(NO_LINKS);

    faithful(container, { [route]: fact.text });
    log.push("/memory/facts/<id>: the recorded fact, its `status` and its two empty collections");
  });

  it("shows an archived fact by id, with the status the gateway sent", async () => {
    const route = factRoute(ARCHIVED_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: fact.code, text: ARCHIVED_FACT },
    });
    const { container } = await renderFact(ARCHIVED_ID);

    const scope = panel(FACT_TITLE);
    // An archived fact is out of retrieval, not out of the ledger: it reads by id like any other,
    // and its `status` is the word that says which it is.
    expect(figureAt(scope, route, ".status").textContent).toBe("archived");
    expect(figureAt(scope, route, ".tags[0]").textContent).toBe("stale");
    expect(within(scope).queryByTestId("fact-tags-empty")).toBeNull();
    faithful(container, { [route]: ARCHIVED_FACT });
    log.push("/memory/facts/<id>: an archived fact reads by id, wearing `status: archived`");
  });

  it("renders a seeded redaction value character for character", async () => {
    const route = factRoute(REDACTED_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: fact.code, text: REDACTED_FACT },
    });
    const { container } = await renderFact(REDACTED_ID);

    const scope = panel(FACT_TITLE);
    const text = figureAt(scope, route, ".text");
    // The dashboard adds no redaction pass and weakens none: what the store holds is a figure like
    // any other, so it is neither re-masked, escaped, nor prettified (PRD, Security).
    expect(text.textContent).toContain(SEEDED_REDACTION);
    expect(text.textContent).toBe(sourceCharactersByPath(REDACTED_FACT).get(".text"));
    faithful(container, { [route]: REDACTED_FACT });
    log.push(`/memory/facts/<id>: a seeded ${SEEDED_REDACTION} renders verbatim`);
  });

  it("quotes the gateway's own 404 where no ledger holds the fact id", async () => {
    const route = factRoute(UNKNOWN_MEM_ID);
    expect(route).toBe(unknownFact.route);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: unknownFact.code, text: unknownFact.text },
    });
    const { container } = await renderFact(UNKNOWN_MEM_ID);

    const body = JSON.parse(unknownFact.text) as { error: string; message: string };
    expect(body.error).toBe("SurfaceNotFound");
    const shown = panel(FACT_TITLE).textContent ?? "";
    expect(shown).toContain(body.error);
    expect(shown).toContain(body.message);
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    log.push("/memory/facts/<id>: an id no ledger holds is `AbsentState` quoting the gateway");
  });
});

// ---- The two absences that are not the same absence -------------------------------------------------

describe("/memory/episodes?session= tells a 404 from an empty 200 (FR-58, `D-3`)", () => {
  it("shows the episodes the session wrote, as sent", async () => {
    const route = episodesRoute(SESSION_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: episodes.code, text: episodes.text },
    });
    const { container } = await renderEpisodes(SESSION_ID);

    expect(route).toBe(episodes.route);
    const rows = sentEpisodes(episodes.text);
    expect(rows).toHaveLength(1);
    const chars = sourceCharactersByPath(episodes.text);
    const scope = panel(EPISODES_TITLE);
    for (const field of ["episode_id", "run_id", "kind", "text", "created_at"]) {
      const path = `.episodes[0].${field}`;
      expect(figureAt(scope, route, path).textContent).toBe(chars.get(path));
    }
    // The episode's text is the turn as it was written, newline and all, and never reflowed.
    expect(figureAt(scope, route, ".episodes[0].text").textContent).toBe(
      "prompt: ping\nreply: pong",
    );
    faithful(container, { [route]: episodes.text });
    log.push("/memory/episodes/<id>: the recorded episode, every field as the gateway sent it");
  });

  it("says the events table never held the session id where the gateway answered 404", async () => {
    const route = episodesRoute(UNKNOWN_SESSION_ID);
    expect(route).toBe(unknownSession.route);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: unknownSession.code, text: unknownSession.text },
    });
    const { container } = await renderEpisodes(UNKNOWN_SESSION_ID);

    const body = JSON.parse(unknownSession.text) as { error: string; message: string };
    const shown = panel(EPISODES_TITLE).textContent ?? "";
    expect(shown).toContain(body.message);
    expect(shown).toContain("the events table never held this session id");
    expect(absentTextFor(route)).toBe("the events table never held this session id");
    // And it is not the other sentence: a 404 and an empty 200 are two different facts.
    expect(shown).not.toContain("the session exists and wrote no episode");
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);

    capture(EPISODES_404_HTML, container);
    log.push("/memory/episodes/<id>: a 404 reads `the events table never held this session id`");
  });

  it("says the session exists and wrote no episode where the gateway answered 200 empty", async () => {
    const route = episodesRoute(SESSION_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: episodes.code, text: NO_EPISODES },
    });
    const { container } = await renderEpisodes(SESSION_ID);

    expect(sentEpisodes(NO_EPISODES)).toEqual([]);
    const shown = panel(EPISODES_TITLE).textContent ?? "";
    expect(shown).toContain("the session exists and wrote no episode");
    expect(emptyTextFor(route)).toBe("the session exists and wrote no episode");
    expect(shown).toContain(RUN_COMMAND);
    // Nothing went wrong, so this is not an alert and not the 404's sentence.
    expect(shown).not.toContain("the events table never held this session id");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    log.push(
      "/memory/episodes/<id>: a 200 with 0 rows reads `the session exists and wrote no episode`",
    );
  });
});

// ---- The one field an operator types an id into -------------------------------------------------------

describe("the memory pages reach the episodes page by a session id (FR-58, FR-60)", () => {
  it("offers a lookup labelled for the id it wants, on every memory page", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
      [proxied(factsRoute(QUERY))]: { status: facts.code, text: facts.text },
      [proxied(factRoute(MEM_ID))]: { status: fact.code, text: fact.text },
      [proxied(episodesRoute(SESSION_ID))]: { status: episodes.code, text: episodes.text },
    });

    for (const open of [
      () => renderProcedures(),
      () => renderFacts({ q: QUERY }),
      () => renderFact(MEM_ID),
      () => renderEpisodes(SESSION_ID),
    ]) {
      await open();
      const group = screen.getByRole("region", { name: LOOKUP_TITLE });
      expect(within(group).getByLabelText("session id")).toBeTruthy();
      expect(within(group).getByTestId("lookup-note").textContent).toBe(LOOKUP_NOTE);
      cleanup();
    }
    log.push("memory: every page offers an `IdLookup` labelled `session id`");
  });

  it("opens the episodes page at the id exactly as it was typed", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
    });
    await renderProcedures();

    const group = screen.getByRole("region", { name: LOOKUP_TITLE });
    const field = within(group).getByLabelText("session id") as HTMLInputElement;
    const form = group.querySelector("form") as HTMLFormElement;

    fireEvent.submit(form);
    expect(pushed).toEqual([]);

    fireEvent.change(field, { target: { value: ` ${SESSION_ID} ` } });
    fireEvent.submit(form);
    expect(pushed).toEqual([episodeHref(` ${SESSION_ID} `)]);
    const opened = new URL(pushed[0] ?? "", "http://dashboard.invalid");
    expect(opened.pathname).toBe("/memory");
    expect(opened.searchParams.get("session")).toBe(` ${SESSION_ID} `);
    log.push("memory: the lookup opens `/memory/episodes?session=<id as typed>` - no trim, no split");
  });

  it("writes nothing from any memory page, and every figure says where it came from", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(QUERY))]: { status: facts.code, text: facts.text },
      [proxied(episodesRoute(SESSION_ID))]: { status: episodes.code, text: episodes.text },
    });

    const searched = await renderFacts({ q: QUERY });
    expect(
      writingControls(searched.container, new Set(["Refresh", "Open", "Menu", SEARCH_BUTTON])),
    ).toEqual([]);
    expect(searched.container.querySelectorAll("form[method]")).toHaveLength(0);
    expect(figuresWithoutSource(searched.container)).toEqual([]);
    cleanup();

    const listed = await renderEpisodes(SESSION_ID);
    expect(writingControls(listed.container, new Set(["Refresh", "Open", "Menu"]))).toEqual([]);
    expect(figuresWithoutSource(listed.container)).toEqual([]);
    log.push("memory: 0 controls write, and 0 figures lack a source");
  });

  it("is reachable from the narrow menu as well as from the sidebar", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
    });
    await renderProcedures();

    expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    const nav = screen.getByRole("navigation");
    expect(nav.getAttribute("data-open")).toBe("true");
    expect(within(nav).getByRole("link", { name: "Memory" })).toBeTruthy();
    log.push("memory: Memory is reachable from the sidebar and the narrow menu");
  });
});

// ---- The states design.md lets no panel skip ----------------------------------------------------------

describe("the memory panels wear every state, and remember nothing (design.md)", () => {
  it("is busy before an answer and holds the answer after it", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
    });
    const page = await ProceduresPage();
    render(shell(page));

    expect(panel(PROCEDURES_TITLE).getAttribute("aria-busy")).toBe("true");
    expect(screen.getAllByTestId("skeleton-row").length).toBeGreaterThan(0);
    expect(screen.getByTestId("gateway-status-pill").textContent).toContain("checking");

    await settle();
    expect(panel(PROCEDURES_TITLE).getAttribute("aria-busy")).toBe("false");
    expect(screen.queryAllByTestId("skeleton-row")).toEqual([]);

    const footer = panel(PROCEDURES_TITLE).querySelector("footer");
    expect(footer?.textContent).toContain("Updated 0 min ago");
    expect(footer?.textContent).not.toContain(PROCEDURES_ROUTE);
    expect(footer?.textContent).not.toContain("200");
    expect(footer?.textContent).not.toContain("fetched");
    log.push("panel: `loading` is the busy skeleton, and the active set replaces it");
  });

  it("goes stale five minutes after it fetched, and a refetch clears the badge", async () => {
    vi.useFakeTimers();
    const route = episodesRoute(SESSION_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: episodes.code, text: episodes.text },
    });
    const page = pageAt(EpisodesPage, { session: SESSION_ID });
    render(shell(page));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 5 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60 * 1000);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 6 min ago");
    // The announcement is frozen at the crossing, so a screen reader hears it once.
    expect(screen.getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

    await act(async () => {
      fireEvent.click(screen.getAllByRole("button", { name: "Refresh" })[0] as HTMLElement);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("");
    log.push("panel: `stale` at five minutes, announced once, cleared by `Refresh`");
  });

  it("renders no figure from memory when the gateway cannot be reached", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(PROCEDURES_ROUTE)]: "rejects",
    });
    const { container } = await renderProcedures();

    expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    // A failed data read never rewrites the pill, which reads its own poll (decision 34).
    expect(screen.getByTestId("gateway-status-pill").getAttribute("data-state")).toBe("reachable");
    cleanup();

    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(PROCEDURES_ROUTE)]: "rejects" });
    await renderProcedures();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("ECONNREFUSED");
    cleanup();

    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(PROCEDURES_ROUTE)]: "rejects silently" });
    await renderProcedures();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("TypeError");
    cleanup();

    // A refusal that is not an `Error` has no name or message to read, so it is quoted as itself.
    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(PROCEDURES_ROUTE)]: "rejects with no error" });
    await renderProcedures();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe(
      "the browser refused the request",
    );
    cleanup();

    log.push("panel: an unreachable gateway leaves 0 figures, and names only the cause it gave");
  });

  it("quotes a body that is not the error contract as itself, and offers the read again", async () => {
    const route = factsRoute(QUERY);
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: 500, text: "the gateway fell over" },
    });
    await renderFacts({ q: QUERY });

    const shown = panel(FACTS_TITLE).textContent ?? "";
    expect(shown).toContain("UnreadableBody");
    expect(shown).toContain("the gateway fell over");
    expect(shown).toContain("500");

    const before = spy.mock.calls.length;
    await act(async () => {
      fireEvent.click(
        within(panel(FACTS_TITLE)).getAllByRole("button", { name: "Refetch" })[0] as HTMLElement,
      );
    });
    expect(spy.mock.calls.length).toBeGreaterThan(before);
    log.push("panel: a body outside decision 24's contract is quoted as itself, and read again");
  });

  it("lets the pill speak for its own poll while a store renders", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
    });
    await renderProcedures();

    const pill = screen.getByTestId("gateway-status-pill");
    expect(pill.getAttribute("data-state")).toBe("reachable");
    expect(screen.getByTestId("pill-announcement").textContent).toBe("gateway reachable");

    fireEvent.click(pill);
    const dialog = screen.getByRole("dialog");
    // The pill cites no path, so its drawer highlights nothing rather than guessing at one.
    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    expect(within(dialog).queryByTestId("cited")).toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByTestId("gateway-status-pill"));
    cleanup();

    answersWith({
      [HEALTH_URL]: "rejects",
      [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
    });
    await renderProcedures();
    const down = screen.getByTestId("gateway-status-pill");
    expect(down.getAttribute("data-state")).toBe("unreachable");
    expect((down as HTMLButtonElement).disabled).toBe(true);
    cleanup();

    answersWith({
      [HEALTH_URL]: { status: 500, text: '{"error":"InternalError","message":"it failed"}' },
      [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
    });
    await renderProcedures();
    expect(screen.getByTestId("gateway-status-pill").getAttribute("data-state")).toBe("error");
    expect(screen.getByTestId("gateway-status-pill").textContent).toContain("gateway error 500");
    log.push("panel: the pill's four words are its own poll's, never a store's");
  });

  it("renders the same search again from the same URL, remembering nothing", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(QUERY))]: { status: facts.code, text: facts.text },
    });
    const opened = await renderFacts({ q: QUERY });
    const before = renderedFigures(opened.container);
    cleanup();

    const again = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(QUERY))]: { status: facts.code, text: facts.text },
    });
    const reopened = await renderFacts({ q: QUERY });
    expect(renderedFigures(reopened.container)).toEqual(before);
    expect(again.mock.calls.map((call) => call[0])).toContain(proxied(factsRoute(QUERY)));
    log.push("/memory/facts: a reload of the same URL renders the same results (`D-6`)");
  });

  it("renders nothing from a read that came back after the page was left (`D-6`)", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
    });
    const page = await ProceduresPage();
    render(shell(page));

    cleanup();
    await settle();
    expect(screen.queryByRole("region", { name: PROCEDURES_TITLE })).toBeNull();
    expect(document.body.textContent).toBe("");
    log.push("panel: an answer that arrives after the page was left is rendered nowhere (`D-6`)");
  });
});

// ---- Where each figure says it came from, proved against the bytes -------------------------------------

describe("every memory figure opens the characters it was read out of (FR-51, `D-4`)", () => {
  it("cites the fact route and a path in its bytes for every figure on the page", async () => {
    const route = factRoute(MEM_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: fact.code, text: fact.text },
    });
    const { container } = await renderFact(MEM_ID);

    // Every figure the recorded fact can put on this page: its id, text, source, status and
    // created_at. Its two collections are empty, so they are sentences and not figures.
    const figures = renderedFigures(container);
    expect(figures).toHaveLength(5);
    expect(figuresWithoutSource(container)).toEqual([]);
    expect([...new Set(figures.map((figure) => figure.route))]).toEqual([route]);

    const chars = sourceCharactersByPath(fact.text);
    expect(figures.filter((figure) => !chars.has(figure.path))).toEqual([]);
    expect(divergences(figures, new Map([[route, fact.text]]))).toEqual([]);
    log.push(`/memory/facts/<id>: ${figures.length} figures, every one a leaf of the response`);
  });

  it("opens the fact's own bytes from every chip, with the gateway's URL and no proxy path", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
    const route = factRoute(MEM_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: fact.code, text: fact.text },
    });
    await renderFact(MEM_ID);

    const scope = panel(FACT_TITLE);
    const chips = [...scope.querySelectorAll<HTMLElement>("[data-figure]")].filter(
      (chip) => chip.getAttribute("data-route") === route,
    );
    expect(chips).toHaveLength(5);
    for (const chip of chips) {
      // The gateway's own route, the same characters `data-route` carries - never a proxy path.
      expect(chip.getAttribute("data-route")).toBe(route);
      fireEvent.click(chip);
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByTestId("record-view")).toBeTruthy();
      expect(within(dialog).getByTestId("cited").textContent).toBeTruthy();
      fireEvent.keyDown(document, { key: "Escape" });
    }

    // Enter opens a chip; nothing else does, so it is not a trap for any other key.
    const first = chips[0] as HTMLElement;
    fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(first, { key: "Enter" });

    const dialog = screen.getByRole("dialog");
    // No endpoint string anywhere in the dialog: not the route, not the origin.
    expect(within(dialog).getByRole("heading").textContent).toBe("Where this came from");
    expect(dialog.textContent).not.toContain(route);
    expect(dialog.textContent).not.toContain(GATEWAY);
    // `harnessed memory fact` prints a single fact's payload since ticket 232.
    expect(within(dialog).getByTestId("cli-twin").textContent).toBe(
      "harnessed memory fact <mem_id> --json",
    );

    const controls = within(dialog).getAllByRole("button");
    const head = controls[0] as HTMLElement;
    const tail = controls[controls.length - 1] as HTMLElement;
    tail.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(head);
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(tail);
    fireEvent.keyDown(dialog, { key: "ArrowDown" });

    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(first);
    log.push(`/memory/facts/<id>: all ${chips.length} chips cite a span of the fact's own bytes`);
  });

  it("offers the CLI twin of the search route beside the curl line", async () => {
    const route = factsRoute(QUERY);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: facts.code, text: facts.text },
    });
    await renderFacts({ q: QUERY });

    const chip = figureAt(panel(FACTS_TITLE), route, ".facts[0].text");
    fireEvent.click(chip);

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByTestId("cli-twin").textContent).toBe(
      "harnessed memory search --json",
    );
    // No `curl` line anywhere in the dialog (`D-9`): raw access is the CLI twin, with a Copy button.
    expect(dialog.textContent).not.toContain("curl");
    const written: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: async (line: string) => {
          written.push(line);
        },
      },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Copy" }));
    expect(written).toEqual(["harnessed memory search --json"]);
    fireEvent.keyDown(document, { key: "Escape" });
    log.push("/memory/facts: the drawer offers `harnessed memory search --json` with a Copy button");
  });

  it("marks the occurrence the path names, not the first one that looks like it", async () => {
    const route = factsRoute(QUERY);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: facts.code, text: TWO_FACTS },
    });
    await renderFacts({ q: QUERY });

    // One value written twice: both facts are `active`, so the two chips must mark two different
    // occurrences of the same characters - structurally, by path, never by a string search that
    // would land on whichever occurrence comes first.
    const pins = [
      { path: ".facts[0].status", cited: "active" },
      { path: ".facts[1].status", cited: "active" },
    ];

    for (const pin of pins) {
      const chip = figureAt(panel(FACTS_TITLE), route, pin.path);
      fireEvent.click(chip);

      const dialog = screen.getByRole("dialog");
      const mark = within(dialog).getByTestId("cited");
      expect(mark.textContent).toBe(pin.cited);
      expect(mark.getAttribute("aria-current")).toBe("true");
      fireEvent.keyDown(document, { key: "Escape" });
    }
    log.push("/memory/facts: a chip marks the field its path names, never a look-alike earlier on");
  });
});

// ---- The merged page (ticket 208) -----------------------------------------------------------------

/** Every route the merged page reads with no parameter: the figures row and the active set. */
function bareAnswers(): Record<string, Route> {
  return {
    [HEALTH_URL]: HEALTH_OK,
    [gate.route]: { status: gate.code, text: gate.text },
    [totals.route]: { status: totals.code, text: totals.text },
    [proxied(PROCEDURES_ROUTE)]: { status: procedures.code, text: procedures.text },
  };
}

async function renderMemory(params: Record<string, string> = {}) {
  const rendered = render(shell(pageAt(MemoryPage, params)));
  await settle();
  return rendered;
}

describe("/memory stacks the figures row, Facts, Procedures and Episodes (FR-45, FR-64, FR-129)", () => {
  it("renders the figures row then the three sections, in that order, each with its id", async () => {
    const spy = answersWith(bareAnswers());
    const { container } = await renderMemory();

    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["Memory"]);
    expect(screen.getByRole("region", { name: "Memory check" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Memory" })).toBeTruthy();

    const sections = ["facts", "procedures", "episodes"].map((id) => container.querySelector(`section#${id}`));
    for (const section of sections) expect(section).not.toBeNull();
    const order = Array.from(container.querySelectorAll("section#facts, section#procedures, section#episodes")).map(
      (node) => node.id,
    );
    expect(order).toEqual(["facts", "procedures", "episodes"]);
    expect(within(sections[1] as HTMLElement).getByRole("region", { name: PROCEDURES_TITLE })).toBeTruthy();

    // Nothing is asked for facts or episodes until the URL carries a query or a session id.
    const asked = spy.mock.calls.map((call) => call[0] as string);
    expect(asked.some((url) => url.includes("/memory/facts") || url.includes("/memory/episodes"))).toBe(false);
    expect(screen.getByTestId("facts-prompt").textContent).toBe(NEEDS_QUERY);
    expect(screen.getByTestId("episodes-prompt").textContent).toBe(NEEDS_SESSION);
  });

  it("renders the episode lookup exactly once", async () => {
    answersWith(bareAnswers());
    await renderMemory();
    expect(screen.getAllByRole("region", { name: LOOKUP_TITLE })).toHaveLength(1);
  });

  it("?q= fills the Facts section with the search results", async () => {
    answersWith({ ...bareAnswers(), [factsRoute(QUERY)]: { status: facts.code, text: facts.text } });
    await renderMemory({ q: QUERY });
    const section = document.querySelector("section#facts") as HTMLElement;
    expect(within(section).getByRole("region", { name: FACTS_TITLE })).toBeTruthy();
    expect(screen.queryByTestId("facts-prompt")).toBeNull();
  });

  it("?id= shows the one fact in the Facts section", async () => {
    answersWith({ ...bareAnswers(), [factRoute(MEM_ID)]: { status: fact.code, text: fact.text } });
    await renderMemory({ id: MEM_ID });
    const section = document.querySelector("section#facts") as HTMLElement;
    expect(within(section).getByRole("region", { name: FACT_TITLE })).toBeTruthy();
  });

  it("?session= fills the Episodes section with that session's episodes", async () => {
    answersWith({ ...bareAnswers(), [episodesRoute(SESSION_ID)]: { status: episodes.code, text: episodes.text } });
    await renderMemory({ session: SESSION_ID });
    const section = document.querySelector("section#episodes") as HTMLElement;
    expect(within(section).getByRole("region", { name: EPISODES_TITLE })).toBeTruthy();
    expect(screen.queryByTestId("episodes-prompt")).toBeNull();
  });
});

describe("the old memory URLs forward to /memory (ticket 208)", () => {
  it.each([
    { Page: FactsForward, at: { q: "gateway" }, to: "/memory?q=gateway" },
    { Page: FactsForward, at: { id: "mem_1" }, to: "/memory?id=mem_1" },
    { Page: ProceduresForward, at: {}, to: "/memory#procedures" },
    { Page: EpisodesForward, at: { session: "run_1" }, to: "/memory?session=run_1" },
  ] as { Page: () => ReactNode; at: Record<string, string>; to: string }[])("lands on $to", ({ Page, at, to }) => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(pageAt(Page, at));
    expect(pushed).toEqual([to]);
    expect(screen.getByTestId("forward-link").querySelector("a")?.getAttribute("href")).toBe(to);
  });
});

// ---- Ticket 254: Consolidation (FR-155, UJ-25) ----------------------------------------------------

describe("Memory shows consolidation (FR-155)", () => {
  const consolidation = recorded("memory.consolidation");
  const noConsolidation = recorded("empty.memory.consolidation");

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-01-01T14:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  async function renderMemory(text: string) {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      "/v1/memory/consolidation": { status: 200, text },
    });
    const rendered = render(shell(pageAt(MemoryPage)));
    await settle();
    return rendered;
  }

  it("follows the figures row with the served line, newest batches first, and a failed batch that wrote nothing", async () => {
    await renderMemory(consolidation.text);
    const region = screen.getByRole("region", { name: "Consolidation" });
    expect(within(region).getByTestId("consolidation-line").textContent).toBe(
      "Turns past sessions into facts after every 20 new episodes. 3 so far.",
    );
    const rows = within(region).getAllByTestId("consolidation-row");
    expect(rows).toHaveLength(2);
    // Served order: the failed batch first.
    expect(rows[0].textContent).toContain("Wrote nothing. The 2 episodes wait for the next batch.");
    expect(rows[0].textContent).toContain("Failed");
    expect(rows[0].textContent).toContain("fake-1");
    expect(rows[0].textContent).toContain("2 h ago");
    expect(within(rows[0]).queryByRole("button", { name: /facts/ })).toBeNull();
    expect(rows[1].textContent).toContain("1 fact from 2 episodes");
    expect(rows[1].textContent).toContain("Consolidated");
    // The figures row comes first, then the panel.
    const facts = screen.getByRole("region", { name: "Facts" });
    expect(region.compareDocumentPosition(facts) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(figuresWithoutSource(document.body)).toEqual([]);
  });

  it("reads each batch's written, skipped and archived counts as served, a null skipped with its reason (FR-170)", async () => {
    await renderMemory(consolidation.text);
    const rows = screen.getAllByTestId("consolidation-row");
    const counts = rows.map((row) => within(row).getByTestId("consolidation-counts").textContent);
    expect(counts[0]).toBe("0 written, unknowncounted from v7 on skipped, 0 archived");
    expect(counts[1]).toBe("1 written, 0 skipped, 0 archived");
    expect(figuresWithoutSource(document.body)).toEqual([]);
  });

  it("expands a batch to the facts it wrote, each with an Open link", async () => {
    await renderMemory(consolidation.text);
    const row = screen.getAllByTestId("consolidation-row")[1];
    const toggle = within(row).getByRole("button", { name: "Show facts" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(row.textContent).toContain("The operator prefers British spelling.");
    const open = within(row).getByRole("link", { name: "Open" });
    expect(open.getAttribute("href")).toBe("/memory?id=mem_0000000011VNMPK1HTSMYR02BN");
  });

  it("offers the real command when nothing has run, and the diagram says no consolidation yet", async () => {
    await renderMemory(noConsolidation.text);
    const empty = screen.getByTestId("consolidation-empty");
    expect(empty.textContent).toContain("No consolidation has run yet. It runs after 20 new episodes, or by hand:");
    expect(screen.getByTestId("consolidation-command").textContent).toBe("harnessed memory consolidate");
    expect(screen.getByTestId("consolidation-status").textContent).toBe("No consolidation yet");
  });

  it("lends the newest batch to the diagram's second status line", async () => {
    await renderMemory(consolidation.text);
    expect(screen.getByTestId("consolidation-status").textContent).toBe(
      "Last consolidation: 0 facts from 2 episodes, 2 h ago",
    );
  });
});

// ---- Ticket 271: a consolidated fact says where it came from in words (FR-155) ------------------

describe("a consolidated fact says where it came from in words (FR-155)", () => {
  const BATCH_SOURCE = "consolidation:bat_d61f115251157c40";
  const CONSOLIDATED_ID = "mem_0000000004JG1MZGXAV3RTMY1W";
  const CONSOLIDATED = [
    `{"created_at":"2026-01-01T12:00:00Z","links":[],"mem_id":"${CONSOLIDATED_ID}","source":"${BATCH_SOURCE}",`,
    '"status":"active","tags":[],"text":"Uncommitted work is kept."}',
  ].join("");
  const CONSOLIDATED_LIST = `{"facts":[${CONSOLIDATED}]}`;

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-01-01T14:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reads 'From consolidation, 2 h ago' in the Facts table, the exact source in the tooltip", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(QUERY))]: { status: 200, text: CONSOLIDATED_LIST },
    });
    await renderFacts({ q: QUERY });
    const cell = figureAt(panel(FACTS_TITLE), factsRoute(QUERY), ".facts[0].source");
    expect(cell.textContent).toBe("From consolidation, 2 h ago");
    expect(cell.getAttribute("title")).toContain(BATCH_SOURCE);
  });

  it("reads the same words on the fact's view, with the exact source as a field beside them", async () => {
    const route = factRoute(CONSOLIDATED_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: 200, text: CONSOLIDATED },
    });
    await renderFact(CONSOLIDATED_ID);
    const scope = panel(FACT_TITLE);
    expect(figureAt(scope, route, ".source").textContent).toBe("From consolidation, 2 h ago");
    expect(within(scope).getByTestId("fact-source-exact").textContent).toContain(BATCH_SOURCE);
  });

  it("leaves any other source as it is today", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(factsRoute(QUERY))]: { status: 200, text: TWO_FACTS },
    });
    await renderFacts({ q: QUERY });
    const cell = figureAt(panel(FACTS_TITLE), factsRoute(QUERY), ".facts[0].source");
    expect(cell.textContent).toBe("operator");
  });
});
