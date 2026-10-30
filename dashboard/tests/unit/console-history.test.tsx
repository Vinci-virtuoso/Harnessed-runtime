// @vitest-environment jsdom
/**
 * FR-122, FR-128 (UJ-16, AC6): HISTORY rows are titled by the operator's own words, with the run
 * key, the ISO time and the stop word kept out of the row - reachable only through the title's own
 * provenance and the row's `Details` chip, never as row text (decision 66, MUST rule 25).
 *
 * `GET /v1/sessions?limit=` is unchanged in shape apart from gaining `title` (FR-48, decision 66);
 * `sessions.listing.body` is the recorded fixture, already carrying one titled run and four
 * untitled eval/improve sessions, so this suite reads it rather than writing a new one.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConsolePage from "../../app/console/page";
import { AppShell } from "../../components/AppShell";
import { COMPOSER_LABEL } from "../../components/ConsolePanel";
import { HEALTH_ROUTE } from "../../components/GatewayStatusPill";
import {
  CANNOT_CONTINUE,
  HISTORY_TITLE,
  RESUME,
  historyRoute,
  resumeHref,
} from "../../components/HistoryPanel";
import { messagesRoute } from "../../lib/control-client";
import { streamRoute } from "../../lib/stream";
import { SESSION_ID } from "../support/stream-drops";

import { pageAt, query } from "./support/query";

import type { ReactNode } from "react";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(DASHBOARD, "../evidence/188");

const log: string[] = [];

vi.mock("next/navigation", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  useSearchParams: (await import("./support/query")).useSearchParams,
  useRouter: () => ({ push: (href: string) => pushed.push(href), replace: (href: string) => pushed.push(href) }),
}));

let pushed: string[] = [];

type Answer = { status: number; text: string };

function recorded(name: string): Answer & { route: string } {
  const meta = JSON.parse(readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8")) as {
    route: string;
    status: number;
  };
  return { route: meta.route, status: meta.status, text: readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8") };
}

const health = recorded("health");
const listing = recorded("sessions.listing");
const LISTED = (
  JSON.parse(listing.text) as {
    sessions: { session_id: string; first_event: string; first_ts: string; terminal_event: string | null; title: string | null }[];
  }
).sessions;

const MESSAGES_ROUTE = `/v1/sessions/${SESSION_ID}/messages`;
const DECISIONS_ROUTE = `/v1/sessions/${SESSION_ID}/decisions`;
const STREAM_ROUTE = streamRoute(SESSION_ID);
const messages = recorded("sessions.messages");
const decisions = recorded("sessions.decisions");

let answers: Record<string, Answer | "rejects"> = {};
let asked: { url: string; method: string }[] = [];

function answer({ status, text }: Answer): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => null },
    text: async () => text,
  } as unknown as Response;
}

function world(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = String(input);
  const method = init?.method ?? "GET";
  asked.push({ url, method });
  if (url === STREAM_ROUTE) {
    return Promise.resolve(
      new Response(
        new ReadableStream<Uint8Array>({
          pull(controller) {
            controller.close();
          },
        }),
        { status: 200, headers: { "content-type": "text/event-stream" } },
      ),
    );
  }
  const next = answers[url];
  if (next === undefined) return Promise.reject(new Error(`no answer for ${url}`));
  if (next === "rejects") return Promise.reject(new Error("ECONNREFUSED"));
  return Promise.resolve(answer(next));
}

async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function flushed(): Promise<void> {
  for (let pass = 0; pass < 8; pass += 1) await settle();
}

function shell(children: ReactNode) {
  return <AppShell>{children}</AppShell>;
}

function toggle(): HTMLButtonElement {
  return screen.getByRole("button", { name: HISTORY_TITLE }) as HTMLButtonElement;
}

function history(): HTMLElement {
  return screen.getByRole("region", { name: HISTORY_TITLE });
}

function rowsOfHistory(): HTMLElement[] {
  return within(history()).queryAllByRole("listitem") as HTMLElement[];
}

function evidence(name: string, container: ParentNode): void {
  mkdirSync(EVIDENCE, { recursive: true });
  writeFileSync(resolve(EVIDENCE, name), (container as HTMLElement).innerHTML, "utf8");
}

async function opened(params: Record<string, string> = {}): Promise<HTMLElement> {
  const { container } = render(shell(pageAt(ConsolePage, params)));
  await flushed();
  fireEvent.click(toggle());
  await flushed();
  return container;
}

beforeEach(() => {
  vi.useFakeTimers();
  answers = { [HEALTH_ROUTE]: health };
  asked = [];
  pushed = [];
  query.current = {};
  vi.stubGlobal("matchMedia", (queryString: string) => ({ matches: false, media: queryString }));
  vi.stubGlobal("fetch", vi.fn(world));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

afterAll(() => {
  mkdirSync(EVIDENCE, { recursive: true });
  writeFileSync(resolve(EVIDENCE, "history.txt"), `${log.join("\n")}\n`, "utf8");
});

// ---- Collapsed by default --------------------------------------------------------------------------

describe("HISTORY is collapsed by default", () => {
  it("asks the gateway nothing about /v1/sessions until it is opened", async () => {
    render(shell(pageAt(ConsolePage)));
    await flushed();
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(asked.some((request) => request.url.startsWith("/v1/sessions"))).toBe(false);
    log.push("collapsed by default: 0 requests to /v1/sessions before the operator opens HISTORY");
  });
});

// ---- AC6: rows titled by the operator's words, 0 run keys/ISO times/stop words in the row --------

describe("AC6: each row is titled by the served title, with the run key, ISO time and stop word kept out of it", () => {
  it("titles every row by its served title, or its own null reason, in the gateway's order", async () => {
    answers[historyRoute("20")] = listing;
    const container = await opened();

    const rows = rowsOfHistory();
    expect(rows).toHaveLength(LISTED.length);
    const titled = rows.map((row) => row.querySelector('[data-figure]')?.textContent ?? "");
    expect(titled).toEqual(LISTED.map((row) => row.title ?? "this session never routed a message"));

    // 0 run keys, ISO timestamps or stop words as row text.
    const rowText = rows.map((row) => row.textContent ?? "").join(" ");
    for (const row of LISTED) {
      expect(rowText).not.toContain(row.session_id);
      expect(rowText).not.toContain(row.first_ts);
      if (row.terminal_event !== null) expect(rowText).not.toContain(row.terminal_event);
    }

    evidence("ac6-history-titles.html", container);
    log.push(`AC6: ${rows.length} rows titled by their served title; 0 run keys, ISO times or stop words in a row`);
  });

  it("shows the run key, the ISO time and the stop word only through the row's Details drawer", async () => {
    answers[historyRoute("20")] = listing;
    const container = await opened();

    const run = LISTED.find((row) => row.first_event === "run.started");
    if (run === undefined) throw new Error("the recorded listing holds no run.started row");
    const runRow = rowsOfHistory().find((row) => row.textContent?.includes(run.title ?? "")) as HTMLElement;
    fireEvent.click(within(runRow).getByRole("button", { name: "Source: Details" }));

    const drawer = screen.getByRole("dialog");
    expect(within(drawer).getByTestId("field-name").textContent).toBe("session_id");
    expect(within(drawer).getByTestId("exact-value").textContent).toBe(run.session_id);
    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));

    evidence("ac6-details-drawer.html", container);
    log.push("AC6: the run key reachable through the row's Details chip, opening the same provenance drawer");
  });

  it("reads 'cannot be continued' on an eval or improve row, and 'resume' on a run", async () => {
    answers[historyRoute("20")] = listing;
    const container = await opened();

    for (const [index, row] of rowsOfHistory().entries()) {
      const listed = LISTED[index];
      const link = within(row).queryByRole("link", { name: RESUME });
      if (listed?.first_event === "run.started") {
        expect(link?.getAttribute("href")).toBe(resumeHref(listed.session_id, "20"));
        expect(row.textContent).not.toContain(CANNOT_CONTINUE);
      } else {
        expect(link).toBeNull();
        expect(within(row).getByText(CANNOT_CONTINUE)).toBeTruthy();
      }
    }
    evidence("ac6-row-actions.html", container);
    log.push("AC6: resume links only run.started rows; every other row reads cannot be continued");
  });

  it("puts the picked id in ?session= verbatim, and nothing else changes the id", () => {
    expect(resumeHref(SESSION_ID, "20")).toBe(`/console?limit=20&session=${SESSION_ID}`);
    const opened = new URL(resumeHref("run_a b&c", "20"), "http://dashboard.invalid");
    expect(opened.pathname).toBe("/console");
    expect(opened.searchParams.get("session")).toBe("run_a b&c");
  });
});

// ---- Picking a row resumes it, reading the messages and decisions the new console reads ----------

describe("picking a run.started row resumes it: the console reads its messages and decisions", () => {
  it("opens the resumed session's conversation, and a follow-up posts to its messages route", async () => {
    answers[MESSAGES_ROUTE] = messages;
    answers[DECISIONS_ROUTE] = decisions;
    const { container } = render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();

    expect(screen.getAllByTestId("exchange")).toHaveLength(1);
    expect(asked.some((request) => request.url === MESSAGES_ROUTE)).toBe(true);
    expect(asked.some((request) => request.url === DECISIONS_ROUTE)).toBe(true);

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if ((init?.method ?? "GET") === "POST") {
          asked.push({ url, method: "POST" });
          return Promise.resolve(
            answer({ status: 202, text: JSON.stringify({ run: `/v1/runs/${SESSION_ID}`, session_id: SESSION_ID, stream: STREAM_ROUTE }) }),
          );
        }
        return world(input, init);
      }),
    );

    // Ticket 210: a resumed session starts with Sidechat closed; opening it is the follow-up's way in.
    fireEvent.click(screen.getByRole("button", { name: "Sidechat" }));
    const input = screen.getByRole("textbox", { name: COMPOSER_LABEL });
    fireEvent.change(input, { target: { value: "and again" } });
    fireEvent.click(screen.getByRole("button", { name: /^Send/ }));
    await settle();

    const post = asked.find((request) => request.method === "POST");
    expect(post?.url).toBe(messagesRoute(SESSION_ID));

    evidence("resume-conversation.html", container);
    log.push(`resume: 1 exchange from the recorded session; a follow-up posts to ${messagesRoute(SESSION_ID)}`);
  });
});

// ---- The limit control ------------------------------------------------------------------------------

describe("the 20/50/100 control", () => {
  it("pushes the console's URL with every other parameter kept, including an open session", async () => {
    answers[historyRoute("20")] = listing;
    answers[MESSAGES_ROUTE] = messages;
    answers[DECISIONS_ROUTE] = decisions;
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
    fireEvent.click(toggle());
    await flushed();

    const control = within(history()).getByTestId("limit-control");
    const radios = within(control).getAllByRole("radio") as HTMLInputElement[];
    expect(radios.map((radio) => radio.value)).toEqual(["20", "50", "100"]);
    fireEvent.click(radios[1] as HTMLInputElement);
    expect(pushed).toEqual([`/console?session=${SESSION_ID}&limit=50`]);
  });
});

// ---- HISTORY holds no rows of its own ---------------------------------------------------------------

describe("HISTORY holds no rows of its own", () => {
  it("finds 0 filter, sort or store over the listing in the HISTORY source", () => {
    const source = readFileSync(resolve(DASHBOARD, "components/HistoryPanel.tsx"), "utf8");
    expect(source).not.toMatch(/\.(filter|sort|reverse)\(/);
    expect(source).not.toMatch(/localStorage|sessionStorage|indexedDB|document\.cookie/);
  });
});
