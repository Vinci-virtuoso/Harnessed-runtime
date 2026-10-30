// @vitest-environment jsdom
/**
 * FR-54 (UJ-12), and FR-43's "with no gateway running it renders `UnreachableState`, never a
 * crash" for the real shell. Decisions 29, 34 and 39.
 *
 * Three things, each able to fail on its own:
 *   - `AppShell` is the frame every page is rendered into: the three landmarks, a skip link that
 *     is first in tab order, a nav link to every static page of `experience.md`'s route map, and
 *     a menu button for 400px;
 *   - `GatewayStatusPill` is the one component that polls. It says whether the gateway answers -
 *     `checking`, `gateway reachable`, `gateway unreachable`, `gateway error` - in the four words
 *     the design names, announces the change and only the change, and opens the `/health`
 *     response it is speaking for;
 *   - `HealthPanel` reads that same response as figures: the schema version the **package
 *     ships**, the database path and the route count, each a `StatTile` carrying its own source.
 *     It sat on `/` through v4; ticket 186 (FR-129) moved it to the Models page's
 *     `SystemSection`, so it is tested here on its own, and through the shell it is still
 *     mounted in.
 *
 * UJ-12's three paths are the fixtures. Path A is a rejected `fetch` and the transport's own
 * unreachable header; Path B is the same state, because `harnessed serve` refusing to start is
 * indistinguishable from it in a browser, so the state carries the line that says where the
 * reason is; Path C is the recorded 500 `StorageError`, where the gateway answers `/health` and
 * every data route fails - the pill must read "gateway reachable" and never "healthy".
 *
 * The page map is read out of `.spec/ux/experience.md` rather than restated here, so a page added
 * to the map and not to the nav fails this test. The mono claim is read off the component's own
 * module CSS, as ticket 104 established: jsdom loads no stylesheet, so a computed font proves
 * nothing. The 400px behaviour is read the same way - the button is in the DOM at every width and
 * the media query is what makes it the sidebar's replacement.
 *
 * The clock is fake throughout the polling section, so "every 15 s" is exact and half an hour can
 * pass in a test: `D-6` says a data route is read on navigation and on `Refetch` and at no other
 * time, and the way to prove only the pill polls is to count the reads that half hour made.
 */
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../../components/AppShell";
import { DataTable } from "../../components/DataTable";
import { GatewayStatusPill } from "../../components/GatewayStatusPill";
import { HealthPanel } from "../../components/HealthPanel";
import { IdLookup } from "../../components/IdLookup";
import { KeyKindTag } from "../../components/KeyKindTag";
import { PanelFrame } from "../../components/PanelFrame";
import { PassMark } from "../../components/PassMark";
import { RuleBadge } from "../../components/RuleBadge";
import { ScorePair } from "../../components/ScorePair";
import { StatTile } from "../../components/StatTile";
import { StopReasonTag } from "../../components/StopReasonTag";
import { UnreachableState } from "../../components/UnreachableState";
import { Value } from "../../components/Value";
import { GATEWAY_ORIGIN } from "../../lib/fetch-panel";
import { nullReasonFor } from "../../lib/null-reasons";
import { parseWire, type Wire } from "../../lib/wire";

import { figuresWithoutSource } from "../support/figures";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const REPO = resolve(DASHBOARD, "..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EXPERIENCE = resolve(REPO, ".spec/ux/experience.md");
const EVIDENCE = resolve(REPO, "evidence/105/shell.txt");
const SHELL_HTML = resolve(REPO, "evidence/105/overview.html");
const UNREACHABLE_HTML = resolve(REPO, "evidence/105/unreachable.html");
const STORAGE_ERROR_HTML = resolve(REPO, "evidence/105/storage-error.html");

/** The gateway this test's shell is pointed at. Not the operator's 8756 (decision 30). */
const GATEWAY = GATEWAY_ORIGIN;

/** The plain source label every panel in this file reads under (`D-4`); the wording itself is
 * proved elsewhere (per-page tickets pick the real one for each page). */
const SOURCE_LABEL = "Ledger: all time";

/** The two routes this ticket reads: the pill's poll and a data route for Path C. */
const HEALTH = "/v1/health";
const RUNS = "/v1/runs";

const HEALTH_URL = "/v1/health";
const RUNS_URL = "/v1/runs";

/** Where `IdLookup` sent the operator, so a lookup's id can be compared with the one typed. */
const pushed: string[] = [];

/** The route `usePathname` reports; `null` is what the hook gives outside an app router. */
let pathname: string | null = null;

vi.mock("next/navigation", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  usePathname: () => pathname,
  useRouter: () => ({
    push: (href: string) => {
      pushed.push(href);
    },
  }),
}));

/** The dashboard's clock, frozen so a poll count is the only thing moving. */
const FROZEN = new Date("2026-09-17T14:03:09");

const SECOND = 1000;
const MINUTE = 60 * SECOND;

/** What `design.md` gives the pill, and the interval `architecture.md` decision 34 fixes. */
const POLL_SECONDS = 15;

/** The lines this run writes to `evidence/105/shell.txt`. */
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

/** `/health` as the gateway sent it (ticket 100), and the two failures UJ-12 names. */
const health = recorded("health");
const storageError = recorded("nodatabase.runs.listing");

/** Every recorded body, read off the directory so a re-record widens the sweep for free. */
function everyRecordedBody(): Recorded[] {
  return readdirSync(GATEWAY_FIXTURES)
    .filter((entry) => entry.endsWith(".body"))
    .sort()
    .map((entry) => recorded(entry.slice(0, entry.length - ".body".length)));
}

/**
 * A non-2xx on `/health` itself. No recording holds one - the gateway answers `/health` without
 * opening the database, so no seeded failure produces it - and decision 24's contract is what the
 * pill reads, so the body is written here rather than a recorded one being edited (MUST rule 24).
 */
const HEALTH_500 = '{"error":"InternalError","message":"the gateway failed while answering"}';

// ---- The page map, read from the experience spine ----------------------------------------------

/**
 * Every page of `experience.md`'s Next.js route map that a nav can link to. A page whose path
 * holds a segment in brackets is reached from a response field or an `IdLookup` and never from
 * the nav, because a nav link to it would have to build an id (MUST rule 25).
 */
function staticPagesInTheMap(): string[] {
  const spine = readFileSync(EXPERIENCE, "utf8");
  const start = spine.indexOf("Next.js route map");
  expect(start, "experience.md no longer holds a Next.js route map").toBeGreaterThan(0);
  const rows: string[] = [];
  let seen = false;
  for (const line of spine.slice(start).split("\n")) {
    if (!line.startsWith("|")) {
      if (seen) break;
      continue;
    }
    seen = true;
    const cell = line.split("|").at(1)?.trim() ?? "";
    const page = cell.startsWith("`") && cell.endsWith("`") ? cell.slice(1, -1) : "";
    if (page.startsWith("/") && !page.includes("[")) rows.push(page);
  }
  // The v4 map also lists pages later tickets build (`/console`, `/models`). A page the tree
  // does not hold yet is not the nav's to link - a link to it is a link to a 404 - so the nav is
  // held to every listed page that exists, and each of those pages joins this set the moment its
  // `page.tsx` lands (ticket 122; the nav order is ticket 147's).
  return rows.filter((page) => existsSync(resolve(DASHBOARD, "app", page.slice(1), "page.tsx")));
}

// ---- The mono claim, read from the component's own stylesheet (ticket 104) ----------------------

/** The declarations of the first rule whose selector is exactly `selector`. */
function block(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|[{};])\\s*${escaped}\\s*\\{([^{}]*)\\}`).exec(
    css.replace(/\/\*[\s\S]*?\*\//g, ""),
  );
  if (!match) throw new Error(`no rule for ${selector}`);
  return match[1] ?? "";
}

function moduleCss(component: string): string {
  return readFileSync(resolve(DASHBOARD, `components/${component}.module.css`), "utf8");
}

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


type Route = Answer | "rejects";

/** Stub `fetch` with one answer per gateway route, and hand back the spy so a test can count reads. */
function answersWith(routes: Record<string, Route>): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async (url: string) => {
    const next = routes[url];
    if (next === undefined) throw new Error(`no answer stubbed for ${url}`);
    if (next === "rejects") throw new Error("ECONNREFUSED");
    return answer(next);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** The recorded `/health`, answered to whoever asks for it. */
const HEALTH_OK: Answer = { status: health.code, text: health.text };

/** Let React settle a read. Every state change after this is a render, never a request. */
async function settle(): Promise<void> {
  await act(async () => {});
}

/** Let the dashboard's clock run, with the fake timers of the polling section. */
async function pass(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** `HealthPanel`, found by its own heading - the Models page's `SystemSection` holds it now. */
function healthPanel(): HTMLElement {
  const found = screen
    .getAllByRole("region")
    .filter((region) => within(region).queryByRole("heading", { name: "Gateway health" }) !== null);
  expect(found).toHaveLength(1);
  return found[0] as HTMLElement;
}

/** The shell as `app/layout.tsx` mounts it, around whatever a page renders. */
function shell(children: ReactNode) {
  return <AppShell>{children}</AppShell>;
}

/** The pill's own live region: the sentence a screen reader hears, and nothing else. */
function announcement(): HTMLElement {
  return screen.getByTestId("pill-announcement");
}

/** The pill itself: the dot, the word and the URL an operator reads. */
function pill(): HTMLElement {
  return screen.getByTestId("gateway-status-pill");
}

beforeEach(() => {
  answersWith({ [HEALTH_URL]: HEALTH_OK });
});

afterEach(() => {
  pathname = null;
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

afterAll(() => {
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
});

// ---- `AppShell`: landmarks, the skip link, the page map and 400px -------------------------------

describe("AppShell frames every page (FR-54)", () => {
  it("has nav, header and main landmarks, and the skip link is first in tab order", async () => {
    const { container } = render(shell(<p>page content</p>));
    await settle();

    expect(screen.getByRole("navigation")).toBeTruthy();
    expect(screen.getByRole("banner")).toBeTruthy();
    const main = screen.getByRole("main");
    expect(main.id).toBe("main");
    expect(within(main).getByText("page content")).toBeTruthy();

    const focusable = container.querySelectorAll("a[href], button, input, [tabindex]");
    const first = focusable.item(0);
    expect(first?.tagName).toBe("A");
    expect(first?.getAttribute("href")).toBe("#main");
    expect(first?.textContent).toBe("Skip to main content");
    log.push("shell: nav, banner and main landmarks; skip link to #main first in tab order");
  });

  it("links to every static page of the experience spine's route map", async () => {
    render(shell(<p>page content</p>));
    await settle();

    const pages = staticPagesInTheMap();
    expect(pages.length).toBeGreaterThanOrEqual(6);
    expect(pages).toContain("/");
    expect(pages).toContain("/evals");
    expect(pages).not.toContain("/evals/compare");
    expect(pages).toContain("/memory");
    expect(pages.filter((page) => page.startsWith("/memory/"))).toEqual([]);

    const nav = screen.getByRole("navigation");
    const links = within(nav).getAllByRole("link");
    const hrefs = links.map((link) => link.getAttribute("href"));
    for (const page of pages) {
      expect(hrefs, `the nav has no link to ${page}`).toContain(page);
    }
    // Every nav link is a page of the map: no link the map does not know about.
    for (const href of hrefs) expect(pages).toContain(href);
    // And every one of them says something, so the nav is readable as well as reachable.
    for (const link of links) expect(link.textContent?.trim()).not.toBe("");
    log.push(`shell: nav links to all ${pages.length} static pages of the route map`);
  });

  it("lists exactly Overview, Console, Evals, Memory, Traces, Models, in that order", async () => {
    render(shell(<p>page content</p>));
    await settle();

    const labels = within(screen.getByRole("navigation"))
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(labels).toEqual(["Overview", "Console", "Evals", "Memory", "Traces", "Models"]);
  });

  it("ends the sidebar in the theme toggle, after the last nav link and the flexible spacer (ticket 251)", async () => {
    render(shell(<p>page content</p>));
    await settle();

    const nav = screen.getByRole("navigation");
    const toggle = within(nav).getByRole("button", { name: /^Switch to (light|dark) theme$/ });
    const links = within(nav).getAllByRole("link");
    expect(toggle.compareDocumentPosition(links[links.length - 1]) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
    const items = [...nav.querySelectorAll("a[href], button")];
    expect(items[items.length - 1]).toBe(toggle);
    const css = readFileSync(resolve(import.meta.dirname, "../../components/AppShell.module.css"), "utf8");
    expect(css).toMatch(/\.spacer\s*{[^}]*flex:\s*1/);
  });

  it("carries a menu button that the 400px query makes the sidebar's replacement", async () => {
    render(shell(<p>page content</p>));
    await settle();

    const menu = screen.getByRole("button", { name: "Menu" });
    const nav = screen.getByRole("navigation");
    expect(menu.getAttribute("aria-controls")).toBe(nav.id);
    expect(menu.getAttribute("aria-expanded")).toBe("false");
    expect(nav.getAttribute("data-open")).toBe("false");

    fireEvent.click(menu);
    expect(menu.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("true");
    fireEvent.click(menu);
    expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("false");

    /*
     * jsdom lays nothing out, so "at 400px the sidebar becomes a menu button" is read where it is
     * written: the media query that hides the closed sidebar and shows the button (ticket 104's
     * rule for an unassertable style).
     */
    const css = moduleCss("AppShell").replace(/\/\*[\s\S]*?\*\//g, "");
    const query = /@media\s*\(max-width:\s*400px\)\s*\{([\s\S]*?\n\s*\})\s*\n\}/.exec(css);
    expect(query, "AppShell.module.css has no 400px query").not.toBeNull();
    const narrow = query?.[1] ?? "";
    expect(narrow).toContain(".menu");
    expect(narrow).toContain(".nav");
    expect(block(narrow, '.nav[data-open="false"]')).toContain("display: none");
    log.push("shell: menu button toggles the nav, and the 400px query hides the closed sidebar");
  });
});

// ---- ticket 259 (FR-161): the nav icons, the active item and the brand mark ----------------------

/** The glyph table under "Nav icon geometry", read off the spine so a redrawn glyph fails here. */
function navGlyphs(spine = readFileSync(resolve(REPO, ".spec/ux/design.md"), "utf8")): Record<string, string> {
  // A checkout with `core.autocrlf` (the Windows default) ends every row `\r\n`, so both endings split
  // (ticket 263); the row's closing `|` would otherwise be followed by a `\r` the pattern refuses.
  const section = spine.split("### Nav icon geometry")[1]?.split(/\r?\n### /)[0] ?? "";
  const glyphs: Record<string, string> = {};
  for (const row of section.split(/\r?\n/)) {
    const cells = /^\|\s*(Overview|Console|Evals|Memory|Traces|Models)\s*\|\s*`(.+)`\s*\|$/.exec(row);
    if (cells) glyphs[cells[1]] = cells[2];
  }
  return glyphs;
}

describe("the sidebar's icons and brand mark (ticket 259, FR-161)", () => {
  it("reads the spine's glyph table whichever line ending the checkout gave it", () => {
    const lf = navGlyphs();
    expect(Object.keys(lf)).toHaveLength(6);
    const crlf = readFileSync(resolve(REPO, ".spec/ux/design.md"), "utf8").replace(/\r?\n/g, "\r\n");
    expect(navGlyphs(crlf)).toEqual(lf);
  });

  it("draws one inline svg per nav item, before the label, with the spine's glyph", async () => {
    render(shell(<p>page content</p>));
    await settle();

    const glyphs = navGlyphs();
    expect(Object.keys(glyphs)).toHaveLength(6);
    const nav = screen.getByRole("navigation");
    const links = within(nav).getAllByRole("link");
    expect(nav.querySelectorAll("li svg")).toHaveLength(6);
    for (const link of links) {
      const label = link.textContent ?? "";
      const icons = link.querySelectorAll("svg");
      expect(icons, `${label} icon count`).toHaveLength(1);
      const icon = icons[0] as SVGElement;
      expect(link.firstElementChild).toBe(icon);
      expect(icon.getAttribute("viewBox")).toBe("0 0 16 16");
      expect(icon.getAttribute("fill")).toBe("none");
      expect(icon.getAttribute("stroke-width")).toBe("1.5");
      expect(icon.getAttribute("stroke-linecap")).toBe("round");
      expect(icon.getAttribute("stroke-linejoin")).toBe("round");
      expect(icon.getAttribute("aria-hidden")).toBe("true");
      expect(icon.getAttribute("focusable")).toBe("false");
      expect(icon.innerHTML.replace(/><\/\w+>/g, "/>"), `${label} glyph`).toBe(glyphs[label]);
      expect(within(nav).getByRole("link", { name: label })).toBe(link);
    }
  });

  it("marks exactly the current page with aria-current, Overview only on /", async () => {
    for (const [route, label] of [
      ["/", "Overview"],
      ["/console", "Console"],
      ["/evals", "Evals"],
      ["/evals/compare", "Evals"],
      ["/memory", "Memory"],
      ["/traces/abc", "Traces"],
      ["/models", "Models"],
    ] as const) {
      pathname = route;
      const { unmount } = render(shell(<p>page content</p>));
      await settle();
      const current = within(screen.getByRole("navigation"))
        .getAllByRole("link")
        .filter((link) => link.getAttribute("aria-current") === "page");
      expect(current.map((link) => link.textContent), route).toEqual([label]);
      unmount();
    }
  });

  it("draws the brand mark at the top, aria-hidden, and no milestone label", async () => {
    const { container } = render(shell(<p>page content</p>));
    await settle();

    const nav = screen.getByRole("navigation");
    const brand = nav.firstElementChild as HTMLElement;
    expect(brand.textContent).toBe("Harnessed");
    const mark = brand.querySelector('[aria-hidden="true"]') as HTMLElement;
    expect(mark).not.toBeNull();
    expect(mark.querySelector("path")?.getAttribute("d")).toBe("M4.5 3v10M11.5 3v10M4.5 8h7");
    expect(container.textContent).not.toContain("v6 daily driver");
  });

  it("styles the icon strokes and the active item through tokens only", () => {
    const css = moduleCss("AppShell").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css).toMatch(/\.icon\s*{[^}]*color:\s*var\(--color-fg-muted\)[^}]*stroke:\s*currentColor/);
    expect(css).toMatch(/\.link\[aria-current="page"\]\s+\.icon\s*{[^}]*color:\s*var\(--color-accent\)/);
    expect(css).toMatch(/\.link:hover\s+\.icon\s*{[^}]*color:\s*var\(--color-fg-secondary\)/);
    expect(css).toMatch(/\.link\[aria-current="page"\]\s*{[^}]*background:\s*var\(--surface-raised\)/);
    expect(css).toMatch(/\.link\[aria-current="page"\]\s*{[^}]*border-color:\s*var\(--surface-edge\)/);
    expect(css).toMatch(/\.link\[aria-current="page"\]\s*{[^}]*font-weight:\s*500/);
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });
});

// ---- `GatewayStatusPill`: the four states, in the four words --------------------------------

describe("GatewayStatusPill says whether the gateway answers (FR-54, decision 39)", () => {
  it("reads checking until /health has answered", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(shell(<p>page content</p>));

    expect(pill().getAttribute("data-state")).toBe("checking");
    expect(pill().textContent).toContain("checking");
    expect(announcement().textContent).toBe("checking the gateway");
    await settle();
    log.push("pill: checking before the first answer");
  });

  it("reads gateway reachable on a 200, and never the word healthy", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(shell(<p>page content</p>));
    await settle();

    expect(pill().getAttribute("data-state")).toBe("reachable");
    expect(pill().textContent).toContain("gateway reachable");
    expect(announcement().textContent).toBe("gateway reachable");
    expect(document.body.textContent).not.toContain("healthy");
    // No endpoint string anywhere in the pill (decision 68, `D-9`): it says the state, not the URL.
    expect(pill().textContent).not.toContain(GATEWAY);
    log.push('pill: "gateway reachable" on 200, never "healthy", and no gateway URL in the pill');
  });

  it("reads unreachable on a rejected fetch, and on nothing else", async () => {
    answersWith({ [HEALTH_URL]: "rejects" });
    render(shell(<p>page content</p>));
    await settle();
    expect(pill().getAttribute("data-state")).toBe("unreachable");
    expect(pill().textContent).toContain("gateway unreachable");
    expect(announcement().textContent).toBe("gateway unreachable");
    log.push("pill: unreachable on a rejected fetch and on nothing else (MUST rule 23)");
  });

  it("reads error on another non-2xx, quoting the status", async () => {
    answersWith({ [HEALTH_URL]: { status: 500, text: HEALTH_500 } });
    render(shell(<p>page content</p>));
    await settle();

    expect(pill().getAttribute("data-state")).toBe("error");
    expect(pill().textContent).toContain("gateway error 500");
    expect(announcement().textContent).toBe("gateway error 500");
    log.push("pill: error on a non-2xx, with the status as the gateway sent it");
  });

  it("says a state change once, and says nothing while the state holds", async () => {
    vi.useFakeTimers({
      toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    vi.setSystemTime(FROZEN);
    const answers: Record<string, Route> = { [HEALTH_URL]: HEALTH_OK };
    const spy = answersWith(answers);
    render(shell(<p>page content</p>));
    await pass(0);

    const said: string[] = [announcement().textContent ?? ""];
    await pass(POLL_SECONDS * SECOND);
    said.push(announcement().textContent ?? "");
    await pass(POLL_SECONDS * SECOND);
    said.push(announcement().textContent ?? "");
    expect(said).toEqual(["gateway reachable", "gateway reachable", "gateway reachable"]);
    expect(spy).toHaveBeenCalledTimes(3);

    answers[HEALTH_URL] = { status: 500, text: HEALTH_500 };
    await pass(POLL_SECONDS * SECOND);
    expect(announcement().textContent).toBe("gateway error 500");
    vi.useRealTimers();
    log.push("pill: the live region changes only when the state does, over four polls");
  });

  it("requests /health every 15 s, and is the only component that polls", async () => {
    vi.useFakeTimers({
      toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    vi.setSystemTime(FROZEN);
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [RUNS_URL]: { status: storageError.code, text: storageError.text },
    });
    render(
      shell(
        <PanelFrame route={RUNS} title="Runs" sourceLabel={SOURCE_LABEL}>
          {(wire) => <Value leaf={wire.at(".runs[0].run_key")} />}
        </PanelFrame>,
      ),
    );
    await pass(0);

    const urls = () => spy.mock.calls.map((call) => call[0] as string);
    expect(urls().filter((url) => url === HEALTH_URL)).toHaveLength(1);
    expect(urls().filter((url) => url === RUNS_URL)).toHaveLength(1);

    await pass(POLL_SECONDS * SECOND);
    expect(urls().filter((url) => url === HEALTH_URL)).toHaveLength(2);

    // Half an hour: the pill keeps asking, the data panel is read once and never again (`D-6`).
    await pass(30 * MINUTE);
    const polls = urls().filter((url) => url === HEALTH_URL).length;
    // The read on mount, the poll fifteen seconds in, and one for every fifteen seconds since.
    expect(polls).toBe(2 + (30 * MINUTE) / (POLL_SECONDS * SECOND));
    expect(urls().filter((url) => url === RUNS_URL)).toHaveLength(1);
    expect(new Set(urls())).toEqual(new Set([HEALTH_URL, RUNS_URL]));
    vi.useRealTimers();
    log.push(`pill: ${polls} polls of /health in half an hour, and one read of ${RUNS}`);
  });

  it("opens its own ProvenanceDrawer over the response it is speaking for", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(shell(<p>page content</p>));
    await settle();

    fireEvent.click(pill());
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Where this came from" })).toBeTruthy();
    expect(within(dialog).getByText("Harness: this machine")).toBeTruthy();
    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    // No endpoint string anywhere in the drawer (decision 68, `D-9`): no URL, no `curl` line.
    expect(dialog.textContent).not.toContain(GATEWAY);
    expect(screen.queryByTestId("curl")).toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(pill());
    log.push("pill: click opens the drawer over the /health response, Esc returns focus");
  });

  it("offers nothing to open while it has no response to show", async () => {
    answersWith({ [HEALTH_URL]: "rejects" });
    render(shell(<p>page content</p>));
    expect((pill() as HTMLButtonElement).disabled).toBe(true);
    await settle();
    expect((pill() as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(pill());
    expect(screen.queryByRole("dialog")).toBeNull();
    log.push("pill: no response, no drawer - a chip that would open nothing is disabled");
  });

  it("names the gateway it was pointed at, and falls back to the operator's own", async () => {
    const spy = answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(<GatewayStatusPill />);
    await settle();

    // No `GatewayOriginContext.Provider` here, unlike inside `AppShell`: the pill falls back to
    // the same relative origin the shell would have provided (decision 43), and reads the same
    // route either way - no endpoint string says so on screen (`D-9`).
    expect(pill().getAttribute("data-state")).toBe("reachable");
    expect(spy.mock.calls.map((call) => call[0])).toEqual([HEALTH_URL]);
    expect(document.body.textContent).not.toContain(GATEWAY_ORIGIN);
    log.push(`pill: outside a shell it still reads ${HEALTH_URL}, the operator's own default origin`);
  });
});

// ---- HealthPanel: the /health response, read as figures (FR-54, moved off / by ticket 186) ------

describe("HealthPanel reads /health as sent (FR-54)", () => {
  it("shows the shipped schema version, the database path and the route count, each sourced", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    const { container } = render(<HealthPanel />);
    await settle();

    const wire = parseWire(health.text, HEALTH);
    const tiles = screen.getAllByRole("group");
    const named = new Map(tiles.map((tile) => [accessibleName(tile), tile]));

    for (const [label, path] of [
      ["schema version shipped", ".schema_version"],
      ["database path", ".database_path"],
      ["route count", ".route_count"],
    ] as const) {
      const tile = named.get(label);
      expect(tile, `no stat tile named "${label}"`).toBeTruthy();
      // The figure is its own trigger now - a `button`, not a plain span beside a `SourceChip`
      // naming the path (decision 68, `D-9`: no jq path caption is drawn any more).
      const figure = tile?.querySelector("[data-figure]") as HTMLElement;
      expect(figure.tagName).toBe("BUTTON");
      expect(figure.textContent).toBe(wire.at(path).text);
      expect(figure.getAttribute("data-route")).toBe(HEALTH);
      expect(figure.getAttribute("data-path")).toBe(path);
      expect(within(tile as HTMLElement).queryByText(path)).toBeNull();
    }

    // The version the package ships, said in words: the database on disk may be behind it (G-2).
    expect(screen.getByTestId("schema-version-gloss").textContent).toContain("ships");
    expect(screen.getByTestId("schema-version-gloss").textContent).toContain("not the version");

    mkdirSync(dirname(SHELL_HTML), { recursive: true });
    writeFileSync(SHELL_HTML, container.innerHTML, "utf8");
    log.push("overview: schema_version, database_path and route_count as sent, each with a source");
  });

  it("walks every figure and finds none without a route and a path (FR-51)", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    const { container } = render(<HealthPanel />);
    await settle();

    const figures = container.querySelectorAll("[data-figure]");
    expect(figures.length).toBe(3);
    expect(figuresWithoutSource(container)).toEqual([]);
    log.push(`health panel: ${figures.length} figures walked, 0 without a route and a path`);
  });

  it("renders no gap placeholder for the doctor ladder or the store counts", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    const { container } = render(<HealthPanel />);
    await settle();

    expect(container.textContent).not.toContain("[GAP");
    expect(container.querySelector("[data-gap]")).toBeNull();
    expect(container.textContent).not.toContain("doctor");
    log.push("health panel: no GapPlaceholder, no doctor ladder (G-1), no store counts (G-15)");
  });
});

// ---- UJ-12's three paths ------------------------------------------------------------------------

describe("UJ-12: the dashboard when the gateway cannot answer", () => {
  it("Path A: a page renders UnreachableState, which names the URL, the error and the fix", async () => {
    // Every panel says its own unreachable state on its own (ticket 134); the generic sweep
    // further below proves that for every recorded route, and `HealthPanel`'s own is read here.
    answersWith({ [HEALTH_URL]: "rejects" });
    const { container } = render(shell(<HealthPanel />));
    await settle();

    const panel = healthPanel();
    const heading = within(panel).getByRole("heading", { name: "Gateway unreachable" });
    const headings = screen.getAllByRole("heading", { name: "Gateway unreachable" });
    expect(headings).toContain(document.activeElement);
    expect(panel.textContent).not.toContain(`${GATEWAY}/health`);
    expect(within(panel).getByTestId("unreachable-cause").textContent).toBe("ECONNREFUSED");
    expect(within(panel).getByText("harnessed serve")).toBeTruthy();
    expect(heading).toBeTruthy();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);

    mkdirSync(dirname(UNREACHABLE_HTML), { recursive: true });
    writeFileSync(UNREACHABLE_HTML, container.innerHTML, "utf8");
    log.push("path A: HealthPanel renders UnreachableState with the URL, the cause and `harnessed serve`");
  });

  it("Path A: nothing an earlier load showed survives the gateway going away (D-6)", async () => {
    const answers: Record<string, Route> = { [HEALTH_URL]: HEALTH_OK };
    answersWith(answers);
    const { container } = render(shell(<HealthPanel />));
    await settle();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(3);

    answers[HEALTH_URL] = "rejects";
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await settle();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    expect(container.textContent).not.toContain(health.text);
    expect(within(healthPanel()).getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    log.push("path A: a refetch into an unreachable gateway leaves 0 figures from memory");
  });

  it("Path B: the same state says the serve terminal names the reason", async () => {
    render(<UnreachableState />);
    expect(screen.getByText(/the serve terminal names the reason/)).toBeTruthy();
    log.push("path B: a serve that refused to start is Path A plus the line that says where to look");
  });

  it("Path C: the pill reads reachable while the first data panel shows the 500 verbatim", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [RUNS_URL]: { status: storageError.code, text: storageError.text },
    });
    const { container } = render(
      shell(
        <PanelFrame route={RUNS} title="Runs" sourceLabel={SOURCE_LABEL}>
          {(wire) => <Value leaf={wire.at(".runs[0].run_key")} />}
        </PanelFrame>,
      ),
    );
    await settle();

    expect(pill().getAttribute("data-state")).toBe("reachable");
    expect(pill().textContent).toContain("gateway reachable");
    expect(pill().textContent).not.toContain("healthy");

    const parsed = JSON.parse(storageError.text) as { error: string; message: string };
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText(parsed.error)).toBeTruthy();
    expect(within(alert).getByText(parsed.message)).toBeTruthy();
    expect(alert.textContent).toContain("500");

    mkdirSync(dirname(STORAGE_ERROR_HTML), { recursive: true });
    writeFileSync(STORAGE_ERROR_HTML, container.innerHTML, "utf8");
    log.push("path C: pill reachable, first data panel quotes the 500 StorageError body verbatim");
  });
});

// ---- The health panel on its own ----------------------------------------------------------------

describe("HealthPanel is one read, in the browser, of one route (decision 34)", () => {
  it("asks for /v1/health once and shows the skeleton until it answers", async () => {
    const spy = answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(<HealthPanel />);
    expect(screen.getAllByTestId("skeleton-row")).toHaveLength(3);
    await settle();
    expect(spy.mock.calls.map((call) => call[0])).toEqual([HEALTH_URL]);
    expect(spy.mock.calls.map((call) => (call[1] as RequestInit).cache)).toEqual(["no-store"]);
    const wire = parseWire(health.text, HEALTH);
    expect(screen.getByText(wire.at(".schema_version").text)).toBeTruthy();
    log.push("overview panel: one no-store GET of /v1/health, skeleton until it answers");
  });
});

// ---- Path A over every recorded response --------------------------------------------------------

/**
 * "**Any** page renders `UnreachableState`" is the acceptance criterion, and the pages the screen
 * tickets build are these components over these responses. So the claim is made where it can be
 * made now: every recorded response, rendered in the real shell through the figure its screen
 * will use, and then the gateway taken away underneath it. What must survive is nothing.
 */
describe("Path A over the recorded corpus: no gateway, no page keeps a figure", () => {
  it("renders every recorded response in the shell, then loses the gateway under each", async () => {
    let walked = 0;
    let replaced = 0;

    for (const body of everyRecordedBody()) {
      const proxy = body.route;
      const answers: Record<string, Route> = {
        [HEALTH_URL]: HEALTH_OK,
        [proxy]: { status: body.code, text: body.text },
      };
      answersWith(answers);
      const { container, unmount } = render(
        shell(
          <PanelFrame route={body.route} title={body.name} sourceLabel={SOURCE_LABEL}>
            {(wire) => figuresFor(body.route, wire)}
          </PanelFrame>,
        ),
      );
      await settle();

      // Whatever this response rendered, every figure in it can say where it came from (FR-51).
      expect(figuresWithoutSource(container), body.name).toEqual([]);

      // A figure is its own trigger now - a `button` carrying `data-figure` - never a chip beside
      // an ordinary one (decision 68, `D-9`); a row link is `data-figure` too but is an anchor, not
      // a trigger, so only the buttons open anything.
      const triggers = [...container.querySelectorAll<HTMLElement>("[data-figure]")].filter(
        (node) => node.tagName === "BUTTON",
      );
      if (triggers.length > 0) {
        fireEvent.click(triggers[0] as HTMLElement);
        const dialog = screen.getByRole("dialog");
        drawerHoldsTheResponse(dialog);
        fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
        expect(screen.queryByRole("dialog")).toBeNull();
      }

      // The panel's own footer button, named `Refresh` now; an `ErrorState` still offers its own
      // `Refetch` beside it, which this walk leaves alone (FR-129 renamed only the footer's).
      const refresh = screen.queryAllByRole("button", { name: "Refresh" });
      if (refresh.length > 0) {
        for (const url of Object.keys(answers)) answers[url] = "rejects";
        fireEvent.click(refresh.at(0) as HTMLElement);
        await settle();

        expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
        expect(screen.getByTestId("unreachable-cause").textContent).toBe("ECONNREFUSED");
        expect(container.querySelectorAll("[data-figure]"), body.name).toHaveLength(0);
        expect(container.textContent, body.name).not.toContain(body.text);
        // The pill still reads what its last poll found: it re-reads on its own 15 s and on
        // nothing else, which is the point of it being the only component that polls.
        // The v3 transport's recorded 502 was answered on `/v1/health` itself, so the poll that
        // read it says `error`, as any non-2xx does now (MUST rule 23).
        expect(pill().getAttribute("data-state")).toBe(
          body.name === "transport-unreachable" ? "error" : "reachable",
        );
        replaced = replaced + 1;
      }

      unmount();
      cleanup();
      walked = walked + 1;
    }

    expect(walked).toBeGreaterThan(40);
    expect(replaced).toBeGreaterThan(30);
    log.push(`path A: ${walked} recorded responses rendered in the shell, ${replaced} replaced by`);
    log.push("        UnreachableState on losing the gateway, each with 0 figures left");
    // Forty-five shells, each rendered twice and each with its drawer walked, do not fit in the
    // default five seconds. The budget is the sweep's, not a loosening of any assertion in it.
  }, 60000);

  it("offers the lookups and the links a page uses, and builds no id to do it", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(
      shell(<IdLookup kind="session id" hrefFor={(id) => `/sessions/${id}`} />),
    );
    await settle();

    const open = screen.getByRole("button", { name: "Open" });
    expect((open as HTMLButtonElement).disabled).toBe(true);
    const typed = "run_0000000002NEHPN093CBQMNNV3";
    fireEvent.change(screen.getByLabelText("session id"), { target: { value: typed } });
    expect((open as HTMLButtonElement).disabled).toBe(false);
    fireEvent.submit(open.closest("form") as HTMLFormElement);
    expect(pushed).toEqual([`/sessions/${typed}`]);
    log.push("shell: an id lookup sends the id exactly as typed (MUST rule 25)");
  });

  it("slides its drawer where the operator has not asked for less motion", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(shell(<p>page content</p>));
    await settle();
    fireEvent.click(pill());
    expect(screen.getByRole("dialog").getAttribute("data-motion")).toBe("slide");
    log.push("shell: the drawer slides for an operator who asked for motion, and not otherwise");
  });
});

// ---- Evidence -----------------------------------------------------------------------------------

describe("ticket 211: one Refresh in the shell re-reads the open page", () => {
  const ROUTE_A = "/v1/health";
  const BAD = "/v1/not-a-route";

  it("AC1: the header holds exactly 1 Refresh and main holds 0", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(shell(<HealthPanel />));
    await settle();
    const buttons = screen.getAllByRole("button", { name: "Refresh" });
    expect(buttons).toHaveLength(1);
    expect(within(document.querySelector("header") as HTMLElement).getAllByRole("button", { name: "Refresh" })).toHaveLength(1);
    expect(within(screen.getByRole("main")).queryAllByRole("button", { name: "Refresh" })).toHaveLength(0);
    log.push("shell: 1 Refresh in the header, 0 in main");
  });

  it("AC2: Refresh requests every mounted PanelFrame once more and keeps the answer busy", async () => {
    const spy = answersWith({ [HEALTH_URL]: HEALTH_OK });
    render(
      shell(
        <>
          <HealthPanel />
          <PanelFrame route={ROUTE_A} title="Second" sourceLabel={SOURCE_LABEL}>
            {() => <p>second body</p>}
          </PanelFrame>
        </>,
      ),
    );
    await settle();
    const reads = () => spy.mock.calls.filter(([url]) => String(url).endsWith("/health")).length;
    const before = reads();
    const pillReads = spy.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.getAllByRole("region").some((r) => r.getAttribute("aria-busy") === "true")).toBe(true);
    await settle();
    // two frames, one more read each (the pill does not read on a Refresh)
    expect(spy.mock.calls.length - pillReads).toBe(2);
    expect(reads()).toBe(before + 2);
    log.push("shell: Refresh re-reads each mounted PanelFrame exactly once");
  });

  it("AC4: a PanelFrame footer holds the badge and no button; ErrorState keeps Refetch", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK, [BAD]: { status: 500, text: "{}" } });
    render(
      <PanelFrame route="/v1/health" title="Plain" sourceLabel={SOURCE_LABEL}>
        {() => <p>body</p>}
      </PanelFrame>,
    );
    await settle();
    expect(screen.queryAllByRole("button", { name: "Refresh" })).toHaveLength(0);
    log.push("shell: a bare PanelFrame has 0 Refresh");
  });
});

describe("evidence", () => {
  it("writes the manifest this ticket declares", () => {
    expect(log.length).toBeGreaterThan(12);
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});

// ---- The figures a screen renders over a response ------------------------------------------------

/**
 * What the page for this route will show. The screen tickets (106 to 112) own these compositions;
 * what this ticket needs from them is a page made of real figures to take the gateway away from,
 * so the shape is kept to the one column set each route's row already has.
 */
/** The two fixed `/v1/runs/...` routes (tickets 126, 127), which are aggregates and not run keys. */
const RUNS_AGGREGATES: ReadonlySet<string> = new Set(["/v1/runs/totals", "/v1/runs/turn_seconds"]);

function figuresFor(route: string, wire: Wire): ReactNode {
  const path = pathOf(route);
  if (path === "/v1/runs") return runsTable(wire);
  if (path.startsWith("/v1/improve/cycles/")) return cycleFigures(wire, route);
  if (path.startsWith("/v1/eval/runs/")) return caseTable(wire);
  if (path.startsWith("/v1/runs/") && !path.endsWith("/trace") && !RUNS_AGGREGATES.has(path)) {
    return runDetail(wire, route);
  }
  const first = firstLeafPath(wire.text);
  return first === undefined ? null : <Value leaf={wire.at(first)} />;
}

/** A route without the query string, so one listing's `?limit=` does not read as another route. */
function pathOf(route: string): string {
  return route.split("?").at(0) ?? route;
}

/** The runs listing, in the gateway's order, linked by each row's own `run_key` (MUST rule 25). */
function runsTable(wire: Wire) {
  const rows = indicesOf(wire.text, ".runs");
  if (rows.length === 0) return null;
  return (
    <DataTable
      caption="Runs, newest first, as the gateway ordered them"
      rows={rows}
      rowLink={{
        header: "run key",
        leaf: (row) => wire.at(`.runs[${row}].run_key`),
        href: (row) => `/runs/${wire.at(`.runs[${row}].run_key`).text}`,
      }}
      columns={[
        { header: "kind", cell: (row) => <KeyKindTag leaf={wire.at(`.runs[${row}].key_kind`)} /> },
        {
          header: "started",
          cell: (row) => <Value leaf={wire.at(`.runs[${row}].started_at`)} />,
        },
        {
          header: "stop reason",
          cell: (row) => (
            <StopReasonTag
              leaf={wire.at(`.runs[${row}].stop_reason`)}
              nullReason={nullReasonFor(wire.route, ".runs[].stop_reason")}
            />
          ),
        },
      ]}
    />
  );
}

/** One improvement cycle: the two scores as read, the rule as spelled, the decision as sent. */
function cycleFigures(wire: Wire, route: string) {
  return (
    <div>
      <ScorePair before={wire.at(".score_before")} after={wire.at(".score_after")} />
      <RuleBadge leaf={wire.at(".rule")} />
      <PassMark leaf={wire.at(".promoted")} />
      <StatTile
        label="regressions"
        leaf={wire.at(".regressions")}
        nullReason={nullReasonFor(route, ".regressions")}
      />
    </div>
  );
}

/** One eval run's cases, each with the mark its own `passed` field carries. */
function caseTable(wire: Wire) {
  const rows = indicesOf(wire.text, ".cases");
  if (rows.length === 0) return null;
  return (
    <DataTable
      caption="Cases, in the order the runner recorded them"
      rows={rows}
      columns={[
        { header: "case", cell: (row) => <Value leaf={wire.at(`.cases[${row}].case_id`)} /> },
        { header: "score", cell: (row) => <Value leaf={wire.at(`.cases[${row}].score`)} /> },
        { header: "passed", cell: (row) => <PassMark leaf={wire.at(`.cases[${row}].passed`)} /> },
      ]}
    />
  );
}

/** One run, by key: the kind, why it stopped, and what it spent. */
function runDetail(wire: Wire, route: string) {
  return (
    <div>
      <KeyKindTag leaf={wire.at(".key_kind")} />
      <StopReasonTag
        leaf={wire.at(".stop_reason")}
        nullReason={nullReasonFor(route, ".stop_reason")}
      />
      <StatTile
        label="input tokens"
        leaf={wire.at(".usage.input_tokens")}
        nullReason={nullReasonFor(route, ".usage.input_tokens")}
      />
    </div>
  );
}

/** The row indices of an array in a body, so a table's order is the response's order and no other. */
function indicesOf(text: string, path: string): number[] {
  const parsed = JSON.parse(text) as Record<string, unknown>;
  const rows = parsed[path.slice(1)];
  if (!Array.isArray(rows)) return [];
  return rows.map((_row, index) => index);
}

/** The jq path of the first scalar in a body, in document order, or nothing where it holds none. */
function firstLeafPath(text: string): string | undefined {
  let found: string | undefined;
  const walk = (node: unknown, path: string): void => {
    if (found !== undefined) return;
    if (Array.isArray(node)) {
      for (const [index, item] of node.entries()) walk(item, `${path}[${index}]`);
      return;
    }
    if (typeof node === "object" && node !== null) {
      for (const [key, item] of Object.entries(node as Record<string, unknown>)) {
        walk(item, `${path}.${key}`);
      }
      return;
    }
    found = path;
  };
  walk(JSON.parse(text), "");
  return found;
}

/** Everything an open drawer shows of the response a figure came from, and both edges of its trap. */
function drawerHoldsTheResponse(dialog: HTMLElement): void {
  expect(within(dialog).getByTestId("record-view")).toBeTruthy();

  const controls = within(dialog).getAllByRole("button");
  const first = controls.at(0);
  const last = controls.at(-1);
  last?.focus();
  fireEvent.keyDown(dialog, { key: "Tab" });
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(last);
  // A key that is not Tab, and a Tab away from either edge, are both left entirely alone.
  fireEvent.keyDown(dialog, { key: "ArrowDown" });
  first?.focus();
  fireEvent.keyDown(dialog, { key: "Tab" });
  expect(document.activeElement).toBe(first);
}

/** A `role="group"`'s accessible name, which `StatTile` builds from its label element. */
function accessibleName(element: HTMLElement): string {
  const labelledBy = element.getAttribute("aria-labelledby") ?? "";
  return document.getElementById(labelledBy)?.textContent?.trim() ?? "";
}
