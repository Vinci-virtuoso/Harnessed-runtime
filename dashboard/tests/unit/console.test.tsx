// @vitest-environment jsdom
/**
 * FR-122 (UJ-16, UJ-18, UJ-20, UJ-22), FR-128: the console is a conversation, and every reply
 * says who handled it and how sure the router was (ticket 188).
 *
 * `Conversation` renders one `Exchange` per served message from `GET /v1/sessions/{id}/messages`,
 * joined with `GET /v1/sessions/{id}/decisions` sliced per message - **0 message lists built from
 * events** (AC1). The three-message session below is **hand-written, not recorded** (decision 33
 * allows exactly this: no committed fixture holds a chain or an uncertain stop, and this ticket's
 * `verify` never runs the Python recorder), built field-for-field against the two recorded
 * fixtures' own shape (`sessions.messages.body`, `sessions.decisions.body`) so every field this
 * suite does not vary still matches what the store actually serves.
 *
 * The composer's send/refusal/follow-up lifecycle is `Composer`'s own (ticket 188 split it out of
 * `ConsolePanel`); it is exercised here through the whole page, since that is what an operator
 * actually drives.
 */
import { humanWord } from "../../lib/human-words";
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { relative, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConsolePage from "../../app/console/page";
import { AppShell } from "../../components/AppShell";
import { COMPOSER_LABEL, IN_FLIGHT_REASON, PLACEHOLDER, READING, SENDING } from "../../components/ConsolePanel";
import { HEALTH_ROUTE } from "../../components/GatewayStatusPill";
import { messagesRoute, RUNS_ROUTE } from "../../lib/control-client";
import { streamRoute } from "../../lib/stream";
import { PanelFrame } from "../../components/PanelFrame";
import { SESSION_ID } from "../support/stream-drops";
import { LINT_CASE_TIMEOUT_MS } from "../support/timeouts";

import { pageAt, query } from "./support/query";

import { useState, type ReactNode } from "react";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const REPO = resolve(DASHBOARD, "..");
const EVIDENCE = resolve(REPO, "evidence/188");

const log: string[] = [];

vi.mock("next/navigation", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  useSearchParams: (await import("./support/query")).useSearchParams,
  useRouter: () => ({ push: () => {}, replace: () => {} }),
}));

const MESSAGES_ROUTE = `/v1/sessions/${SESSION_ID}/messages`;
const DECISIONS_ROUTE = `/v1/sessions/${SESSION_ID}/decisions`;
const SPEND_ROUTE = `/v1/spend?session=${encodeURIComponent(SESSION_ID)}`;
const STREAM_ROUTE = streamRoute(SESSION_ID);
const PERMISSIONS_ROUTE = `/v1/permissions?session=${encodeURIComponent(SESSION_ID)}`;
const NO_PERMISSIONS: Answer = { status: 200, text: JSON.stringify({ permissions: [] }) };

/** The side rail's own read (ticket 189): one row per handler across the session, its total and
 * its API-equivalent figure - a plausible session spend, not tied to any one message's rows. */
const SESSION_SPEND = {
  handlers: [
    {
      handler: "groq",
      kind: "chat_model",
      billing: null,
      turns: "2",
      cost: "0.003121",
      api_equivalent: null,
      not_metered_turns: "0",
      reason: null,
    },
    {
      handler: "wireframe-designer",
      kind: "agent",
      billing: "openrouter",
      turns: "1",
      cost: "0.010000",
      api_equivalent: null,
      not_metered_turns: "0",
      reason: null,
    },
    {
      handler: "concierge",
      kind: "agent",
      billing: "oauth",
      turns: "1",
      cost: null,
      api_equivalent: null,
      not_metered_turns: "1",
      reason: null,
    },
    {
      handler: "router",
      kind: "router",
      billing: null,
      turns: "5",
      cost: "0.000111",
      api_equivalent: null,
      not_metered_turns: "0",
      reason: null,
    },
  ],
  total: "0.013232",
  currency: "USD",
  router_cost: "0.000111",
  router_decisions: "5",
  agent_cost: "0.010000",
  api_equivalent: "0.380000",
  not_metered_turns: "0",
  priced_turns: "3",
  prices_fetched_at: "2026-01-05T09:00:00Z",
  unknown_model_turns: "0",
  unknown_usage_turns: "0",
  unpriced_turns: "0",
  unpriced_models: [],
};
const SESSION_SPEND_BODY = JSON.stringify(SESSION_SPEND);

// ---- The hand-written three-message conversation (AC1 to AC4) ------------------------------------

const MESSAGE_1 = {
  message_seq: 1,
  first_seq: 10,
  last_seq: 16,
  text: "what did I decide about pricing last week?",
  text_reason: null,
  handler: "groq",
  outcome: "routed",
  reply: { output_kind: "text", text: "You set the plan at $42/month.", output_id: null },
  reply_reason: null,
  hops: [],
  steps: 1,
  seconds: "0.6",
  handlers: [
    {
      handler: "groq",
      kind: "chat_model",
      billing: null,
      turns: "1",
      cost: "0.003100",
      api_equivalent: null,
      not_metered_turns: "0",
      reason: null,
    },
    {
      handler: "router",
      kind: "router",
      billing: null,
      turns: "1",
      cost: "0.000021",
      api_equivalent: null,
      not_metered_turns: "0",
      reason: null,
    },
  ],
  created_at: "2026-01-05T09:00:00Z",
};

const DECISION_1 = {
  idx: 0,
  message_seq: 1,
  hop: 0,
  outcome: "routed",
  route: "groq_chat",
  handler: "groq",
  confidence: "0.9600",
  confidence_percent: "96%",
  floor: "0.5000",
  floor_percent: "50%",
  needs_memory: "0.9000",
  needs_memory_percent: "90%",
  memory_kind: "episodic",
  stop_reason: null,
  fallback_cause: null,
  state_truncated: false,
  latency_seconds: "0.412",
  cost: "0.000021",
  model: "typesafe/jev-1.13-20260917",
  created_at: "2026-01-05T09:00:00Z",
  options: [
    { question: "route", option: "groq_chat", description: null, probability: "0.9600", probability_percent: "96%" },
    { question: "route", option: "needs_human", description: null, probability: "0.0400", probability_percent: "4%" },
    { question: "memory_kind", option: "episodic", description: null, probability: "0.7000", probability_percent: "70%" },
  ],
};

const MESSAGE_2 = {
  message_seq: 2,
  first_seq: 20,
  last_seq: 40,
  text: "design a wireframe for X, then draft the launch email",
  text_reason: null,
  handler: "agent",
  outcome: "routed",
  reply: { output_kind: "text", text: "Launch email drafted and ready to send.", output_id: null },
  reply_reason: null,
  hops: [
    {
      hop: 0,
      outputs: [
        {
          output_id: "out_wireframe01",
          output_kind: "ui",
          media_type: "text/html",
          name: "wireframe.html",
          caption: null,
          size_bytes: 512,
          too_large: false,
        },
      ],
    },
    {
      hop: 1,
      outputs: [
        {
          output_id: "out_email01",
          output_kind: "text",
          media_type: "text/plain",
          name: null,
          caption: null,
          size_bytes: 128,
          too_large: false,
        },
      ],
    },
  ],
  steps: 5,
  seconds: "12.4",
  handlers: [
    {
      handler: "wireframe-designer",
      kind: "agent",
      billing: "openrouter",
      turns: "1",
      cost: "0.010000",
      api_equivalent: null,
      not_metered_turns: "0",
      reason: null,
    },
    {
      handler: "email-writer",
      kind: "agent",
      billing: "openrouter",
      turns: "1",
      cost: "0.005000",
      api_equivalent: null,
      not_metered_turns: "0",
      reason: null,
    },
    {
      handler: "router",
      kind: "router",
      billing: null,
      turns: "3",
      cost: "0.000060",
      api_equivalent: null,
      not_metered_turns: "0",
      reason: null,
    },
  ],
  created_at: "2026-01-05T09:05:00Z",
};

function chainDecision(idx: number, hop: number, route: string, percent: string) {
  return {
    idx,
    message_seq: 2,
    hop,
    outcome: "routed",
    route,
    handler: route === "done" ? null : "agent",
    confidence: `0.${percent.slice(0, -1)}00`,
    confidence_percent: percent,
    floor: "0.8000",
    floor_percent: "80%",
    needs_memory: "0.1000",
    needs_memory_percent: "10%",
    memory_kind: null,
    stop_reason: null,
    fallback_cause: null,
    state_truncated: false,
    latency_seconds: "0.5",
    cost: "0.000020",
    model: "typesafe/jev-1.13-20260917",
    created_at: "2026-01-05T09:05:00Z",
    options: [{ question: "route", option: route, description: null, probability: "0.9900", probability_percent: percent }],
  };
}

const DECISION_2A = chainDecision(1, 0, "wireframe-designer", "99%");
const DECISION_2B = chainDecision(2, 1, "email-writer", "98%");
const DECISION_2C = chainDecision(3, 2, "done", "97%");

const MESSAGE_3 = {
  message_seq: 3,
  first_seq: 50,
  last_seq: 52,
  text: "ping",
  text_reason: null,
  handler: null,
  outcome: "uncertain",
  reply: null,
  reply_reason: "uncertain stop: nothing ran",
  hops: [],
  steps: 0,
  seconds: "0.1",
  handlers: [
    {
      handler: "router",
      kind: "router",
      billing: null,
      turns: "1",
      cost: "0.000010",
      api_equivalent: null,
      not_metered_turns: "0",
      reason: null,
    },
  ],
  created_at: "2026-01-05T09:10:00Z",
};

const DECISION_3 = {
  idx: 4,
  message_seq: 3,
  hop: 0,
  outcome: "uncertain",
  route: null,
  handler: null,
  confidence: "0.4300",
  confidence_percent: "43%",
  floor: "0.5000",
  floor_percent: "50%",
  needs_memory: "0.2000",
  needs_memory_percent: "20%",
  memory_kind: null,
  stop_reason: "below_floor",
  fallback_cause: null,
  state_truncated: false,
  latency_seconds: "0.3",
  cost: "0.000010",
  model: "typesafe/jev-1.13-20260917",
  created_at: "2026-01-05T09:10:00Z",
  options: [
    { question: "route", option: "groq_chat", description: null, probability: "0.4300", probability_percent: "43%" },
    { question: "route", option: "claude-code", description: null, probability: "0.3000", probability_percent: "30%" },
    { question: "route", option: "needs_human", description: null, probability: "0.2700", probability_percent: "27%" },
  ],
};

const THREE_MESSAGES = JSON.stringify({ messages: [MESSAGE_1, MESSAGE_2, MESSAGE_3] });
const THREE_MESSAGES_DECISIONS = JSON.stringify({
  decisions: [DECISION_1, DECISION_2A, DECISION_2B, DECISION_2C, DECISION_3],
});

/** Ticket 197: a chain whose hop 1 stopped uncertain after grok ran; the served reply is grok's. */
const CHAIN_STOP_MESSAGE = {
  ...MESSAGE_1,
  message_seq: 5,
  text: "what is the current value of $QNT",
  handler: "grok",
  outcome: "uncertain",
  reply: { output_kind: "text", text: "QNT trades near $90 today.", output_id: null },
  reply_reason: null,
};
const CHAIN_STOP_DECISION_0 = { ...chainDecision(5, 0, "grok", "91%"), message_seq: 5 };
const CHAIN_STOP_DECISION_1 = {
  ...DECISION_3,
  idx: 6,
  message_seq: 5,
  hop: 1,
  route: "grok",
  floor_percent: "80%",
  options: [
    { question: "route", option: "groq_chat", description: null, probability: "0.0300", probability_percent: "3%" },
    { question: "route", option: "groq_memory", description: null, probability: "0.0000", probability_percent: "0%" },
    { question: "route", option: "grok", description: null, probability: "0.7500", probability_percent: "75%" },
    { question: "route", option: "done", description: null, probability: "0.2100", probability_percent: "21%" },
  ],
};
const CHAIN_STOP_MESSAGES = JSON.stringify({ messages: [CHAIN_STOP_MESSAGE] });
const CHAIN_STOP_DECISIONS = JSON.stringify({ decisions: [CHAIN_STOP_DECISION_0, CHAIN_STOP_DECISION_1] });

/** A fallback message, its own small conversation (AC4's second half - not part of the three above,
 * since a fallback decision carries no `route` and would not belong beside a routed chain). */
const FALLBACK_MESSAGE = {
  ...MESSAGE_1,
  message_seq: 1,
  outcome: "fallback",
  reply: { output_kind: "text", text: "Groq answered without the router.", output_id: null },
};
const FALLBACK_DECISION = {
  ...DECISION_1,
  outcome: "fallback",
  route: null,
  handler: "groq",
  confidence: null,
  confidence_percent: null,
  floor: null,
  floor_percent: null,
  fallback_cause: "timeout",
  options: [],
};
const FALLBACK_MESSAGES = JSON.stringify({ messages: [FALLBACK_MESSAGE] });
const FALLBACK_DECISIONS = JSON.stringify({ decisions: [FALLBACK_DECISION] });

/** MUST rule 27's own defensive fixture: a message with neither a turn nor an agent turn behind
 * it, and an outcome that is not `uncertain` - a state the store should never produce, proven here
 * to render `AbsentState` rather than a reply anyway. */
const NO_TURN_MESSAGE = { ...MESSAGE_1, reply: null, reply_reason: null };
const NO_TURN_MESSAGES = JSON.stringify({ messages: [NO_TURN_MESSAGE] });
const NO_TURN_DECISIONS = JSON.stringify({ decisions: [DECISION_1] });

const EMPTY_MESSAGES = JSON.stringify({ messages: [] });
const EMPTY_DECISIONS = JSON.stringify({ decisions: [] });

// ---- The scripted gateway and control app ---------------------------------------------------------

type Answer = { status: number; text: string };

const health: Answer = { status: 200, text: readFileSync(resolve(DASHBOARD, "tests/fixtures/gateway/health.body"), "utf8") };

let answers: Record<string, Answer | "rejects"> = {};
let posts: (Answer | "rejects" | Promise<Answer>)[] = [];
/** Routes whose read never answers (ticket 216: a read still pending). */
let pending = new Set<string>();
let asked: { url: string; method: string; body?: string; headers?: Record<string, string> }[] = [];
/** The controller of every stream connection opened so far, so a test can push a frame down them. */
let open: ReadableStreamDefaultController<Uint8Array>[] = [];
let ending: "close" | "hold" | "drop" = "hold";
/** Every `pull()` currently waiting on `ending` to change - `setEnding` wakes them all, so a
 * connection already open and idle reacts to a later `setEnding` rather than staying stuck on the
 * promise its own `pull()` returned when it was last asked (`ReadableStream` never calls `pull()`
 * again on its own while an earlier call's promise is still pending). */
let waiting: (() => void)[] = [];

function setEnding(next: "close" | "hold" | "drop"): void {
  ending = next;
  const resolvers = waiting;
  waiting = [];
  for (const resolve of resolvers) resolve();
}

function answer({ status, text }: Answer): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => null },
    text: async () => text,
  } as unknown as Response;
}

function sseResponse(): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      open.push(controller);
    },
    pull(controller) {
      if (ending === "close") {
        controller.close();
        return undefined;
      }
      if (ending === "drop") {
        controller.error(new Error("the connection was reset"));
        return undefined;
      }
      return new Promise<void>((resolve) => {
        waiting.push(resolve);
      });
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

function world(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = String(input);
  const method = init?.method ?? "GET";
  asked.push({
    url,
    method,
    body: typeof init?.body === "string" ? init.body : undefined,
    headers: init?.headers as Record<string, string> | undefined,
  });

  if (method === "POST") {
    const next = posts.shift();
    if (next === undefined) return Promise.reject(new Error(`no POST answer for ${url}`));
    if (next === "rejects") return Promise.reject(new Error("ECONNREFUSED"));
    return Promise.resolve(next).then(answer);
  }
  if (url === STREAM_ROUTE) return Promise.resolve(sseResponse());
  if (pending.has(url)) return new Promise<Response>(() => undefined);
  const next = answers[url];
  if (next === undefined) return Promise.reject(new Error(`no answer stubbed for ${url}`));
  if (next === "rejects") return Promise.reject(new Error("ECONNREFUSED"));
  return Promise.resolve(answer(next));
}

function answering(routes: Record<string, Answer | "rejects">): void {
  answers = { [HEALTH_ROUTE]: health, [PERMISSIONS_ROUTE]: NO_PERMISSIONS, ...routes };
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

function input(): HTMLTextAreaElement {
  return screen.getByRole("textbox", { name: COMPOSER_LABEL }) as HTMLTextAreaElement;
}

function sendButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: /^Send/ }) as HTMLButtonElement;
}

function type(text: string): void {
  fireEvent.change(input(), { target: { value: text } });
}

function evidence(name: string, container: ParentNode): void {
  mkdirSync(EVIDENCE, { recursive: true });
  writeFileSync(resolve(EVIDENCE, name), (container as HTMLElement).innerHTML, "utf8");
}

/** Open `/console?session=<id>`, every read answered (the side rail's spend read included), and
 * let every panel settle. */
async function opened(
  messages: string,
  decisions: string,
  spend: string = SESSION_SPEND_BODY,
): Promise<HTMLElement> {
  answering({
    [MESSAGES_ROUTE]: { status: 200, text: messages },
    [DECISIONS_ROUTE]: { status: 200, text: decisions },
    [SPEND_ROUTE]: { status: 200, text: spend },
  });
  ending = "close";
  const { container } = render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
  await flushed();
  return container;
}

beforeEach(() => {
  vi.useFakeTimers();
  answers = {};
  pending = new Set();
  posts = [];
  asked = [];
  ending = "hold";
  waiting = [];
  open = [];
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
  writeFileSync(resolve(EVIDENCE, "console.txt"), `${log.join("\n")}\n`, "utf8");
});

// ---- State 1: no session open ---------------------------------------------------------------------

describe("/console with no session open", () => {
  it("is the composer alone: no conversation, no session id, Send waits for text", async () => {
    answering({});
    const { container } = render(shell(<ConsolePage />));
    await flushed();

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Console");
    expect(input().placeholder).toBe(PLACEHOLDER);
    expect(input().disabled).toBe(false);
    expect(sendButton().disabled).toBe(true);
    type("say pong");
    expect(sendButton().disabled).toBe(false);
    expect(screen.queryByTestId("conversation")).toBeNull();
    expect(asked.filter((request) => request.url !== HEALTH_ROUTE)).toEqual([]);

    evidence("state-1-no-session.html", container);
    log.push("state 1: composer alone, Send disabled while empty; 0 requests before a send");
  });
});

// ---- AC1: a recorded three-message session renders three exchanges, 0 message lists from events --

describe("ticket 204: a Recent activity link lands on its own exchange", () => {
  const scroll = vi.fn();

  beforeEach(() => {
    scroll.mockClear();
    Element.prototype.scrollIntoView = scroll;
  });

  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("gives each exchange id=exchange-<message_seq> and tabIndex -1", async () => {
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    const exchanges = screen.getAllByTestId("exchange");
    expect(exchanges.map((exchange) => exchange.id)).toEqual(["exchange-1", "exchange-2", "exchange-3"]);
    expect(exchanges.map((exchange) => exchange.getAttribute("tabindex"))).toEqual(["-1", "-1", "-1"]);
  });

  it("focuses and scrolls the exchange the hash names, once", async () => {
    window.history.replaceState(null, "", "/#exchange-2");
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    const exchanges = screen.getAllByTestId("exchange");
    expect(document.activeElement).toBe(exchanges[1]);
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(scroll.mock.instances[0]).toBe(exchanges[1]);
    await flushed();
    expect(scroll).toHaveBeenCalledTimes(1);
  });

  it("focuses and scrolls nothing for a hash naming no exchange", async () => {
    window.history.replaceState(null, "", "/#exchange-99");
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    expect(scroll).not.toHaveBeenCalled();
    expect(screen.getAllByTestId("exchange")).not.toContain(document.activeElement);
  });

  it("does not crash on a hash with a malformed escape", async () => {
    window.history.replaceState(null, "", "/#%");
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    expect(screen.getAllByTestId("exchange")).toHaveLength(3);
    expect(scroll).not.toHaveBeenCalled();
  });

  it("styles only the :target exchange, with token names from tokens.css", () => {
    const css = readFileSync(resolve(DASHBOARD, "components/Exchange.module.css"), "utf8");
    const rule = /\.exchange:target\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).toMatch(/border-color:\s*var\(--color-accent\)/);
    expect(rule).toMatch(/background:\s*var\(--color-accent-subtle\)/);
    expect(rule).not.toMatch(/#[0-9a-f]{3,8}/i);
    const tokens = readFileSync(resolve(DASHBOARD, "app/tokens.css"), "utf8");
    expect(tokens).toContain("--color-accent:");
    expect(tokens).toContain("--color-accent-subtle:");
  });
});

describe("AC1: three exchanges, in served order, 0 message lists built from events", () => {
  it("renders one Exchange per served message, in the order the messages read sent them", async () => {
    const container = await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);

    const exchanges = screen.getAllByTestId("exchange");
    expect(exchanges).toHaveLength(3);
    expect(exchanges.map((exchange) => within(exchange).getByTestId("exchange-message").textContent)).toEqual([
      MESSAGE_1.text,
      MESSAGE_2.text,
      MESSAGE_3.text,
    ]);
    // The conversation reads exactly the messages route and the decisions route - never `/events`.
    expect(asked.some((request) => request.url === MESSAGES_ROUTE)).toBe(true);
    expect(asked.some((request) => request.url === DECISIONS_ROUTE)).toBe(true);
    expect(asked.some((request) => request.url.endsWith("/events"))).toBe(false);

    evidence("ac1-three-exchanges.html", container);
    log.push("AC1: 3 exchanges in served order; the conversation read 0 /events");
  });

  it("finds 0 message lists built from the events stream anywhere under Conversation.tsx", () => {
    const source = readFileSync(resolve(DASHBOARD, "components/Conversation.tsx"), "utf8");
    expect(source).not.toMatch(/\/events/);
    expect(source).not.toMatch(/streamRoute|readStream/);
  });
});

// ---- ticket 189, AC1: the reply caption's cost segment is CostLine, never $0.00/0/- --------------

describe("ticket 189 AC1: the reply caption's cost segment", () => {
  it("shows a chat turn's bare figure, and a chain's own last (non-router) handler's figure", async () => {
    const container = await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    const exchanges = screen.getAllByTestId("exchange");

    const groqCaption = within(exchanges[0] as HTMLElement).getByTestId("reply-caption");
    expect(groqCaption.textContent).toContain(MESSAGE_1.handlers[0].cost);
    expect(groqCaption.textContent).not.toContain("$0.00");

    // The chain's caption reads its own last non-router handler (email-writer, the one that
    // produced the reply) - never the router's own bucket, and never the first agent's figure.
    const chainCaption = within(exchanges[1] as HTMLElement).getByTestId("reply-caption");
    expect(chainCaption.textContent).toContain(MESSAGE_2.handlers[1].cost);
    expect(chainCaption.textContent).not.toContain(MESSAGE_2.handlers[2].cost);
    expect(chainCaption.textContent).not.toContain(MESSAGE_2.handlers[0].cost);

    evidence("ac1-cost-captions.html", container);
    log.push("ticket 189 AC1: reply caption cost segment reads CostLine - bare figures, 0 $0.00");
  });
});

// ---- ticket 189, AC2: a chain's wireframe renders as UiPreview, the email as the reply's TextView -

describe("ticket 189 AC2: the recorded chain's wireframe is a UiPreview, the reply a TextView", () => {
  it("renders the hop's ui output through UiPreview and the reply through TextView", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: THREE_MESSAGES },
      [DECISIONS_ROUTE]: { status: 200, text: THREE_MESSAGES_DECISIONS },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
      "/v1/outputs/out_wireframe01": { status: 200, text: "<p>wireframe</p>" },
    });
    ending = "close";
    const { container } = render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
    const chain = screen.getAllByTestId("exchange")[1] as HTMLElement;

    fireEvent.click(within(chain).getByRole("button", { name: /Steps/ }));
    // `UiPreview`'s own fetch of the output body settles on a microtask, never a timer, so the
    // fake-timer-aware `settle()` (this file's own helper, `console.test.tsx`'s established
    // pattern) flushes it rather than `waitFor`'s real-timer polling, which fake timers stall.
    await settle();
    const outputViews = within(chain).getAllByTestId("output-view");
    // The reply's own `OutputView` renders above the Steps disclosure (Exchange.tsx's own order),
    // so it is always the first one in this exchange, whatever else the opened disclosure adds.
    const reply = outputViews[0] as HTMLElement;
    const wireframeOutput = outputViews.find((node) => node.getAttribute("data-kind") === "ui");
    if (wireframeOutput === undefined) throw new Error("no ui output rendered under the chain");

    expect(within(wireframeOutput).getByTestId("ui-preview")).toBeTruthy();
    const iframe = within(wireframeOutput).getByTestId("ui-preview-frame") as HTMLIFrameElement;
    expect(iframe.getAttribute("sandbox")).toBe("");
    expect(iframe.srcdoc).toBe("<p>wireframe</p>");

    expect(reply.getAttribute("data-kind")).toBe("text");
    expect(within(reply).getByTestId("text-view").textContent).toContain(MESSAGE_2.reply.text);

    evidence("ac2-chain-kinds.html", container);
    log.push("ticket 189 AC2: the chain's wireframe is a UiPreview (sandboxed), the reply a TextView");
  });
});

// ---- AC2: a chain holds three route lines and its steps stay together ----------------------------

describe("AC2: a chain's exchange holds three route lines, its steps grouped under it alone", () => {
  it("renders three route lines under the chain's own exchange, bounded by its own served fields", async () => {
    const container = await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    const chain = screen.getAllByTestId("exchange")[1] as HTMLElement;

    const lines = within(chain).getAllByTestId("route-line");
    expect(lines).toHaveLength(3);
    expect(lines.map((line) => within(line).getByTestId("route-sentence").textContent)).toEqual([
      "Jev sent this to wireframe-designer",
      "then to email-writer",
      "then done",
    ]);

    // The steps disclosure is this exchange's own: opening it reads the message's own served
    // `steps` count, scoped to `message_seq = 2` by the store, never inferred.
    fireEvent.click(within(chain).getByRole("button", { name: /Steps/ }));
    const summary = within(chain).getByTestId("steps-summary");
    expect(summary.textContent).toContain(`${MESSAGE_2.steps} steps`);
    // The first exchange's own route line never leaks into the chain's list.
    expect(within(chain).queryByText(MESSAGE_1.text)).toBeNull();

    evidence("ac2-chain.html", container);
    log.push("AC2: 3 route lines under one exchange; its own served steps count, not the session's");
  });
});

// ---- Ticket 317: an exchange with no route says routing is off ------------------------------------

describe("ticket 317: an exchange recorded with routing off", () => {
  it("shows the served route_reason on RouteLine, never a fallback notice", async () => {
    const messages = readFileSync(resolve(DASHBOARD, "tests/fixtures/gateway/sessions.messages.routing-off.body"), "utf8");
    const container = await opened(messages, EMPTY_DECISIONS);
    const exchange = screen.getByTestId("exchange");
    const line = within(exchange).getByTestId("route-line");

    expect(within(line).getByTestId("route-sentence").textContent).toBe("routing is off");
    expect(within(exchange).queryByTestId("fallback-notice")).toBeNull();
    expect(within(exchange).queryByText(/did not answer/)).toBeNull();

    evidence("317-routing-off-exchange.html", container);
    log.push('317: route line reads the served route_reason "routing is off"');
  });
});

// ---- AC3: a routed Groq reply reads the exact sentence, and 0 browser arithmetic -----------------

describe("AC3: a routed Groq reply's route line and its Jev-percent source chip", () => {
  it('reads "Jev sent this to Groq (memory: past sessions)" with "Jev 96%" from the served percent', async () => {
    const container = await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    const exchange = screen.getAllByTestId("exchange")[0] as HTMLElement;
    const line = within(exchange).getByTestId("route-line");

    expect(within(line).getByTestId("route-sentence").textContent).toBe(
      "Jev sent this to Groq (memory: past sessions)",
    );
    const chip = within(line).getByRole("button", { name: "Source: Jev 96%" });
    expect(within(chip).getByText("Jev 96%")).toBeTruthy();

    evidence("ac3-groq-route-line.html", container);
    log.push('AC3: route line "Jev sent this to Groq (memory: past sessions)", chip "Jev 96%"');
  });

  // A cold type-aware ESLint start outruns vitest's 5 s default on slow hosts (ticket 228), so this case
  // declares the shared lint-case budget; the global testTimeout stays at the default.
  it("finds 0 no-derivation findings over RouteLine and Exchange (no browser arithmetic on probabilities)", { timeout: LINT_CASE_TIMEOUT_MS }, async () => {
    const { ESLint } = await import("eslint");
    const eslint = new ESLint({ cwd: DASHBOARD });
    const results = await eslint.lintFiles(["components/RouteLine.tsx", "components/Exchange.tsx"]);
    const findings = results.flatMap((result) =>
      result.messages.filter((message) => message.ruleId === "no-restricted-syntax"),
    );
    expect(findings).toEqual([]);
  });
});

// ---- AC4: the uncertain stop and the fallback notice ----------------------------------------------

describe("AC4: UncertainStop names the reason, the failed floor and the top routes as stored", () => {
  it('renders role="status", the reason, the floor and the stored route percentages', async () => {
    const container = await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    const exchange = screen.getAllByTestId("exchange")[2] as HTMLElement;

    const stop = within(exchange).getByTestId("uncertain-stop");
    expect(stop.getAttribute("role")).toBe("status");
    expect(within(stop).getByTestId("uncertain-reason").textContent).toBe("43%, below its 50% floor");
    const routes = within(stop).getByTestId("uncertain-routes").textContent ?? "";
    for (const option of DECISION_3.options) {
      expect(routes).toContain(option.probability_percent);
    }
    // No reply is rendered in an uncertain exchange's place.
    expect(within(exchange).queryByTestId("output-view")).toBeNull();

    evidence("ac4-uncertain-stop.html", container);
    log.push("AC4: UncertainStop role=status, reason + floor + stored route percentages");
  });

  it("ticket 284: the route options read needs_human and done as words, the exact word in the tooltip", async () => {
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    const exchange = screen.getAllByTestId("exchange")[2] as HTMLElement;
    const routes = within(exchange).getByTestId("uncertain-routes");
    const text = routes.textContent ?? "";
    expect(text).toContain("Needs you");
    expect(text).not.toContain("needs_human");
    expect(humanWord("route_option", "done")).toBe("Finished");
    expect(humanWord("route_option", "claude-code")).toBeUndefined();
    expect(text).toContain("claude-code");
    expect(routes.querySelector('[title$=", needs_human"]')).not.toBeNull();
  });

  it("hop 0 stop still says nothing ran", async () => {
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    const exchange = screen.getAllByTestId("exchange")[2] as HTMLElement;
    expect(exchange.textContent).toContain("Not sure enough to act.");
    expect(exchange.textContent).toContain("Nothing ran.");
    expect(within(exchange).queryByTestId("output-view")).toBeNull();
    // Ticket 212: the served null has its documented reason; it is not a 404.
    const text = exchange.textContent ?? "";
    expect(text).not.toContain("Not found");
    expect(text).not.toContain("404");
    expect(text).not.toContain("NoReply");
  });

  it("ticket 197: an uncertain hop after a step ran shows the reply beside Stopped here.", async () => {
    await opened(CHAIN_STOP_MESSAGES, CHAIN_STOP_DECISIONS);
    const exchange = screen.getByTestId("exchange");
    expect(within(exchange).getByTestId("output-view").textContent).toContain("QNT trades near $90 today.");
    const stop = within(exchange).getByTestId("uncertain-stop");
    expect(stop.textContent).toContain("Stopped here.");
    expect(stop.textContent).toContain("The last step's output is the reply.");
    expect(stop.textContent).toContain("80%");
    expect(exchange.textContent).not.toContain("Nothing ran.");
    const routes = within(stop).getByTestId("uncertain-routes").textContent ?? "";
    expect(routes).toContain("Groq 3%");
    expect(routes).toContain("Groq with memory 0%");
    expect(routes.indexOf("Groq with memory")).toBeGreaterThan(routes.indexOf("Groq 3%"));
  });

  it('renders FallbackNotice reading "The router did not answer (timeout), so Groq replied directly." above the reply', async () => {
    const container = await opened(FALLBACK_MESSAGES, FALLBACK_DECISIONS);
    const exchange = screen.getByTestId("exchange");

    const notice = within(exchange).getByTestId("fallback-notice");
    expect(notice.getAttribute("role")).toBe("status");
    expect(notice.textContent).toBe("The router did not answer (timeout), so Groq replied directly.");
    expect(within(exchange).getByTestId("output-view").textContent).toContain(FALLBACK_MESSAGE.reply.text);
    // The notice sits above the reply in the DOM.
    const output = within(exchange).getByTestId("output-view");
    expect(notice.compareDocumentPosition(output) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    evidence("ac4-fallback.html", container);
    log.push("AC4: FallbackNotice above a normal reply, cause read verbatim");
  });
});

// ---- AC5: one string per send, and AbsentState for a reply with no turn behind it ------------------

describe("AC5: a send is one string, and a reply with no turn or agent turn renders AbsentState", () => {
  it("sends exactly one string under the key prompt", async () => {
    answering({});
    posts = [{ status: 202, text: JSON.stringify({ run: "/v1/runs/x", session_id: "run_x", stream: streamRoute("run_x") }) }];
    render(shell(<ConsolePage />));
    await flushed();
    type("say pong");
    fireEvent.click(sendButton());
    await settle();

    const post = asked.find((request) => request.method === "POST");
    expect(post?.url).toBe(RUNS_ROUTE);
    expect(JSON.parse(post?.body ?? "")).toEqual({ prompt: "say pong" });
  });

  it("ticket 219: a failed delegation is a plain reply state, never a 404", async () => {
    const failed = {
      ...MESSAGE_1,
      reply: null,
      reply_reason: "the delegation to claude-code timed_out",
    };
    await opened(JSON.stringify({ messages: [failed] }), NO_TURN_DECISIONS);
    const exchange = screen.getByTestId("exchange");
    const absent = within(exchange).getByTestId("reply-absent");
    expect(absent.getAttribute("role")).toBe("status");
    expect(absent.textContent).toContain("No reply.");
    expect(absent.textContent).toContain("claude-code");
    expect(absent.textContent).toContain("Timed out");
    const text = exchange.textContent ?? "";
    expect(text).not.toContain("Not found");
    expect(text).not.toContain("404");
    expect(text).not.toContain("NoReply");
    expect(within(exchange).queryByRole("alert")).toBeNull();
  });

  it("ticket 219: a served raise reason is the same plain reply state", async () => {
    const raised = { ...MESSAGE_1, reply: null, reply_reason: "the run raised ProviderError" };
    await opened(JSON.stringify({ messages: [raised] }), NO_TURN_DECISIONS);
    const absent = within(screen.getByTestId("exchange")).getByTestId("reply-absent");
    expect(absent.textContent).toContain("No reply.");
    expect(absent.textContent).toContain("the run raised ProviderError");
  });

  it("renders AbsentState, never a reply, for a message with neither a turn nor an agent turn behind it", async () => {
    const container = await opened(NO_TURN_MESSAGES, NO_TURN_DECISIONS);
    const exchange = screen.getByTestId("exchange");

    expect(within(exchange).queryByTestId("output-view")).toBeNull();
    const alert = within(exchange).getByRole("alert");
    expect(alert.textContent).toContain("NoReply");

    evidence("ac5-absent-state.html", container);
    log.push("AC5: 0 turns/agent turns behind a reply -> AbsentState, never a reply");
  });
});

// ---- AC7: the read this ticket adds --------------------------------------------------------------

describe("AC7: the console's own reads", () => {
  it("reads the session's messages and its decisions, and posts one control write to open a run", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: EMPTY_MESSAGES },
      [DECISIONS_ROUTE]: { status: 200, text: EMPTY_DECISIONS },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    posts = [{ status: 202, text: JSON.stringify({ run: `/v1/runs/${SESSION_ID}`, session_id: SESSION_ID, stream: STREAM_ROUTE }) }];
    render(shell(<ConsolePage />));
    await flushed();
    type("say pong");
    fireEvent.click(sendButton());
    await flushed();

    expect(asked.some((request) => request.url === MESSAGES_ROUTE)).toBe(true);
    expect(asked.some((request) => request.url === DECISIONS_ROUTE)).toBe(true);
    expect(asked.filter((request) => request.method === "POST")).toHaveLength(1);
  });
});

// ---- The in-flight composer, refusals, and a follow-up --------------------------------------------

describe("a send opens a run: the composer disables, then re-enables once the run is at rest", () => {
  it('disables the composer with "Harnessed is working" while in flight, showing "Live" in the reply\'s place, then re-enables', async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [{ ...MESSAGE_1, last_seq: null, reply: null, reply_reason: null }] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    ending = "hold";
    let accept: (value: Answer) => void = () => {};
    posts = [new Promise<Answer>((resolved) => (accept = resolved))];
    const { container } = render(shell(<ConsolePage />));
    await flushed();
    type("say pong");
    fireEvent.click(sendButton());
    await settle();
    expect(sendButton().textContent).toBe(SENDING);

    accept({ status: 202, text: JSON.stringify({ run: `/v1/runs/${SESSION_ID}`, session_id: SESSION_ID, stream: STREAM_ROUTE }) });
    await flushed();

    expect(input().disabled).toBe(true);
    const reason = document.getElementById(input().getAttribute("aria-describedby") ?? "");
    expect(reason?.textContent).toBe(IN_FLIGHT_REASON);
    expect(screen.getByTestId("exchange-live").textContent).toBe("Live");
    expect(screen.queryByTestId("output-view")).toBeNull();
    evidence("state-in-flight.html", container);

    setEnding("close");
    await flushed();
    expect(input().disabled).toBe(false);
    log.push('in flight: composer disabled ("Harnessed is working"), newest exchange reads "Live"');
  });

  it("quotes a 409 beneath the input, verbatim, and keeps the typed text", async () => {
    answering({});
    const inFlight = JSON.stringify({
      error: "RunInFlight",
      message: `a run is already in flight: session id ${SESSION_ID}. The control app runs one at a time; wait for it to finish, then try again.`,
    });
    posts = [{ status: 409, text: inFlight }];
    render(shell(<ConsolePage />));
    await flushed();
    type("say pong again");
    fireEvent.click(sendButton());
    await flushed();

    const strip = screen.getByRole("alert");
    expect(strip.textContent).toContain("409");
    expect(strip.textContent).toContain("RunInFlight");
    expect(input().value).toBe("say pong again");
    expect(input().disabled).toBe(false);
  });

  it("says a control app that never answered is unreachable, and keeps the text", async () => {
    answering({});
    posts = ["rejects"];
    render(shell(<ConsolePage />));
    await flushed();
    type("anyone there");
    fireEvent.keyDown(input(), { key: "Enter" });
    await flushed();

    expect(screen.getByTestId("unreachable-cause").textContent).toBe("ECONNREFUSED");
    expect(input().value).toBe("anyone there");
  });

  it("continues an open session on the messages route with one more string", async () => {
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    posts = [{ status: 202, text: JSON.stringify({ run: `/v1/runs/${SESSION_ID}`, session_id: SESSION_ID, stream: STREAM_ROUTE }) }];
    fireEvent.click(sidechatButton());
    type("and again");
    fireEvent.click(sendButton());
    await settle();

    const post = asked.find((request) => request.method === "POST");
    expect(post?.url).toBe(messagesRoute(SESSION_ID));
    expect(JSON.parse(post?.body ?? "")).toEqual({ prompt: "and again" });
    expect(Object.keys(JSON.parse(post?.body ?? ""))).toEqual(["prompt"]);
  });
});

// ---- ticket 210: Sidechat continues a conversation in place, and the console has no Refresh ------

function sidechatButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Sidechat" }) as HTMLButtonElement;
}

const ACCEPTED = () => ({
  status: 202,
  text: JSON.stringify({ run: `/v1/runs/${SESSION_ID}`, session_id: SESSION_ID, stream: STREAM_ROUTE }),
});

describe("ticket 210: Sidechat", () => {
  it("AC1: a finished conversation has 0 Refresh, 1 Sidechat after the last exchange, 0 composers", async () => {
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);

    expect(within(screen.getByRole("main")).queryAllByRole("button", { name: "Refresh" })).toHaveLength(0);
    expect(screen.queryAllByRole("textbox", { name: COMPOSER_LABEL })).toHaveLength(0);
    expect(screen.getAllByRole("button", { name: "Sidechat" })).toHaveLength(1);
    const block = screen.getByTestId("sidechat");
    const list = screen.getByTestId("conversation");
    expect(within(block).getByRole("button", { name: "Sidechat" })).toBeTruthy();
    expect(list.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Ticket 216: the Sidechat sits below the panel, outside its body.
    expect(list.contains(block)).toBe(false);
    expect(block.closest("section")?.querySelector("h2")?.textContent).not.toBe("Conversation");
    // The "Updated N min ago" badges stay.
    expect(screen.getAllByText(/^Updated/).length).toBeGreaterThan(0);
  });

  // Slow by nature: renders a conversation and drives a send; 3.5 s on the operator's PC.
  it("AC2: clicking Sidechat renders one composer in its place; a send posts one string", { timeout: 15_000 }, async () => {
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    posts = [ACCEPTED()];
    fireEvent.click(sidechatButton());

    expect(screen.queryByRole("button", { name: "Sidechat" })).toBeNull();
    expect(screen.getAllByRole("textbox", { name: COMPOSER_LABEL })).toHaveLength(1);
    expect(within(screen.getByTestId("sidechat")).getByRole("textbox", { name: COMPOSER_LABEL })).toBeTruthy();

    type("and again");
    fireEvent.click(sendButton());
    await settle();
    const post = asked.find((request) => request.method === "POST");
    expect(post?.url).toBe(messagesRoute(SESSION_ID));
    expect(JSON.parse(post?.body ?? "")).toEqual({ prompt: "and again" });
  });

  // Slow by nature: drives a send through a full live stream to rest; 4.3 s on the operator's PC.
  it("AC3: an accepted send keeps Sidechat open, disabled while live, re-enabled at rest, session unchanged", { timeout: 15_000 }, async () => {
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    posts = [ACCEPTED()];
    fireEvent.click(sidechatButton());
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [{ ...MESSAGE_1, last_seq: null, reply: null, reply_reason: null }] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    ending = "hold";
    type("and again");
    fireEvent.click(sendButton());
    await flushed();

    expect(screen.getAllByRole("textbox", { name: COMPOSER_LABEL })).toHaveLength(1);
    expect(input().disabled).toBe(true);
    const reason = document.getElementById(input().getAttribute("aria-describedby") ?? "");
    expect(reason?.textContent).toBe(IN_FLIGHT_REASON);
    expect(screen.queryByRole("button", { name: "Sidechat" })).toBeNull();

    answering({
      [MESSAGES_ROUTE]: { status: 200, text: THREE_MESSAGES },
      [DECISIONS_ROUTE]: { status: 200, text: THREE_MESSAGES_DECISIONS },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    setEnding("close");
    await flushed();
    expect(input().disabled).toBe(false);
    expect(screen.getAllByTestId("exchange").length).toBe(3);
    expect(screen.getByTestId("sidechat").contains(input())).toBe(true);
    expect(asked.find((request) => request.method === "POST")?.url).toBe(messagesRoute(SESSION_ID));
    expect(asked.filter((request) => request.url === SPEND_ROUTE).length).toBeGreaterThan(0);
    expect(within(screen.getByRole("main")).queryAllByRole("button", { name: "Refresh" })).toHaveLength(0);
  });

  it("AC4: no session: one composer starts a session, then Sidechat is open and the bottom composer is gone", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [MESSAGE_1] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    posts = [ACCEPTED()];
    render(shell(<ConsolePage />));
    await flushed();
    expect(screen.getAllByRole("textbox", { name: COMPOSER_LABEL })).toHaveLength(1);
    expect(screen.queryByTestId("sidechat")).toBeNull();
    type("say pong");
    fireEvent.click(sendButton());
    await flushed();

    expect(asked.find((request) => request.method === "POST")?.url).toBe(RUNS_ROUTE);
    expect(screen.getAllByRole("textbox", { name: COMPOSER_LABEL })).toHaveLength(1);
    expect(screen.getByTestId("sidechat").contains(input())).toBe(true);
    expect(screen.queryByRole("button", { name: "Sidechat" })).toBeNull();
  });

  it("AC5: a run that raised renders Sidechat under the raise", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [ONLY_LIVE] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    ending = "hold";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
    pushFrame(1, "error.raised", '{"class":"Boom","message":"it broke","stop_reason":"error"}');
    await flushed();

    expect(screen.getByRole("alert").textContent).toContain("Boom");
    expect(screen.getAllByRole("button", { name: "Sidechat" })).toHaveLength(1);
    fireEvent.click(sidechatButton());
    expect(screen.getAllByRole("textbox", { name: COMPOSER_LABEL })).toHaveLength(1);
  });
});

// ---- ticket 216: the Sidechat is present while the conversation panel is loading or errored ------

describe("ticket 216: Sidechat outside the panel body", () => {
  const sidechats = () => screen.getAllByTestId("sidechat");
  const ok = (messages: string, decisions: string = EMPTY_DECISIONS) => ({
    [MESSAGES_ROUTE]: { status: 200, text: messages },
    [DECISIONS_ROUTE]: { status: 200, text: decisions },
    [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
  });
  const atSession = async () => {
    ending = "close";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
  };

  it("AC1: a pending read shows 1 Sidechat below the panel, Send disabled, 'Reading this session', 0 posts", async () => {
    answering(ok(THREE_MESSAGES, THREE_MESSAGES_DECISIONS));
    pending = new Set([MESSAGES_ROUTE]);
    await atSession();

    expect(sidechats()).toHaveLength(1);
    const status = within(screen.getByTestId("sidechat")).getByRole("status");
    expect(status.textContent).toBe(READING);
    expect(input().disabled).toBe(true);
    expect(sendButton().disabled).toBe(true);
    fireEvent.keyDown(input(), { key: "Enter" });
    fireEvent.click(sendButton());
    await flushed();
    expect(asked.filter((request) => request.method === "POST")).toHaveLength(0);
  });

  it.each([
    ["500", { status: 500, text: JSON.stringify({ error: "Boom", message: "it broke" }) }],
    ["unreachable", "rejects" as const],
  ])("AC2: a %s messages read still leaves 1 enabled Sidechat that posts one string", async (_name, answer) => {
    answering({ ...ok(THREE_MESSAGES, THREE_MESSAGES_DECISIONS), [MESSAGES_ROUTE]: answer });
    posts = [ACCEPTED()];
    await atSession();

    expect(sidechats()).toHaveLength(1);
    expect(within(screen.getByTestId("sidechat")).queryByRole("status")).toBeNull();
    expect(sendButton().disabled).toBe(true); // empty text
    type("still here");
    expect(sendButton().disabled).toBe(false);
    fireEvent.click(sendButton());
    await settle();
    const post = asked.find((request) => request.method === "POST");
    expect(post?.url).toBe(messagesRoute(SESSION_ID));
    expect(JSON.parse(post?.body ?? "")).toEqual({ prompt: "still here" });
  });

  it("AC2: a failing post shows the composer's own error", async () => {
    answering({ ...ok(THREE_MESSAGES, THREE_MESSAGES_DECISIONS), [MESSAGES_ROUTE]: { status: 500, text: "{}" } });
    posts = [{ status: 409, text: JSON.stringify({ error: "RunInFlight", message: "busy" }) }];
    await atSession();
    type("hello");
    fireEvent.click(sendButton());
    await flushed();
    expect(within(screen.getByTestId("sidechat")).getByRole("alert").textContent).toContain("RunInFlight");
  });

  it("AC3: zero rows leaves 1 enabled Sidechat after the panel's empty text", async () => {
    answering(ok(EMPTY_MESSAGES, EMPTY_DECISIONS));
    await atSession();

    expect(sidechats()).toHaveLength(1);
    expect(screen.queryByTestId("conversation")).toBeNull();
    expect(within(screen.getByTestId("sidechat")).queryByRole("status")).toBeNull();
    type("first words");
    expect(sendButton().disabled).toBe(false);
  });

  it("AC4: a served conversation has exactly 1 Sidechat, after the last exchange in document order", async () => {
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);

    expect(sidechats()).toHaveLength(1);
    const block = screen.getByTestId("sidechat");
    const exchanges = screen.getAllByTestId("exchange");
    const last = exchanges[exchanges.length - 1] as HTMLElement;
    expect(last.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByTestId("conversation").contains(block)).toBe(false);
  });

  it("AC5: after an accepted send the new pending read reads 'Harnessed is working', never 'Reading this session'", async () => {
    await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);
    posts = [ACCEPTED()];
    fireEvent.click(sidechatButton());
    pending = new Set([MESSAGES_ROUTE]);
    ending = "hold";
    type("and again");
    fireEvent.click(sendButton());
    await flushed();

    expect(sidechats()).toHaveLength(1);
    expect(input().disabled).toBe(true);
    expect(within(screen.getByTestId("sidechat")).getByRole("status").textContent).toBe(IN_FLIGHT_REASON);
    expect(screen.queryByText(READING)).toBeNull();
  });
});

// ---- ticket 189: the side rail's "This session" CostLine ------------------------------------------

describe('ticket 189: the side rail\'s "This session" reads /v1/spend?session= as a CostLine list', () => {
  it("lists one row per handler, the session total, and the API-equivalent row", async () => {
    const container = await opened(THREE_MESSAGES, THREE_MESSAGES_DECISIONS);

    expect(asked.some((request) => request.url === SPEND_ROUTE)).toBe(true);
    const rail = screen.getByTestId("cost-line-rows");
    const chips = within(rail).getAllByRole("button", { name: /^Source: / });
    expect(chips.map((chip) => chip.textContent)).toEqual(
      SESSION_SPEND.handlers.map((handler) => handler.handler),
    );
    // The subscription row with no reported figure reads "not metered" - never $0.00, 0 or -.
    expect(within(rail).getByText("not metered")).toBeTruthy();
    expect(rail.textContent).not.toContain("$0.00");
    expect(rail.textContent).toContain(SESSION_SPEND.total);
    expect(rail.textContent).toContain("API-equivalent, not in the total");
    expect(rail.textContent).toContain(SESSION_SPEND.api_equivalent);

    evidence("side-rail-cost-line.html", container);
    log.push('ticket 189: side rail "This session" - one row per handler, total, API-equivalent row');
  });
});

// ---- The scan: no token-by-token streaming, 0 message lists from events (MUST rule 26) -----------

function sources(): string[] {
  const found: string[] = [];
  const walk = (dir: string): void => {
    for (const name of readdirSync(dir)) {
      const path = resolve(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.tsx?$/.test(name)) found.push(path);
    }
  };
  for (const root of ["app", "components", "lib"]) walk(resolve(DASHBOARD, root));
  return found;
}

describe("token-by-token streaming does not exist, and the console sends one string", () => {
  it("finds 0 code reading partial content from a frame outside the named readers", () => {
    const readers: string[] = [];
    const deltas: string[] = [];
    for (const path of sources()) {
      const text = readFileSync(path, "utf8");
      const name = relative(DASHBOARD, path).replaceAll("\\", "/");
      if (/(?<!styles)\.(data|payload|paths)\b/.test(text)) readers.push(name);
      if (/\b(text_delta|content_block_delta)\b|\.delta\b/.test(text)) deltas.push(name);
    }
    expect(readers.sort()).toEqual([
      "app/traces/ReplayPanel.tsx",
      "components/ConsolePanel.tsx",
      "components/TraceStep.tsx",
      "components/diagram/status.ts",
      "lib/stream.ts",
    ]);
    expect(deltas).toEqual([]);
    log.push(`scan: ${sources().length} source files; frame content read only by ${readers.join(", ")}; 0 delta readers`);
  });

  it("finds 0 role-tagged message arrays and 0 prior replies concatenated into a prompt", () => {
    const roleTagged: string[] = [];
    const concatenated: string[] = [];
    for (const path of sources()) {
      const text = readFileSync(path, "utf8");
      const name = relative(DASHBOARD, path).replaceAll("\\", "/");
      if (/\brole\s*:\s*["'`](user|assistant|system|tool)["'`]/.test(text)) roleTagged.push(name);
      if (/\bmessages\s*:\s*\[/.test(text)) roleTagged.push(name);
      if (/(startRun|sendMessage)\([^)]*\b(reply|history|join|concat)\b/.test(text)) concatenated.push(name);
      if (/[{,]\s*prompt\s*:\s*(`|\[|[^,;)\n]*\+)/.test(text)) concatenated.push(name);
    }
    expect(roleTagged).toEqual([]);
    expect(concatenated).toEqual([]);

    const composer = readFileSync(resolve(DASHBOARD, "components/Composer.tsx"), "utf8");
    expect(composer.match(/\b(startRun|sendMessage)\([^)]*\)/g)).toEqual(["startRun(text)", "sendMessage(session, text)"]);
    log.push(`scan: 0 role-tagged arrays; 0 prompt concatenations; Composer sends only startRun(text)/sendMessage(session, text)`);
  });
});

// ---- ticket 200: the live Steps are this message's own, and the reply lands by itself -------------

const LIVE_SECOND = { ...MESSAGE_2, last_seq: null, reply: null, reply_reason: null };
const ONLY_LIVE = { ...MESSAGE_1, last_seq: null, reply: null, reply_reason: null };

function pushFrame(seq: number, name: string, data = "{}"): void {
  const frame = `id: ${seq}\nevent: ${name}\ndata: ${data}\n\n`;
  for (const controller of open) controller.enqueue(new TextEncoder().encode(frame));
}

function messageReads(): number {
  return asked.filter((request) => request.url === MESSAGES_ROUTE).length;
}

function streamRequests() {
  return asked.filter((request) => request.url === STREAM_ROUTE);
}

describe("ticket 200 AC1/AC2: the live exchange's Steps is its own stream, at the previous served last_seq", () => {
  it("holds 0 Replay headings and 0 replay-log, and opens the stream at message 1's last_seq", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [MESSAGE_1, LIVE_SECOND] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    ending = "hold";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();

    expect(screen.queryAllByRole("heading", { name: "Replay" })).toHaveLength(0);
    expect(screen.queryAllByTestId("replay-log")).toHaveLength(0);
    const live = screen.getAllByTestId("exchange").at(-1) as HTMLElement;
    expect(within(live).getByRole("region", { name: "Session stream" })).toBeTruthy();
    const requests = streamRequests();
    expect(requests.length).toBeGreaterThan(0);
    for (const request of requests) expect(request.headers?.["Last-Event-ID"]).toBe(String(MESSAGE_1.last_seq));
  });

  it("sends no Last-Event-ID when the only message is live", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [ONLY_LIVE] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    ending = "hold";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();

    const requests = streamRequests();
    expect(requests.length).toBeGreaterThan(0);
    for (const request of requests) expect(request.headers?.["Last-Event-ID"]).toBeUndefined();
  });
});

describe("ticket 200 AC3/AC4: the reply lands at the close, and only some frames re-read", () => {
  it("renders the reply and drops exchange-live once the stream closes, with no Refresh click", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [ONLY_LIVE] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    ending = "hold";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
    expect(screen.getByTestId("exchange-live")).toBeTruthy();
    fireEvent.click(sidechatButton());
    expect(input().disabled).toBe(true);

    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [MESSAGE_1] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    setEnding("close");
    await flushed();

    expect(screen.queryByTestId("exchange-live")).toBeNull();
    expect(screen.getByTestId("output-view").textContent).toContain("You set the plan at $42/month.");
    expect(input().disabled).toBe(false);
  });

  it("re-reads the messages on route.decided, error.raised and run.finished, not on tool or iteration frames", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [ONLY_LIVE] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    ending = "hold";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
    const before = messageReads();

    pushFrame(1, "acp.tool.started");
    pushFrame(2, "loop.iteration.finished", '{"iteration":"1"}');
    await flushed();
    expect(messageReads()).toBe(before);

    pushFrame(3, "loop.route.decided", '{"handler":"groq","memory_kind":"none","confidence_percent":"90%"}');
    await flushed();
    expect(messageReads()).toBe(before + 1);

    pushFrame(4, "run.finished");
    await flushed();
    expect(messageReads()).toBe(before + 2);

    setEnding("close");
    await flushed();
    expect(messageReads()).toBe(before + 3);
  });

  it("re-reads on error.raised too (the spend rail shows it, the conversation gives way to the raise)", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [ONLY_LIVE] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    ending = "hold";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
    const spend = () => asked.filter((request) => request.url === SPEND_ROUTE).length;
    const before = spend();

    pushFrame(1, "error.raised", '{"class":"Boom","message":"it broke","stop_reason":"error"}');
    await flushed();
    expect(spend()).toBe(before + 1);
  });
});

describe("ticket 200 AC5: a refresh re-read keeps the answer on screen, busy", () => {
  it("keeps the answer with aria-busy and no skeleton until the new read lands; Refresh still clears", async () => {
    let release: (value: Response) => void = () => {};
    let reads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url !== MESSAGES_ROUTE) return world(input);
        reads += 1;
        if (reads === 1) return Promise.resolve(answer({ status: 200, text: JSON.stringify({ messages: [MESSAGE_1] }) }));
        return new Promise<Response>((resolved) => (release = resolved));
      }),
    );
    answers = { [HEALTH_ROUTE]: health };
    function Probe() {
      const [refresh, setRefresh] = useState<object>({});
      return (
        <>
          <button type="button" onClick={() => setRefresh({})}>
            bump
          </button>
          <PanelFrame route={MESSAGES_ROUTE} title="Probe" sourceLabel="Ledger: this session" refresh={refresh}>
            {(wire) => <p data-testid="answer">{wire.at(".messages[0].text").text}</p>}
          </PanelFrame>
        </>
      );
    }
    render(shell(<Probe />));
    await flushed();
    const panel = () => screen.getByRole("heading", { name: "Probe" }).closest("section") as HTMLElement;
    expect(screen.getByTestId("answer")).toBeTruthy();
    expect(panel().getAttribute("aria-busy")).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "bump" }));
    await settle();
    expect(screen.getByTestId("answer")).toBeTruthy();
    expect(panel().getAttribute("aria-busy")).toBe("true");
    expect(within(panel()).queryAllByTestId("skeleton-row")).toHaveLength(0);
    expect(within(panel()).getByText(/^Updated/)).toBeTruthy();

    await act(async () => {
      release(answer({ status: 200, text: JSON.stringify({ messages: [MESSAGE_1] }) }));
    });
    await flushed();
    expect(panel().getAttribute("aria-busy")).toBe("false");

    // The shell's Refresh is one more read too: the answer stays, busy, until the new one lands (ticket 211).
    expect(within(panel()).queryAllByRole("button", { name: "Refresh" })).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await settle();
    expect(screen.getByTestId("answer")).toBeTruthy();
    expect(panel().getAttribute("aria-busy")).toBe("true");
    expect(within(panel()).queryAllByTestId("skeleton-row")).toHaveLength(0);
  });
});

// ---- ticket 253: a task's conversation and the agent's request (FR-154) ---------------------------
//
// Every body below is a recorded `tasked.*` fixture, or one of them with a single served field
// changed (named where it is). The clock is pinned to two minutes after the recorded request, so
// "in 8 min" is what the dashboard's own clock says about the served ceiling.

const fixture = (name: string): string => readFileSync(resolve(DASHBOARD, `tests/fixtures/gateway/${name}.body`), "utf8");
const variant = (name: string, change: (body: Record<string, unknown>) => void): string => {
  const body = JSON.parse(fixture(name)) as Record<string, unknown>;
  change(body);
  return JSON.stringify(body);
};

const PERMISSION_SESSION = "run_00000000000000000TASKPERM";
const PERMISSION_TASK = "task_0000000014C0RVA6C5JTBD58CM";
const UNCERTAIN_SESSION = "run_00000000000000000TASKUNSR";
const UNCERTAIN_TASK = "task_0000000015CX2D0XNEV2MCJ0BW";
const DONE_SESSION = "run_00000000000000000TASKDONE";
const DONE_TASK = "task_0000000016S72A9N7WS7ZKGVYX";
const PENDING_PERMISSION = "perm_0000000017R97MB0CYP8769W55";

type TaskedWorld = { session: string; task: string; messages: string; taskBody: string; permissions?: string; decisions?: string };

function taskedRoutes(world: TaskedWorld): Record<string, Answer> {
  const { session } = world;
  return {
    [`/v1/sessions/${session}/messages`]: { status: 200, text: world.messages },
    [`/v1/sessions/${session}/decisions`]: { status: 200, text: world.decisions ?? EMPTY_DECISIONS },
    [`/v1/permissions?session=${encodeURIComponent(session)}`]: {
      status: 200,
      text: world.permissions ?? JSON.stringify({ permissions: [] }),
    },
    [`/v1/tasks/${world.task}`]: { status: 200, text: world.taskBody },
    [`/v1/spend?session=${encodeURIComponent(session)}`]: { status: 200, text: SESSION_SPEND_BODY },
  };
}

async function openTasked(world: TaskedWorld): Promise<HTMLElement> {
  answering(taskedRoutes(world));
  ending = "close";
  const { container } = render(shell(pageAt(ConsolePage, { session: world.session })));
  await flushed();
  return container;
}

const PERMISSION_WORLD: TaskedWorld = {
  session: PERMISSION_SESSION,
  task: PERMISSION_TASK,
  messages: fixture("tasked.sessions.messages.permission"),
  taskBody: fixture("tasked.tasks.permission"),
  permissions: fixture("tasked.permissions.permission"),
};

const permissionsRoute = (session: string): string => `/v1/permissions?session=${encodeURIComponent(session)}`;
const squash = (element: Element | null): string => (element?.textContent ?? "").replace(/\s+/g, " ").trim();
const taskedEvidence = (name: string, container: ParentNode): void => {
  mkdirSync(resolve(REPO, "evidence/253"), { recursive: true });
  writeFileSync(resolve(REPO, "evidence/253", name), (container as HTMLElement).innerHTML, "utf8");
};

describe("ticket 253 AC1: TaskHeader and TaskRail", () => {
  beforeEach(() => {
    vi.setSystemTime(new Date("2026-01-01T12:02:00Z"));
  });

  it("shows the task's text, its state and pause reason, and the rail's two-value figures", async () => {
    const container = await openTasked(PERMISSION_WORLD);

    const header = screen.getByTestId("task-header");
    expect(squash(header)).toContain("run the release checks");
    expect(squash(header)).toContain("Needs you: Waiting for your answer to an agent's request");

    const rail = screen.getByTestId("task-rail");
    const fields = new Map(
      [...rail.querySelectorAll("dl > div")].map((row) => [squash(row.querySelector("dt")), squash(row.querySelector("dd"))]),
    );
    expect(fields.get("Steps")).toBe("0 of 20");
    expect(fields.get("Time")).toBe("0.0 s of 10 min");
    expect(fields.get("Cost")).toBe("$0.000000 of $0.50");
    expect(fields.get("Messages from you")).toBe("0");
    expect(fields.get("Pauses")).toBe("1");
    expect(fields.get("Started by")).toBe("By hand");
    expect(fields.get("Queued")).toBe("12:00");
    expect(rail.querySelector("[role=progressbar], progress, meter")).toBeNull();
    expect(screen.getByRole("button", { name: "Copy id" })).toBeTruthy();
    expect(screen.getByTestId("task-show-command").textContent).toBe(`harnessed task show ${PERMISSION_TASK} --json`);

    // "This task" sits above "This session" in the rail.
    const headings = [...container.querySelectorAll("aside h2")].map((heading) => heading.textContent);
    expect(headings).toEqual(["This task", "This session"]);
    taskedEvidence("task-pending-permission.html", container);
  });

  it("says a done task that paused once was not counted as unattended, from the served flag", async () => {
    await openTasked({
      session: DONE_SESSION,
      task: DONE_TASK,
      messages: fixture("tasked.sessions.messages.done"),
      taskBody: variant("tasked.tasks.done", (task) => {
        task.unattended = "false";
        task.pauses = "1";
      }),
    });
    expect(squash(screen.getByTestId("task-header"))).toContain("Done");
    expect(squash(screen.getByTestId("task-header"))).toContain("not counted as unattended: paused once for you");
  });

  it("names a budget stop with the word in the state and shows the figures plainly", async () => {
    await openTasked({
      session: DONE_SESSION,
      task: DONE_TASK,
      messages: fixture("tasked.sessions.messages.done"),
      taskBody: variant("tasked.tasks.done", (task) => {
        task.state = "failed";
        task.stop_reason = "budget";
        task.unattended = null;
      }),
    });
    expect(squash(screen.getByTestId("task-header"))).toContain("Failed: Stopped at its budget");
    expect(squash(screen.getByTestId("task-rail"))).toContain("Budget spent");
  });
});

describe("ticket 253 AC2: who wrote each message", () => {
  it("labels a task's messages from the served author and task_message, with 0 You bubbles", async () => {
    await openTasked({
      session: DONE_SESSION,
      task: DONE_TASK,
      messages: fixture("tasked.sessions.messages.done"),
      taskBody: fixture("tasked.tasks.done"),
    });
    const labels = screen.getAllByTestId("exchange-author").map((label) => label.textContent);
    expect(labels).toEqual(["From the task, queued 12:00", "Next step of the task"]);
    expect(screen.queryByText("You")).toBeNull();
  });

  it("labels a message the operator typed You, and renders a task-done continuation's reason", async () => {
    const messages = variant("tasked.sessions.messages.done", (body) => {
      const rows = body.messages as Record<string, unknown>[];
      rows[0] = { ...rows[0], author: "operator", task_message: null };
      rows[1] = { ...rows[1], reply: null, reply_reason: "the task was judged finished" };
    });
    await openTasked({
      session: DONE_SESSION,
      task: DONE_TASK,
      messages,
      taskBody: fixture("tasked.tasks.done"),
    });
    const labels = screen.getAllByTestId("exchange-author").map((label) => label.textContent);
    expect(labels).toEqual(["You", "Next step of the task"]);
    const [, continuation] = screen.getAllByTestId("exchange");
    expect(continuation?.textContent).toContain("The task finished");
    expect(continuation?.textContent).not.toMatch(/uncertain stop|No reply/);
    // The Updated line is drawn once under the conversation, not once per nested frame.
    const conversation = screen.getByTestId("conversation").closest("section")?.parentElement;
    expect(within(conversation as HTMLElement).getAllByText(/^Updated/).length).toBe(1);
    expect(within(continuation as HTMLElement).queryByTestId("text-view")).toBeNull();
  });
});

describe("ticket 273: an uncertain stop still reads as one", () => {
  it("keeps the nothing-ran reason for an uncertain continuation", async () => {
    const messages = variant("tasked.sessions.messages.done", (body) => {
      const rows = body.messages as Record<string, unknown>[];
      rows[1] = { ...rows[1], reply: null, reply_reason: "uncertain stop: nothing ran" };
    });
    await openTasked({ session: DONE_SESSION, task: DONE_TASK, messages, taskBody: fixture("tasked.tasks.done") });
    const [, continuation] = screen.getAllByTestId("exchange");
    expect(continuation?.textContent).toContain("uncertain stop: nothing ran");
  });
});

describe("ticket 253 AC3: a PermissionCard in each served state", () => {
  beforeEach(() => {
    vi.setSystemTime(new Date("2026-01-01T12:02:00Z"));
  });

  it("places one card per decision on its exchange, by the served message_seq, in its served words", async () => {
    await openTasked({
      session: PERMISSION_SESSION,
      task: PERMISSION_TASK,
      messages: PERMISSION_WORLD.messages,
      taskBody: PERMISSION_WORLD.taskBody,
      permissions: fixture("permissions.session"),
    });
    const [exchange] = screen.getAllByTestId("exchange");
    const cards = within(exchange as HTMLElement).getAllByTestId("permission-card");
    const read = cards.map((card) => ({
      title: card.querySelector("h3")?.textContent,
      state: card.getAttribute("data-state"),
      code: card.querySelector("[data-testid=code-view]")?.textContent ?? "",
      when: squash(card.querySelector("p")),
    }));
    expect(read.map((card) => card.state)).toEqual(["pending", "approved", "denied", "expired", "allowed", "refused"]);
    expect(read[0]?.title).toBe("recorded-agent asks to run a command");
    expect(read[0]?.code).toContain("a planted pending request");
    expect(read[0]?.when).toBe("Asked 12:00. Denied automatically at 12:10 (in 8 min) if you do not answer.");
    expect(read[1]?.when).toBe("Approved by you at 12:00");
    expect(read[2]?.when).toBe("Denied by you at 12:00");
    expect(read[3]?.when).toBe("No answer by 12:10, so it was denied");
    expect(read[4]?.when).toBe("Allowed by the agent's settings");
    expect(read[5]?.when).toBe("Refused by the agent's settings");
    // Only the pending card has controls.
    expect(screen.getAllByRole("button", { name: "Approve" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Deny" })).toHaveLength(1);
    taskedEvidence("permission-states.html", exchange as HTMLElement);
  });

  it("draws no card for a decision whose message_seq names no exchange", async () => {
    const permissions = variant("tasked.permissions.permission", (body) => {
      (body.permissions as Record<string, unknown>[])[0]!.message_seq = "9";
    });
    await openTasked({ ...PERMISSION_WORLD, permissions });
    expect(screen.queryByTestId("permission-card")).toBeNull();
  });
});

describe("ticket 298: the card shows what the agent sent and lands live", () => {
  beforeEach(() => {
    vi.setSystemTime(new Date("2026-01-01T12:02:00Z"));
  });

  it("shows detail collapsed through CodeView and the warning through Value, 0 derived", async () => {
    await openTasked({
      session: PERMISSION_SESSION,
      task: PERMISSION_TASK,
      messages: PERMISSION_WORLD.messages,
      taskBody: PERMISSION_WORLD.taskBody,
      permissions: fixture("permissions.session"),
    });
    const cards = screen.getAllByTestId("permission-card");
    const details = cards.map((card) => card.querySelector("[data-testid=permission-detail]"));
    expect(details.every((detail) => detail !== null && !(detail as HTMLDetailsElement).open)).toBe(true);
    expect(details[0]?.querySelector("[data-testid=code-view]")?.textContent).toContain('{"command":"echo pending"}');
    const warnings = cards.map((card) => card.querySelector("[data-testid=permission-warning]")?.textContent ?? null);
    expect(warnings).toEqual([null, null, null, null, "ask degraded to allowlist: no one could be asked", null]);
    taskedEvidence("permission-detail-warning.html", cards[4] as HTMLElement);
  });

  it("places a card on the exchange the frame names before the permissions read returns, and re-reads", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: JSON.stringify({ messages: [ONLY_LIVE] }) },
      [DECISIONS_ROUTE]: { status: 200, text: JSON.stringify({ decisions: [DECISION_1] }) },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
    });
    ending = "hold";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
    expect(screen.queryByTestId("permission-arriving")).toBeNull();
    const before = messageReads();

    pushFrame(
      2,
      "acp.permission.request",
      JSON.stringify({
        agent: "recorded-agent",
        ceiling_at: "2026-01-01T12:10:00Z",
        description: "git log --oneline -20",
        hop: 0,
        message_seq: Number(ONLY_LIVE.message_seq),
        permission_id: "perm_LIVE",
        tool: "execute",
        tool_kind: "execute",
      }),
    );
    await flushed();
    const exchange = screen.getAllByTestId("exchange").at(-1) as HTMLElement;
    expect(within(exchange).getByTestId("permission-arriving")).toBeTruthy();
    expect(messageReads()).toBe(before + 1);
  });
});

describe("ticket 253 AC4: answering", () => {
  beforeEach(() => {
    vi.setSystemTime(new Date("2026-01-01T12:02:00Z"));
  });

  const answerRoute = `/control/v1/permissions/${PENDING_PERMISSION}`;
  const approved = (): string =>
    variant("tasked.permissions.permission", (body) => {
      Object.assign((body.permissions as Record<string, unknown>[])[0]!, {
        state: "approved",
        answer: "approve",
        answered_at: "2026-01-01T12:03:00Z",
        answered_via: "console",
        rule: "operator",
      });
    });
  const permissionWrites = (): typeof asked => asked.filter((request) => request.method === "POST");

  it("sends one POST, disables both buttons while it is out, then re-reads the card as approved", async () => {
    await openTasked(PERMISSION_WORLD);
    let release: (value: Answer) => void = () => undefined;
    posts = [new Promise<Answer>((resolve) => (release = resolve))];

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await settle();

    expect(permissionWrites().map((request) => [request.url, request.body])).toEqual([
      [answerRoute, JSON.stringify({ answer: "approve" })],
    ]);
    const pressed = screen.getByRole("button", { name: "Approving..." }) as HTMLButtonElement;
    const other = screen.getByRole("button", { name: "Deny" }) as HTMLButtonElement;
    expect(pressed.disabled).toBe(true);
    expect(other.disabled).toBe(true);
    taskedEvidence("permission-answering.html", screen.getByTestId("permission-card"));

    answers[permissionsRoute(PERMISSION_SESSION)] = { status: 200, text: approved() };
    release({ status: 200, text: JSON.stringify({ permission_id: PENDING_PERMISSION, state: "approved" }) });
    await flushed();

    const card = screen.getByTestId("permission-card");
    expect(card.getAttribute("data-state")).toBe("approved");
    expect(squash(card)).toContain("Approved by you at 12:03");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(permissionWrites()).toHaveLength(1);
    expect(screen.getByTestId("conversation-announcement").textContent).toContain("approved");
    taskedEvidence("permission-approved.html", card);
  });

  it("says Denying... on the pressed button and posts the deny answer", async () => {
    await openTasked(PERMISSION_WORLD);
    posts = [new Promise<Answer>(() => undefined)];
    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    await settle();
    expect(permissionWrites().map((request) => request.body)).toEqual([JSON.stringify({ answer: "deny" })]);
    expect((screen.getByRole("button", { name: "Denying..." }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps the card on a 409 and quotes who answered, from the body", async () => {
    await openTasked(PERMISSION_WORLD);
    posts = [
      {
        status: 409,
        text: JSON.stringify({
          error: "PermissionAnswered",
          message: "perm already answered",
          answer: "approve",
          rule: "operator",
          answered_via: "cli",
          answered_at: "2026-01-01T12:15:00Z",
        }),
      },
    ];
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await flushed();

    expect(screen.getByTestId("already-answered").textContent).toBe("Already answered: approved from the terminal at 12:15");
    expect(screen.getByTestId("permission-card")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(false);
    taskedEvidence("permission-already-answered.html", screen.getByTestId("permission-card"));
  });

  it("shows an ErrorState strip for any other refusal and re-enables both buttons", async () => {
    await openTasked(PERMISSION_WORLD);
    posts = [{ status: 500, text: JSON.stringify({ error: "StorageError", message: "the ledger is locked" }) }];
    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    await flushed();

    const strip = within(screen.getByTestId("permission-card")).getByRole("alert");
    expect(strip.textContent).toContain("500");
    expect(strip.textContent).toContain("the ledger is locked");
    expect((screen.getByRole("button", { name: "Deny" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("ticket 253 AC5: the card is a labelled section, announced once, never answered by Enter", () => {
  beforeEach(() => {
    vi.setSystemTime(new Date("2026-01-01T12:02:00Z"));
  });

  it("is a section labelled by its title, not a live region, and announced once through the polite region", async () => {
    await openTasked(PERMISSION_WORLD);
    const card = screen.getByTestId("permission-card");
    expect(card.tagName).toBe("SECTION");
    const title = document.getElementById(card.getAttribute("aria-labelledby") ?? "");
    expect(title?.textContent).toBe("recorded-agent asks to run a command");
    expect(card.getAttribute("aria-live")).toBeNull();
    expect(card.getAttribute("role")).toBeNull();
    expect(card.querySelector("[aria-live]")).toBeNull();

    const region = screen.getByTestId("conversation-announcement");
    expect(region.getAttribute("role")).toBe("status");
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.textContent).toBe("recorded-agent asks to run a command");

    // Neither button took focus, and the same pending read arriving again says nothing new.
    expect(document.activeElement).not.toBe(screen.getByRole("button", { name: "Approve" }));
    expect(document.activeElement).not.toBe(screen.getByRole("button", { name: "Deny" }));
    expect(screen.getByRole("button", { name: "Approve" }).getAttribute("type")).toBe("button");
  });

  it("sends 0 posts to the permissions route when Enter is pressed in the composer with a card pending", async () => {
    // A session that is not a task's: its composer is open while a card is pending.
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: THREE_MESSAGES },
      [DECISIONS_ROUTE]: { status: 200, text: THREE_MESSAGES_DECISIONS },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
      [PERMISSIONS_ROUTE]: {
        status: 200,
        text: variant("tasked.permissions.permission", (body) => {
          (body.permissions as Record<string, unknown>[])[0]!.message_seq = "1";
        }),
      },
    });
    ending = "close";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
    expect(screen.getByTestId("permission-card").getAttribute("data-state")).toBe("pending");

    posts = [ACCEPTED()];
    fireEvent.click(sidechatButton());
    type("hello");
    fireEvent.keyDown(input(), { key: "Enter" });
    await settle();

    const writes = asked.filter((request) => request.method === "POST");
    expect(writes.map((request) => request.url)).toEqual([messagesRoute(SESSION_ID)]);
    expect(writes.some((request) => request.url.includes("/permissions/"))).toBe(false);
  });
});

describe("ticket 253 AC6: the composer follows the task", () => {
  it("is disabled, saying Claude Code is waiting, while a permission is pending", async () => {
    await openTasked(PERMISSION_WORLD);
    expect(input().disabled).toBe(true);
    expect(screen.getByText("Claude Code is waiting for your answer above")).toBeTruthy();
    expect(sendButton().disabled).toBe(true);
  });

  it("is ready, saying Reply to continue the task, when the task paused on an uncertain stop, and sends a message", async () => {
    await openTasked({
      session: UNCERTAIN_SESSION,
      task: UNCERTAIN_TASK,
      messages: fixture("tasked.sessions.messages.uncertain"),
      taskBody: fixture("tasked.tasks.uncertain"),
    });
    expect(input().disabled).toBe(false);
    expect(input().placeholder).toBe("Reply to continue the task");
    expect(screen.queryByText("Reply to continue the task")).toBeNull();

    posts = [{ status: 202, text: JSON.stringify({ run: `/v1/runs/${UNCERTAIN_SESSION}`, session_id: UNCERTAIN_SESSION, stream: streamRoute(UNCERTAIN_SESSION) }) }];
    type("go ahead");
    fireEvent.click(sendButton());
    await settle();
    const post = asked.find((request) => request.method === "POST");
    expect(post?.url).toBe(messagesRoute(UNCERTAIN_SESSION));
    expect(JSON.parse(post?.body ?? "")).toEqual({ prompt: "go ahead" });
    taskedEvidence("paused-uncertain.html", document.body);
  });

  it("is disabled, saying Harnessed is working on this task, while the task runs", async () => {
    await openTasked({
      session: DONE_SESSION,
      task: DONE_TASK,
      messages: fixture("tasked.sessions.messages.done"),
      taskBody: variant("tasked.tasks.done", (task) => {
        task.state = "running";
        task.finished_at = null;
        task.stop_reason = null;
        task.unattended = null;
      }),
    });
    expect(input().disabled).toBe(true);
    expect(screen.getByText("Harnessed is working on this task")).toBeTruthy();
  });
});

describe("ticket 253 AC7: Queue as a task", () => {
  const QUEUED = {
    task_id: "task_0000000018Q7R2ZZQ0AAAAAAAA",
    state: "queued",
    budget_iterations: "20",
    budget_seconds: "600",
    budget_dollars: "0.50",
    budget_source: "default",
    queued_via: "console",
  };

  it("posts the one string to the tasks route and shows the notice, the budget it got and the run line", async () => {
    answering({});
    render(shell(<ConsolePage />));
    await flushed();
    posts = [{ status: 201, text: JSON.stringify(QUEUED) }];
    type("tidy the notes tomorrow");
    fireEvent.click(screen.getByRole("button", { name: "Queue as a task" }));
    await flushed();

    const writes = asked.filter((request) => request.method === "POST");
    expect(writes.map((request) => [request.url, request.body])).toEqual([
      ["/control/v1/tasks", JSON.stringify({ text: "tidy the notes tomorrow" })],
    ]);
    const notice = screen.getByTestId("queued-notice");
    expect(notice.getAttribute("role")).toBe("status");
    expect(squash(notice)).toContain("Queued as a task");
    expect(squash(notice)).toContain("tidy the notes tomorrow");
    expect(squash(notice)).toContain("Budget: 20 steps, 10 min, $0.50, the default");
    expect(screen.getByTestId("task-run-command").textContent).toBe(`harnessed task run ${QUEUED.task_id}`);
    expect(within(notice).getByRole("link", { name: "See it in Traces" })).toBeTruthy();
    expect(input().value).toBe("");
    taskedEvidence("queued-notice.html", notice);
  });

  it("names the next run from an installed schedule (ticket 305, G2)", async () => {
    answering({ "/v1/schedules": { status: 200, text: fixture("tasked.schedules.listing") } });
    render(shell(<ConsolePage />));
    await flushed();
    posts = [{ status: 201, text: JSON.stringify(QUEUED) }];
    type("tidy the notes tomorrow");
    fireEvent.click(screen.getByRole("button", { name: "Queue as a task" }));
    await flushed();
    const notice = screen.getByTestId("queued-notice");
    expect(within(notice).getByTestId("queued-next-run").textContent).toMatch(/next scheduled run, .*\d/);
    expect(squash(notice)).not.toContain("harnessed task run --due");
  });

  it("shows the copy line, never a run time, when no schedule is installed (ticket 305, G2)", async () => {
    answering({ "/v1/schedules": { status: 200, text: fixture("empty.schedules") } });
    render(shell(<ConsolePage />));
    await flushed();
    posts = [{ status: 201, text: JSON.stringify(QUEUED) }];
    type("tidy the notes tomorrow");
    fireEvent.click(screen.getByRole("button", { name: "Queue as a task" }));
    await flushed();
    const notice = screen.getByTestId("queued-notice");
    expect(within(notice).queryByTestId("queued-next-run")).toBeNull();
    expect(screen.getByTestId("task-run-due-command").textContent).toBe("harnessed task run --due");
    expect(squash(notice)).not.toContain("next scheduled run");
  });

  it("still sends on Enter, and keeps the text with an ErrorState strip when the queue is refused", async () => {
    answering({});
    render(shell(<ConsolePage />));
    await flushed();
    posts = [ACCEPTED()];
    type("say pong");
    fireEvent.keyDown(input(), { key: "Enter" });
    await settle();
    expect(asked.find((request) => request.method === "POST")?.url).toBe(RUNS_ROUTE);
    cleanup();

    asked = [];
    render(shell(<ConsolePage />));
    await flushed();
    posts = [{ status: 400, text: JSON.stringify({ error: "PromptError", message: "the body must be a JSON object" }) }];
    type("   queue me");
    fireEvent.click(screen.getByRole("button", { name: "Queue as a task" }));
    await flushed();
    expect(screen.getByRole("alert").textContent).toContain("PromptError");
    expect(input().value).toBe("   queue me");
    expect(screen.queryByTestId("queued-notice")).toBeNull();
    taskedEvidence("queue-refused.html", document.body);
  });
});

describe("ticket 253 AC8: RepoRecord", () => {
  it("shows the branch, short heads, git's diff stat verbatim, the paths as a list and the closing line", async () => {
    const writeText = vi.fn();
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await openTasked({
      session: DONE_SESSION,
      task: DONE_TASK,
      messages: fixture("tasked.sessions.messages.done"),
      taskBody: fixture("tasked.tasks.done"),
    });
    const card = screen.getByTestId("repo-record");
    expect(card.tagName).toBe("SECTION");
    expect(squash(card)).toContain("Changed files on main");
    expect(squash(card)).toContain("1111111 to 2222222");
    expect(squash(card)).toContain("2 files changed, 9 insertions(+), 1 deletion(-)");
    expect([...card.querySelectorAll("ul > li")].map((item) => item.textContent)).toEqual(["notes/todo.md", "notes/ideas.md"]);
    expect(squash(card)).toContain("Nothing was pushed, merged or deployed. The branch is yours.");

    fireEvent.click(within(card).getByRole("button", { name: "Copy diff command" }));
    expect(writeText).toHaveBeenCalledWith(
      "git -C /work/notes diff 1111111111111111111111111111111111111111 2222222222222222222222222222222222222222",
    );
    taskedEvidence("repo-changed.html", card);
  });

  it("reads Uncommitted changes, and diffs the working tree, when the head did not move but paths changed", async () => {
    const writeText = vi.fn();
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const uncommitted = variant("tasked.sessions.messages.done", (body) => {
      const hop = ((body.messages as Record<string, unknown>[])[0]!.hops as Record<string, unknown>[])[0]!;
      Object.assign(hop.repository as object, {
        committed: "false",
        head_after: "1111111111111111111111111111111111111111",
        head_after_short: "1111111",
      });
    });
    await openTasked({
      session: DONE_SESSION,
      task: DONE_TASK,
      messages: uncommitted,
      taskBody: fixture("tasked.tasks.done"),
    });
    const card = screen.getByTestId("repo-record");
    expect(squash(card)).toContain("Uncommitted changes on main");
    expect(squash(card)).not.toContain("Changed files on");
    expect(squash(card)).toContain("2 files changed, 9 insertions(+), 1 deletion(-)");
    expect([...card.querySelectorAll("ul > li")].map((item) => item.textContent)).toEqual(["notes/todo.md", "notes/ideas.md"]);
    fireEvent.click(within(card).getByRole("button", { name: "Copy diff command" }));
    expect(writeText).toHaveBeenCalledWith("git -C /work/notes diff 1111111111111111111111111111111111111111");
    taskedEvidence("repo-uncommitted.html", card);
  });

  it("ticket 284: an uncommitted card with an unmoved head reads At <head> once; a commit keeps 'to'", async () => {
    const unmoved = variant("tasked.sessions.messages.done", (body) => {
      const hop = ((body.messages as Record<string, unknown>[])[0]!.hops as Record<string, unknown>[])[0]!;
      Object.assign(hop.repository as object, {
        committed: "false",
        head_before_short: "3c1d6a1",
        head_after_short: "3c1d6a1",
      });
    });
    await openTasked({ session: DONE_SESSION, task: DONE_TASK, messages: unmoved, taskBody: fixture("tasked.tasks.done") });
    const heads = squash(screen.getByTestId("repo-record").querySelector("p:nth-of-type(2)") as HTMLElement);
    expect(heads).toBe("At 3c1d6a1");
    cleanup();
    await openTasked({ session: DONE_SESSION, task: DONE_TASK, messages: fixture("tasked.sessions.messages.done"), taskBody: fixture("tasked.tasks.done") });
    expect(squash(screen.getByTestId("repo-record"))).toMatch(/[0-9a-f]{7} to [0-9a-f]{7}/);
  });

  it("reads No files changed when the heads are equal, and draws no card outside a repository", async () => {
    const unchanged = variant("tasked.sessions.messages.done", (body) => {
      const hop = ((body.messages as Record<string, unknown>[])[0]!.hops as Record<string, unknown>[])[0]!;
      Object.assign(hop.repository as object, {
        changed_count: "0",
        committed: "false",
        changed_paths: [],
        diff_stat: "",
        head_after: "1111111111111111111111111111111111111111",
        head_after_short: "1111111",
      });
    });
    await openTasked({
      session: DONE_SESSION,
      task: DONE_TASK,
      messages: unchanged,
      taskBody: fixture("tasked.tasks.done"),
    });
    const [card, ...others] = screen.getAllByTestId("repo-record");
    expect(others).toHaveLength(0);
    expect(squash(card as HTMLElement)).toContain("No files changed on main");
    expect((card as HTMLElement).querySelector("ul")).toBeNull();
    taskedEvidence("repo-unchanged.html", card as HTMLElement);
    cleanup();

    await openTasked(PERMISSION_WORLD);
    expect(screen.queryByTestId("repo-record")).toBeNull();
  });
});

describe("ticket 253 AC9: DirtyTreeRefusal", () => {
  it("is an alert with the served dirty paths and the --allow-dirty copy line, in the reply's place", async () => {
    await openTasked({
      session: UNCERTAIN_SESSION,
      task: UNCERTAIN_TASK,
      messages: fixture("tasked.sessions.messages.uncertain"),
      taskBody: fixture("tasked.tasks.uncertain"),
    });
    const refusal = screen.getByTestId("dirty-tree-refusal");
    expect(refusal.getAttribute("role")).toBe("alert");
    // No agent is served for this task (no entry, no routed agent): the title does not name one.
    expect(squash(refusal)).toContain("The agent did not start");
    expect(squash(refusal)).toContain("The working tree at /work/notes has uncommitted changes");
    expect([...refusal.querySelectorAll("ul > li")].map((item) => item.textContent)).toEqual(["notes/todo.md", "notes/ideas.md"]);
    expect(screen.getByTestId("allow-dirty-command").textContent).toBe(`harnessed task run ${UNCERTAIN_TASK} --allow-dirty`);
    // The refused hop's own repository record is the refusal, not a "changed files" card.
    expect(screen.queryByTestId("repo-record")).toBeNull();
    taskedEvidence("failed-dirty-tree.html", refusal);
  });

  it("names the served agent by its display name: \"Claude Code did not start\" (ticket 269)", async () => {
    await openTasked({
      session: UNCERTAIN_SESSION,
      task: UNCERTAIN_TASK,
      messages: fixture("tasked.sessions.messages.uncertain"),
      taskBody: variant("tasked.tasks.uncertain", (body) => {
        body.entry = "claude-code";
      }),
    });
    expect(squash(screen.getByTestId("dirty-tree-refusal"))).toContain("Claude Code did not start");
    cleanup();

    // An agent with no display name is shown by its served name.
    await openTasked({
      session: UNCERTAIN_SESSION,
      task: UNCERTAIN_TASK,
      messages: fixture("tasked.sessions.messages.uncertain"),
      taskBody: variant("tasked.tasks.uncertain", (body) => {
        body.entry = "aider";
      }),
    });
    expect(squash(screen.getByTestId("dirty-tree-refusal"))).toContain("aider did not start");
  });
});

describe("ticket 279: a task paused on an uncertain stop says so", () => {
  const stopMessage = (seq: number) => ({
    ...MESSAGE_3,
    message_seq: seq,
    author: "task",
    task_message: seq === 1 ? "first" : "continuation",
  });
  const stopDecision = (seq: number) => ({ ...DECISION_3, idx: seq, message_seq: seq });
  const paused = (pause: string, state = "needs_human"): TaskedWorld => ({
    session: UNCERTAIN_SESSION,
    task: UNCERTAIN_TASK,
    messages: variant("tasked.sessions.messages.uncertain", (body) => {
      body.messages = [stopMessage(1), stopMessage(2)];
    }),
    decisions: JSON.stringify({ decisions: [stopDecision(1), stopDecision(2)] }),
    taskBody: variant("tasked.tasks.uncertain", (task) => {
      task.state = state;
      task.pause_reason = pause;
    }),
  });

  it("adds the paused line and the resume command to the last stop only, and a reply placeholder", async () => {
    await openTasked(paused("uncertain"));
    const stops = screen.getAllByTestId("uncertain-stop");
    expect(stops).toHaveLength(2);
    expect(within(stops[0]!).queryByTestId("uncertain-paused")).toBeNull();
    expect(within(stops[0]!).queryByTestId("resume-command")).toBeNull();
    expect(within(stops[1]!).getByTestId("uncertain-paused").textContent).toBe("The task is paused until you answer.");
    expect(within(stops[1]!).getByTestId("resume-command").textContent).toBe(`harnessed run --session ${UNCERTAIN_SESSION} "<reply>"`);
    expect(input().placeholder).toBe("Reply to continue the task");
    expect(screen.queryByText("Reply to continue the task")).toBeNull();
    expect(document.body.textContent).not.toMatch(/[\u2013\u2014]/);
    taskedEvidence("paused-uncertain-stop.html", stops[1]!);
  });

  it("shows none of it for a permission pause, and keeps Message Harnessed for a finished task", async () => {
    await openTasked(paused("permission"));
    expect(screen.queryByTestId("uncertain-paused")).toBeNull();
    expect(screen.queryByTestId("resume-command")).toBeNull();
    expect(input().placeholder).toBe("Message Harnessed");
  });

  it("shows none of it in a session with no task", async () => {
    answering({
      [MESSAGES_ROUTE]: { status: 200, text: THREE_MESSAGES },
      [DECISIONS_ROUTE]: { status: 200, text: THREE_MESSAGES_DECISIONS },
      [SPEND_ROUTE]: { status: 200, text: SESSION_SPEND_BODY },
      [PERMISSIONS_ROUTE]: NO_PERMISSIONS,
    });
    ending = "close";
    render(shell(pageAt(ConsolePage, { session: SESSION_ID })));
    await flushed();
    expect(screen.getByTestId("uncertain-stop")).toBeTruthy();
    expect(screen.queryByTestId("uncertain-paused")).toBeNull();
    fireEvent.click(sidechatButton());
    expect(input().placeholder).toBe("Message Harnessed");
  });
});

describe("ticket 299: the stop line names its compared figure once, a finished task reads finished", () => {
  const stopWorld = (compared: string): TaskedWorld => ({
    session: UNCERTAIN_SESSION,
    task: UNCERTAIN_TASK,
    messages: variant("tasked.sessions.messages.uncertain", (body) => {
      const rows = body.messages as Record<string, unknown>[];
      rows[0] = { ...rows[0], outcome: "uncertain", reply: null, hops: [] };
    }),
    taskBody: fixture("tasked.tasks.uncertain"),
    decisions: fixture(`tasked.sessions.decisions.uncertain-${compared}`),
  });

  it("reads the pre-281 case as its stored 78% against 80% with the router's confidence, floor once", async () => {
    await openTasked(stopWorld("confidence"));
    const stop = screen.getAllByTestId("uncertain-reason").at(-1) as HTMLElement;
    expect(squash(stop)).toBe("78%, the router's confidence, below its 80% floor");
    expect((squash(stop).match(/floor/g) ?? []).length).toBe(1);
  });

  it("reads the post-281 case as the chance it was finished", async () => {
    await openTasked(stopWorld("done"));
    const stop = screen.getAllByTestId("uncertain-reason").at(-1) as HTMLElement;
    expect(squash(stop)).toBe("78%, the chance it was finished, below its 80% floor");
  });

  it("reads the task's pause line in the header and the rail from the served pause leaves", async () => {
    await openTasked(stopWorld("confidence"));
    expect(squash(screen.getByTestId("task-pause-line"))).toBe("41%, the router's confidence, below its 60% floor");
    expect(squash(screen.getByTestId("rail-pause-line"))).toBe("41%, the router's confidence, below its 60% floor");
    expect(document.body.textContent).not.toMatch(/[\u2013\u2014]/);
    taskedEvidence("stop-line.html", document.body);
  });

  it("reads The task finished for the finished reply, never an uncertain stop's words", async () => {
    await openTasked({
      session: DONE_SESSION,
      task: DONE_TASK,
      messages: fixture("tasked.sessions.messages.finished"),
      taskBody: fixture("tasked.tasks.done"),
    });
    expect(document.body.textContent).toContain("The task finished");
    expect(screen.queryByTestId("uncertain-stop")).toBeNull();
    expect(document.body.textContent).not.toContain("confidence floor");
  });
});
