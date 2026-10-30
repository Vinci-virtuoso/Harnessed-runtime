// @vitest-environment jsdom
/**
 * FR-55, FR-59, FR-64 (UJ-7, UJ-11): `/traces` holds the sessions and the runs.
 *
 * The two files that tested `/sessions` and `/runs` are one file here, each as its own `describe`
 * scope (the top-level helpers of the two would otherwise collide), and every assertion either
 * made is kept with its imports and asserted hrefs moved to `/traces`. The last scope holds what
 * ticket 206 added: the stacked page, the one shared `?limit=`, the forwarders and the nav.
 *
 * Original scopes' headers follow.
 *
 * --- sessions ---
 * FR-59 (UJ-11), the page half: `/sessions` and `/sessions?session=`.
 *
 * Two pages, composed from the components tickets 101 to 104 and 111 already built and adding
 * none:
 *   - `/sessions` lists what the events table holds, in the gateway's order, each row linked by
 *     its own `session_id` verbatim, with the `?limit=` control the URL carries, the `IdLookup`
 *     beside it for a session the cap did not reach, and a `terminal_event: null` that reads
 *     `unknown` rather than "running";
 *   - `/sessions?session=` replays `/sessions/{id}/events` and then opens `/stream` with
 *     `Last-Event-ID` equal to the last replayed `seq`, so the handoff shows 0 rows twice; an id
 *     the table never held is the gateway's own 404, and a 200 with zero events opens no
 *     subscription at all, because there is no `seq` to open one at.
 *
 * Both halves render each event as a `TraceStep` (FR-129), and **0 cursors or `seq`s are drawn**,
 * so a row is known by the sentence it reads as: each recorded frame's own `TraceStep`, rendered
 * alone, is what a replayed or streamed row is compared against, and those sentences are distinct
 * across the recording, so "0 lost, 0 repeated" is still a claim that can fail.
 *
 * The inputs are the recorded gateway responses and the recorded SSE stream (decision 33). The
 * one state no recording holds - a listing row whose `terminal_event` is `null` - is written here
 * in the shape `sessions/read.py` sends, the way `runs.test.tsx` writes the same kind of absence,
 * because `design.md` gives it a state and no seeded session produces one. Nothing here edits a
 * recorded fixture (MUST rule 24).
 *
 * Every page is rendered inside `AppShell`, which is how `app/layout.tsx` mounts it, so the walk
 * sees what the operator sees. The two oracles are the shared ones: `tests/support/figures.ts`
 * reports any figure missing half its provenance, and `tests/support/source-at.ts` re-reads the
 * raw bytes at each rendered figure's path and reports any character that differs.
 *
 * --- runs ---
 * FR-55 (UJ-7): the operator reads back a loop run, on `/runs` and `/runs?key=`.
 *
 * Two pages, composed from the shared components of tickets 101 to 104 and adding none:
 *   - `/runs` lists what the run journal holds, in the gateway's order, each row tagged
 *     `session` or `case_run`, with a `?limit=` control the URL carries and a footer that says
 *     the list is capped and whose order it is;
 *   - `/runs?key=` holds two panels over two routes - the run and its trace - where `usage`
 *     is three counts or one explained `unknown`, the trace is walked turn by turn, and a
 *     `case_run` is named as one case of an eval run with no link to that run (`[GAP G-4]`).
 *
 * The inputs are the recorded gateway responses (decision 33) wherever a recording holds the
 * value. Three states no recording holds - `stop_reason: "max_tokens"`, `usage: null`, and a
 * trace with more than one iteration and any tool result at all - are parsed from small bodies
 * written here, as `tests/unit/vocabulary.test.tsx` established for the same reason: `design.md`
 * gives each of them a state and no seeded run produces one. Nothing here edits a recorded
 * fixture (MUST rule 24).
 *
 * Every page is rendered inside `AppShell`, which is how `app/layout.tsx` mounts it, so the walk
 * sees what the operator sees. The two oracles are the shared ones: `tests/support/figures.ts`
 * reports any figure missing half its provenance, and `tests/support/source-at.ts` re-reads the
 * raw bytes at each rendered figure's path and reports any character that differs.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { REPLAY_TITLE, TRANSCRIPT_TITLE } from "../../app/traces/ReplayPanel";
import { DEFAULT_LIMIT, LIMITS, RUN_COMMAND, eventsRoute, footerLine, listingHref, listingRoute, sessionHref, runHref } from "../../app/traces/limit";
import RunsForward from "../../app/runs/page";
import SessionsForward from "../../app/sessions/page";
import TracesPage from "../../app/traces/page";
import { LOOKUP_LABEL, LOOKUP_NOTE } from "../../app/traces/SessionLookup";
import { PANEL_TITLE } from "../../app/traces/SessionsPanel";
import { sessionMessagesRoute } from "../../components/Conversation";
import { AppShell } from "../../components/AppShell";
import { HEALTH_ROUTE } from "../../components/GatewayStatusPill";
import { statusWord } from "../../components/StreamPanel";
import { TraceStep } from "../../components/TraceStep";
import { cliTwinFor } from "../../lib/cli-twins";
import { STALE_AFTER_MINUTES } from "../../lib/clock";
import { GATEWAY_ORIGIN } from "../../lib/fetch-panel";
import { BACKOFF_MS, IDLE_CEILING, LAST_EVENT_ID, type StreamFrame, frameOf, parseFrames, streamRoute } from "../../lib/stream";
import { pageAt } from "./support/query";
import { figuresWithoutSource } from "../support/figures";
import { divergences, renderedFigures, sourceCharactersByPath, nullPaths } from "../support/source-at";
import { EVENTS_BODY, RECORDED_FRAMES, SESSION_ID, VOCABULARY_FRAMES, cutAt, seqOf } from "../support/stream-drops";
import type { ReactNode } from "react";
import { TRUNCATED } from "../../components/StopReasonTag";
import { humanWord } from "../../lib/human-words";
import { nullReasonFor } from "../../lib/null-reasons";
import type { RenderedFigure } from "../support/source-at";

/** Where a limit choice, a lookup or a forward sent the operator. */
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

describe("sessions", () => {
  const DASHBOARD = resolve(import.meta.dirname, "../..");
  const REPO = resolve(DASHBOARD, "..");
  const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
  const EVIDENCE = resolve(REPO, "evidence/112/sessions.txt");
  const LISTING_HTML = resolve(REPO, "evidence/112/sessions-listing.html");
  const DETAIL_HTML = resolve(REPO, "evidence/112/session-detail.html");

  /** The gateway this test's shell is pointed at. Not the operator's 8756 (decision 30). */
  const GATEWAY = GATEWAY_ORIGIN;

  /** The lines this run writes to `evidence/112/sessions.txt`. */
  const log: string[] = [];



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

  const listing = recorded("sessions.listing");
  const emptyListing = recorded("empty.sessions.listing");
  const limitRefused = recorded("badrequest.sessions.limit");
  const noSuchSession = recorded("notfound.sessions.events");
  const health = recorded("health");

  const messagesRecorded = recorded("sessions.messages");

  /** The events read and the messages read the replay makes, both answered 200 (ticket 201). */
  function replaying(events: string, messages: string = messagesRecorded.text): Record<string, Answer> {
    return {
      [proxied(eventsRoute(REPLAY_ID))]: { status: 200, text: events },
      [proxied(sessionMessagesRoute(REPLAY_ID))]: { status: 200, text: messages },
    };
  }

  /**
   * Every figure of the replay against the bytes it cites. `.seconds` is the caption's own
   * humanised `0.0 s` of the served `"0.0"`, the one figure that is worded rather than copied.
   */
  function faithfulReplay(container: ParentNode): void {
    const figures = renderedFigures(container).filter((figure) => !figure.path.endsWith(".seconds"));
    expect(figures.length).toBeGreaterThan(0);
    expect(figuresWithoutSource(container)).toEqual([]);
    expect(
      divergences(
        figures,
        new Map([
          [eventsRoute(REPLAY_ID), EVENTS_BODY],
          [sessionMessagesRoute(REPLAY_ID), messagesRecorded.text],
        ]),
      ),
    ).toEqual([]);
  }

  /** The session id the recorded replay and the recorded stream are both about. */
  const REPLAY_ID = SESSION_ID;

  /** An id the events table never held, as the recorded 404 names it. */
  const UNKNOWN_ID = "run_00000000000000000000000000";

  /**
   * A listing whose newest session has not finished: `terminal_event` is `null`.
   *
   * FR-48 sends that null for a run still in flight *and* for a publisher that died, and no seeded
   * corpus holds either - every recorded session ran to its finish under the fake provider. `D-2`
   * gives the state a rendering, so the body is written here in the shape the emitter sends rather
   * than a recorded one being edited (MUST rule 24).
   */
  const LISTING_WITH_A_NULL = [
    '{"sessions":[',
    '{"first_event":"run.started","first_ts":"2026-01-01T12:00:00Z",',
    `"session_id":"${REPLAY_ID}","terminal_event":null},`,
    '{"first_event":"eval.run.started","first_ts":"2026-01-01T12:00:00Z",',
    '"session_id":"run_0000000004N5DGRB5S4R6NJ6Y2","terminal_event":"eval.run.finished"}]}',
  ].join("");

  /** A session the gateway answered 200 for and that held no event at all. */
  const NO_EVENTS = '{"events":[]}';

  /** One minute of the fake clock, for the five-minute stale mark `D-5` sets. */
  const MINUTE_MS = 60 * 1000;

  /** The recorded 400 a malformed `Last-Event-ID` is refused with, quoted from the gateway. */
  const badCursor = recorded("badrequest.sessions.stream.cursor").text;

  /**
   * The gateway failing to read its own storage. No recording holds a 500 - every fixture is
   * recorded against a database that opens - and `design.md` gives the state its own rendering, so
   * the body is written here in the shape decision 24's error contract sends (MUST rule 24).
   */
  const STORAGE_ERROR = '{"error":"StorageError","message":"no such table: events"}';

  /** The `data:` line of the recorded `retrieval.gate.decided` frame, character for character. */
  const GATE_PAYLOAD = '{"answer":"0.1","failed_open":false,"reason":"","retrieve":false}';

  /** A failing body that is not decision 24's `{"error","message"}` - a proxy's own page. */
  const NOT_THE_CONTRACT = "<html><body>502 Bad Gateway</body></html>";

  // ---- The scripted gateway ----------------------------------------------------------------------

  type Answer = { status: number; text: string; headers?: Record<string, string> };

  /** How one scripted subscription ends: cleanly, cut mid-flight, or still open. */
  type Ending = "close" | "drop" | "hold";

  /** One scripted answer to one `GET .../stream`: frames and an ending, or a refusal. */
  type Episode =
    | { kind: "stream"; chunks: readonly string[]; ending: Ending }
    | { kind: "refused"; status: number; body: string };


  /** What a cut connection says for itself, so a cause is never invented by the panel. */
  const DROP_CAUSE = "the connection was reset";

  /** The JSON routes this run answers, keyed by the proxy URL that asks for them. */
  let answers: Record<string, Answer | "rejects"> = {};
  /** The subscriptions this run hands out, in order; the last one repeats. */
  let episodes: Episode[] = [];
  /** Every `Last-Event-ID` a subscription was opened with, exactly as it was sent. */
  let cursors: (string | null)[] = [];
  /** Every URL and method the pages asked for, so "only `/v1`, only GET" is checked. */
  let asked: { url: string; method: string }[] = [];

  /** A response object with only what `fetch-panel` reads. */
  function answer({ status, text, headers = {} }: Answer): Response {
    return {
      status,
      ok: status === 200,
      headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
      text: async () => text,
    } as unknown as Response;
  }

  /**
   * One scripted subscription: a chunk per read, then the ending.
   *
   * The chunks are enqueued on demand rather than up front, because erroring a stream discards
   * whatever is still queued - a drop scripted the other way round would deliver no frame at all.
   */
  function sseResponse({ chunks, ending }: { chunks: readonly string[]; ending: Ending }): Response {
    let at = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (at < chunks.length) {
          controller.enqueue(new TextEncoder().encode(chunks[at] ?? ""));
          at += 1;
          return undefined;
        }
        if (ending === "close") controller.close();
        if (ending === "drop") controller.error(new Error(DROP_CAUSE));
        return ending === "hold" ? new Promise<void>(() => {}) : undefined;
      },
      cancel() {},
    });
    return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  }

  /** The one `fetch` these pages meet: a JSON table, a stream script, and nothing else. */
  function gateway(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const url = String(input);
    asked.push({ url, method: init?.method ?? "GET" });

    if (url === streamRoute(REPLAY_ID)) {
      cursors.push(new Headers(init?.headers).get(LAST_EVENT_ID));
      const episode = episodes.length > 1 ? (episodes.shift() as Episode) : episodes[0];
      if (episode?.kind === "refused") {
        return Promise.resolve(
          new Response(episode.body, {
            status: episode.status,
            headers: { "content-type": "application/json" },
          }),
        );
      }
      return Promise.resolve(sseResponse(episode ?? { kind: "stream", chunks: [], ending: "hold" }));
    }

    const next = answers[url];
    if (next === undefined) return Promise.reject(new Error(`no answer stubbed for ${url}`));
    if (next === "rejects") return Promise.reject(new Error("ECONNREFUSED"));
    return Promise.resolve(answer(next));
  }

  /** The recorded `/health` the shell's pill polls, so no test here is about the pill. */
  const HEALTH_URL = HEALTH_ROUTE;
  const HEALTH_OK: Answer = { status: health.code, text: health.text };

  /** The proxy URL a gateway route is read through. */
  function proxied(route: string): string {
    return route;
  }

  /** Stub the JSON routes of one test, over the shell's own `/health`. */
  function answering(routes: Record<string, Answer | "rejects">): void {
    answers = { [HEALTH_URL]: HEALTH_OK, ...routes };
  }

  /** Let React settle its reads and its timers. Every state change after this is a render. */
  async function settle(ms = 0): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  /** Three passes, so a replay, its handoff and the subscription it opens all land. */
  async function flushed(): Promise<void> {
    await settle();
    await settle();
    await settle();
    await settle();
  }

  /** The shell as `app/layout.tsx` mounts it, around whatever a page renders. */
  function shell(children: ReactNode) {
    return <AppShell>{children}</AppShell>;
  }

  /** `/sessions` at a limit, rendered as the server renders it. */
  async function renderSessions(limit?: string) {
      const page = pageAt(TracesPage, limit === undefined ? {} : { limit });
    const rendered = render(shell(page));
    await flushed();
    return rendered;
  }

  /** `/sessions?session=`, rendered as the server renders it (FR-64). */
  async function renderSession(sessionId: string) {
    const page = pageAt(TracesPage, { session: sessionId });
    const rendered = render(shell(page));
    await flushed();
    return rendered;
  }

  /** The panel whose heading is `title`, so one page's panels never answer for each other. */
  function panel(title: string): HTMLElement {
    return screen.getByRole("region", { name: title });
  }

  /** The figure at a route and path, found by its non-rendered provenance pair. */
  function figureAt(scope: HTMLElement, route: string, path: string): HTMLElement {
    const found = scope.querySelector<HTMLElement>(`[data-route="${route}"][data-path="${path}"]`);
    if (found === null) throw new Error(`no figure for ${route} -> ${path}`);
    return found;
  }

  /** Every figure of a tree, checked against the bytes of the bodies it claims to come from. */
  function faithful(container: ParentNode, bodies: Record<string, string>): void {
    const figures = renderedFigures(container);
    expect(figures.length).toBeGreaterThan(0);
    expect(figuresWithoutSource(container)).toEqual([]);
    expect(divergences(figures, new Map(Object.entries(bodies)))).toEqual([]);
  }

  /** The stream route the recorded frames were read off, so a frame's figures cite it. */
  const STREAM_ROUTE = streamRoute(REPLAY_ID);

  /** One recorded or hand-written frame, as `lib/stream.ts` hands it to a row. */
  function frameFrom(text: string): StreamFrame {
    const [raw] = parseFrames(text).frames;
    return frameOf(raw, STREAM_ROUTE);
  }

  /** The sentence each `TraceStep` under `root` reads as, in the order the list shows them. */
  function sentencesIn(root: ParentNode | null): string[] {
    if (root === null) return [];
    return Array.from(root.querySelectorAll('[data-testid="trace-step"]')).map(
      (step) => step.querySelector('[data-testid="trace-sentence"]')?.textContent ?? "",
    );
  }

  /**
   * What each frame reads as on its own: its `TraceStep`, rendered alone into a list that is never
   * attached to the page - the identity a row is compared by, now that no `seq` is drawn.
   */
  function sentencesOf(frames: readonly string[]): string[] {
    return frames.map((text) => {
      const alone = render(<TraceStep frame={frameFrom(text)} />, {
        container: document.createElement("ul"),
      });
      const [sentence] = sentencesIn(alone.container);
      alone.unmount();
      return sentence;
    });
  }

  /** What the replay panel's steps read as, in the order it shows them. */
  function replayedSentences(): string[] {
    return sentencesIn(within(panel(REPLAY_TITLE)).getByTestId("replay-log"));
  }

  /** What the stream log's steps read as, in the order the log shows them. */
  function streamedSentences(): string[] {
    return sentencesIn(screen.queryByTestId("stream-log"));
  }

  /** The streamed steps themselves, in order. */
  function streamedSteps(): HTMLElement[] {
    return within(screen.getByTestId("stream-log")).queryAllByTestId("trace-step");
  }

  /** Each field an expanded step lists: its path, and the figure `Value` rendered for it. */
  function fieldsOf(step: HTMLElement): { path: string; shown: string }[] {
    const list = within(step).getByTestId("trace-step-fields");
    return Array.from(list.querySelectorAll("dd [data-figure]"), (figure) => ({
      path: figure.getAttribute("data-path") ?? "",
      shown: figure.textContent ?? "",
    }));
  }

  function statusText(): string {
    return screen.getByTestId("stream-status").textContent ?? "";
  }

  /** The panel's status words (`StreamPanel.tsx::statusWord`), read off the component itself. */
  const WAITING = statusWord({ kind: "connecting" }, false);
  const RECONNECTING = statusWord({ kind: "reconnecting", cursor: "0" }, true);
  const DONE = statusWord({ kind: "closed", reason: IDLE_CEILING }, true);
  const COULD_NOT_CONNECT = statusWord({ kind: "refused", status: "400", body: "" }, false);

  function writeHtml(path: string, container: ParentNode): void {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, (container as HTMLElement).innerHTML, "utf8");
  }

  beforeEach(() => {
    vi.useFakeTimers();
    pushed = [];
    answers = {};
    episodes = [];
    cursors = [];
    asked = [];
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
    vi.stubGlobal("fetch", vi.fn(gateway));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  afterAll(() => {
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });

  // ---- `/sessions`: the listing --------------------------------------------------------------------

  describe("/sessions lists every session the events table holds (FR-59)", () => {
    it("shows each row's id, first event, first ts and terminal event, in the sent order", async () => {
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text } });
      const { container } = await renderSessions();

      const table = within(panel(PANEL_TITLE)).getByRole("table");
      expect(
        within(table)
          .getAllByRole("columnheader")
          .map((cell) => cell.textContent),
      ).toEqual(["session id", "first event", "first ts", "terminal event"]);

      const sent = JSON.parse(listing.text) as {
        sessions: { session_id: string; first_event: string; terminal_event: string | null }[];
      };
      const rows = within(table).getAllByRole("row").slice(1);
      expect(rows).toHaveLength(sent.sessions.length);
      expect(rows.length).toBeGreaterThan(1);

      // The order is the response's order, row for row, and the link is the row's own id verbatim.
      for (const [index, row] of rows.entries()) {
        const expected = sent.sessions[index];
        const id = within(row).getByRole("link");
        expect(id.textContent).toBe(expected?.session_id);
        expect(id.getAttribute("href")).toBe(`/traces?session=${expected?.session_id}`);
        expect(sessionHref(expected?.session_id ?? "")).toBe(id.getAttribute("href"));
        expect(row.textContent).toContain(expected?.first_event);
        expect(row.textContent).toContain(expected?.terminal_event);
      }

      faithful(container, { [listingRoute(DEFAULT_LIMIT)]: listing.text });
      writeHtml(LISTING_HTML, container);
      log.push(`/sessions: ${rows.length} rows in the gateway's order, each linked by its own id`);
    });

    it("reads a session with no terminal event as `unknown`, never as running", async () => {
      answering({
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: 200, text: LISTING_WITH_A_NULL },
      });
      const { container } = await renderSessions();

      const table = within(panel(PANEL_TITLE)).getByRole("table");
      const first = within(table).getAllByRole("row")[1] as HTMLElement;
      const terminal = within(first).getAllByRole("cell")[3] as HTMLElement;
      expect(terminal.textContent).toContain("unknown");
      expect(first.textContent).not.toContain("running");
      expect(first.textContent).not.toContain("live");

      // The oracle reads the same null off the raw bytes, so `unknown` is the faithful rendering.
      faithful(container, { [listingRoute(DEFAULT_LIMIT)]: LISTING_WITH_A_NULL });
      log.push("/sessions: `terminal_event: null` reads `unknown`, never `running` and never `live`");
    });

    it("offers 20, 50 and 100, keeps the choice in the URL and says what it is showing", async () => {
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text } });
      await renderSessions();

      expect([...LIMITS]).toEqual(["20", "50", "100"]);
      // The one 20/50/100 control on `/traces` is the Runs section's (asserted in the runs scope).
      expect(screen.queryAllByTestId("limit-control")).toHaveLength(0);
      expect(screen.getByTestId("sessions-footer").textContent).toBe(footerLine(DEFAULT_LIMIT));
      expect(screen.getByTestId("sessions-footer").textContent).toBe(
        "showing up to 20, newest first, as the gateway ordered them",
      );

      cleanup();

      // And the page served at that URL reads the gateway at that limit and says so.
      answering({ [proxied(listingRoute("50"))]: { status: listing.code, text: listing.text } });
      await renderSessions("50");
      expect(asked.map((call) => call.url)).toContain(proxied(listingRoute("50")));
      expect(screen.getByTestId("sessions-footer").textContent).toBe(footerLine("50"));
      log.push("/sessions: the 20/50/100 control is URL state; the footer names the cap and the order");
    });

    it("keeps the `session id` lookup beside the listing and opens what was typed, verbatim", async () => {
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text } });
      await renderSessions();

      const lookup = screen.getByRole("region", { name: LOOKUP_LABEL });
      expect(screen.getByTestId("lookup-note").textContent).toBe(LOOKUP_NOTE);
      const field = within(lookup).getByLabelText("session id");
      const open = within(lookup).getByRole("button", { name: "Open" }) as HTMLButtonElement;
      expect(open.disabled).toBe(true);

      // An id with the shape of a case run key: nothing is trimmed, split or reformatted.
      const typed = `  ${REPLAY_ID}:case:1  `;
      fireEvent.change(field, { target: { value: typed } });
      fireEvent.submit(field.closest("form") as HTMLFormElement);
      expect(pushed).toEqual([sessionHref(typed)]);
      const opened = new URL(pushed[0] ?? "", "http://dashboard.invalid");
      expect(opened.searchParams.get("session")).toBe(typed);
      log.push("/sessions: the lookup opens the characters typed, untrimmed and unsplit");
    });

    it("names the route and `harnessed run` where the events table is empty", async () => {
      answering({
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: emptyListing.code, text: emptyListing.text },
      });
      const { container } = await renderSessions();

      const body = panel(PANEL_TITLE).textContent ?? "";
      expect(body).toContain("No sessions recorded yet.");
      expect(body).not.toContain(listingRoute(DEFAULT_LIMIT));
      expect(body).toContain(RUN_COMMAND);
      expect(body).toContain("harnessed run");
      expect(within(panel(PANEL_TITLE)).queryByRole("table")).toBeNull();
      // The one way to reach a session an empty listing did not name stays on the page.
      expect(within(screen.getByRole("region", { name: LOOKUP_LABEL })).getByLabelText("session id")).
        toBeTruthy();
      expect(figuresWithoutSource(container)).toEqual([]);
      log.push("/sessions: zero rows name the route that answered and the command that makes some");
    });

    it("shows the gateway's own refusal when the URL asks for a limit it will not serve", async () => {
      answering({
        [proxied(listingRoute("0"))]: { status: limitRefused.code, text: limitRefused.text },
      });
      await renderSessions("0");

      const body = panel(PANEL_TITLE).textContent ?? "";
      expect(body).toContain("LimitError");
      expect(body).toContain("limit '0' is not a positive integer.");
      expect(within(panel(PANEL_TITLE)).queryByRole("table")).toBeNull();
      log.push("/sessions: a limit the gateway refuses is its own 400, quoted, never a client guess");
    });

    it("renders no figure from memory when the gateway cannot be reached", async () => {
      answering({
        [proxied(listingRoute(DEFAULT_LIMIT))]: "rejects",
      });
      const { container } = await renderSessions();

      expect(within(panel(PANEL_TITLE)).getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
      expect(within(panel(PANEL_TITLE)).queryByRole("table")).toBeNull();
      expect(renderedFigures(panel(PANEL_TITLE))).toEqual([]);
      expect(figuresWithoutSource(container)).toEqual([]);
      log.push("/sessions: an unreachable gateway renders 0 figures, not a remembered listing");
    });
  });

  // ---- `/sessions?session=`: the replay and the stream it hands off to -------------------------

  describe("/sessions?session= replays, then follows from the cursor (FR-59)", () => {
    it("replays every recorded event and opens the stream at the last replayed `seq`", async () => {
      answering(replaying(EVENTS_BODY));
      episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
      const { container } = await renderSession(REPLAY_ID);

      expect(screen.getByTestId("session-identity").textContent).toBe(REPLAY_ID);
      // The replay is a log of steps now, not a table of `seq`, `name` and `ts` columns.
      const steps = within(panel(REPLAY_TITLE)).getByTestId("replay-log");
      expect(steps.getAttribute("role")).toBe("log");
      expect(within(panel(REPLAY_TITLE)).queryByRole("table")).toBeNull();

      // One step per recorded event, in the response's order, each reading as that event's own.
      const sent = JSON.parse(EVENTS_BODY) as { events: { seq: number; name: string; ts: string }[] };
      const recording = sentencesOf(RECORDED_FRAMES);
      expect(new Set(recording).size).toBe(RECORDED_FRAMES.length);
      expect(replayedSentences()).toEqual(recording);
      expect(replayedSentences()).toHaveLength(sent.events.length);
      // A replayed step carries its own `ts`, said relative to the dashboard's clock.
      for (const time of within(steps).getAllByTestId("trace-step-time")) {
        expect(time.textContent).not.toBe("");
      }

      // The cursor is the last replayed leaf's own characters, sent verbatim, never `seq + 1`.
      const last = String(sent.events.at(-1)?.seq);
      expect(cursors).toEqual([last]);
      // ...and it is sent, never drawn: 0 cursors, `seq`s or raw event names on the page (FR-129).
      expect(screen.queryByTestId("replay-handoff")).toBeNull();
      expect(screen.queryByTestId("stream-cursor")).toBeNull();
      expect(container.textContent).not.toContain(LAST_EVENT_ID);
      for (const event of sent.events) {
        expect(panel(REPLAY_TITLE).textContent).not.toContain(event.name);
      }

      // 0 duplicated rows at the handoff: the replay holds every row and the stream holds none.
      expect(streamedSentences()).toEqual([]);
      expect(statusText()).toBe(WAITING);
      expect(asked.every((call) => call.method === "GET")).toBe(true);
      expect(asked.every((call) => call.url.startsWith("/v1"))).toBe(true);

      faithfulReplay(panel(REPLAY_TITLE));
      expect(figuresWithoutSource(container)).toEqual([]);
      writeHtml(DETAIL_HTML, container);
      log.push(
        `/sessions?session=: ${replayedSentences().length} replayed steps, stream opened at seq ${last}, 0 repeated`,
      );
    });

    it("renders the frames after the cursor and none at or before it", async () => {
      // The replay is cut short of the recording, so the stream has rows left to deliver: the page
      // is asserted on what it rendered, not on a number any component worked out.
      const partial = shortReplay(4);
      answering(replaying(partial));
      episodes = [{ kind: "stream", chunks: [cutAt("4").after.join("")], ending: "close" }];
      const { container } = await renderSession(REPLAY_ID);

      const cut = cutAt("4");
      expect(cursors).toEqual(["4"]);
      expect(seqOf(cut.upTo)).toEqual(["1", "2", "3", "4"]);
      expect(replayedSentences()).toEqual(sentencesOf(cut.upTo));
      expect(seqOf(cut.after)).toEqual(["5", "6", "7", "8"]);
      expect(streamedSentences()).toEqual(sentencesOf(cut.after));

      // 0 lost and 0 repeated: the two halves together are the recorded stream, each step once.
      const walked = [...replayedSentences(), ...streamedSentences()];
      expect(walked).toEqual(sentencesOf(RECORDED_FRAMES));
      expect(walked).toHaveLength(seqOf(RECORDED_FRAMES).length);
      expect(new Set(walked).size).toBe(walked.length);
      expect(statusText()).toBe(DONE);
      expect(figuresWithoutSource(container)).toEqual([]);
      log.push(
        "/sessions?session=: a cursor of 4 renders 5, 6, 7 - 0 rows lost and 0 rows repeated",
      );
    });

    it("loses nothing and repeats nothing when the connection is cut and comes back", async () => {
      const partial = shortReplay(2);
      answering(replaying(partial));
      const cut = cutAt("4");
      episodes = [
        { kind: "stream", chunks: [RECORDED_FRAMES.slice(2, 4).join("")], ending: "drop" },
        { kind: "stream", chunks: [cut.after.join("")], ending: "close" },
      ];
      await renderSession(REPLAY_ID);

      expect(cursors).toEqual(["2"]);
      expect(replayedSentences()).toEqual(sentencesOf(RECORDED_FRAMES.slice(0, 2)));
      expect(streamedSentences()).toEqual(sentencesOf(RECORDED_FRAMES.slice(2, 4)));
      expect(statusText()).toBe(RECONNECTING);

      // The retry opens at the last row the operator can see, so the gateway resumes after it.
      await settle(1000);
      await flushed();
      expect(cursors).toEqual(["2", "4"]);
      expect(streamedSentences()).toEqual(sentencesOf(RECORDED_FRAMES.slice(2)));

      const walked = [...replayedSentences(), ...streamedSentences()];
      expect(walked).toEqual(sentencesOf(RECORDED_FRAMES));
      expect(new Set(walked).size).toBe(walked.length);
      log.push("/sessions?session=: a cut mid-stream resumes at its cursor - 0 lost, 0 repeated");
    });

    it("quotes the gateway's 404 for an id the events table never held", async () => {
      answering({
        [proxied(eventsRoute(UNKNOWN_ID))]: { status: noSuchSession.code, text: noSuchSession.text },
      });
      const { container } = await renderSession(UNKNOWN_ID);

      const body = panel(REPLAY_TITLE).textContent ?? "";
      expect(body).toContain("SurfaceNotFound");
      expect(body).toContain(`no session id '${UNKNOWN_ID}'`);
      expect(body).toContain("the events table holds no event under it");

      // No replay, so no cursor, so no subscription: a 404 is the end of this page and not a wait.
      expect(within(panel(REPLAY_TITLE)).queryByTestId("replay-log")).toBeNull();
      expect(screen.queryByTestId("stream-status")).toBeNull();
      expect(cursors).toEqual([]);
      expect(figuresWithoutSource(container)).toEqual([]);
      log.push("/sessions?session=: an unknown id is the gateway's own 404 body, and opens no stream");
    });

    it("opens no stream for a session that answered with no events at all", async () => {
      // A session with no events is the same 404 on this gateway (`events/read.py` keys the table
      // by the rows it would be empty of), so this 200 is the response that cannot arrive - and if
      // it did, there is no `seq` to open a subscription at, and the page says so rather than
      // subscribing from the start and printing the session twice.
      answering(replaying(NO_EVENTS));
      await renderSession(REPLAY_ID);

      expect(panel(REPLAY_TITLE).textContent).toContain("No replay recorded yet.");
      expect(panel(REPLAY_TITLE).textContent).not.toContain(eventsRoute(REPLAY_ID));
      expect(within(panel(REPLAY_TITLE)).queryByTestId("replay-log")).toBeNull();
      expect(screen.queryByTestId("stream-status")).toBeNull();
      expect(cursors).toEqual([]);
      log.push("/sessions?session=: 0 replayed rows is 0 subscriptions; the 404 is the gateway's answer");
    });

    it("renders no figure from memory when the gateway cannot be reached", async () => {
      answering({
        [proxied(eventsRoute(REPLAY_ID))]: "rejects",
      });
      const { container } = await renderSession(REPLAY_ID);

      expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
      expect(renderedFigures(panel(REPLAY_TITLE))).toEqual([]);
      expect(screen.queryByTestId("stream-status")).toBeNull();
      expect(cursors).toEqual([]);
      expect(figuresWithoutSource(container)).toEqual([]);
      log.push("/sessions?session=: an unreachable gateway replays 0 rows and opens 0 subscriptions");
    });
  });

  /**
   * The recorded replay, cut to its first `count` events.
   *
   * It is the recording's own characters, sliced at a frame boundary and re-closed, which is what a
   * replay of a session still in flight looks like: the rows so far, and the rest still to come
   * over the stream. Nothing is rewritten and no field is invented (MUST rule 24).
   */
  function shortReplay(count: number): string {
    const sent = JSON.parse(EVENTS_BODY) as { events: unknown[] };
    return JSON.stringify({ events: sent.events.slice(0, count) });
  }

  // ---- The replay reads as a transcript (ticket 201) -------------------------------------------

  /** Two hand-written messages over events 1 to 7 and 8 to 14, in the shape `sessions/read.py` sends. */
  function twoTurns(): { events: string; messages: string } {
    const recordedEvents = (JSON.parse(EVENTS_BODY) as { events: { seq: number; ts: string }[] }).events;
    const turn = recordedEvents.length;
    const again = recordedEvents.map((event) => ({ ...event, seq: event.seq + turn }));
    const message = (seq: number, first: number, last: number | null, text: string | null, reply: string | null) => ({
      first_seq: first,
      last_seq: last,
      message_seq: seq,
      outcome: "routed",
      handlers: [],
      hops: [],
      steps: 1,
      seconds: "0.0",
      handler: "groq",
      created_at: "2026-01-01T12:00:00Z",
      text,
      text_reason: text === null ? "recorded before the router" : null,
      reply: reply === null ? null : { output_id: null, output_kind: "text", text: reply },
      reply_reason: reply === null ? "the message is still running" : null,
    });
    return {
      events: JSON.stringify({ events: [...recordedEvents, ...again] }),
      messages: JSON.stringify({
        messages: [message(1, 1, turn, "ping", "pong"), message(2, turn + 1, 2 * turn, "again", "pong again")],
      }),
    };
  }

  function rowKinds(): string[] {
    const log = within(panel(REPLAY_TITLE)).getByTestId("replay-log");
    return Array.from(log.querySelectorAll("li"))
      .filter((li) => li.closest("[data-testid='trace-step']") === null || li.matches("[data-testid='trace-step']"))
      .map((li) => li.getAttribute("data-testid") ?? "")
      .filter((id) => id === "transcript-input" || id === "transcript-response" || id === "trace-step");
  }

  describe("/sessions?session= reads as a transcript (FR-59, FR-128, FR-129)", () => {
    it("opens with the operator's input, the steps, then the response", async () => {
      answering(replaying(EVENTS_BODY));
      episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
      const { container } = await renderSession(REPLAY_ID);

      const log = within(panel(REPLAY_TITLE)).getByTestId("replay-log");
      const input = within(log).getByTestId("transcript-input");
      const response = within(log).getByTestId("transcript-response");
      expect(input.textContent).toBe("user input:ping");
      expect(response.textContent).toContain("response:");
      expect(response.textContent).toContain("pong");
      expect(within(response).getByTestId("output-view")).toBeTruthy();

      const rows = Array.from(log.querySelectorAll("ul > li"));
      expect(rows[0]).toBe(input);
      expect(rows.at(-1)).toBe(response);
      expect(replayedSentences()).toEqual(sentencesOf(RECORDED_FRAMES));

      // Each figure cites its own response: the input and reply the messages read, steps the events.
      const messagesRoute = sessionMessagesRoute(REPLAY_ID);
      expect(figureAt(input as HTMLElement, messagesRoute, ".messages[0].text").textContent).toBe("ping");
      faithfulReplay(panel(REPLAY_TITLE));
      expect(figuresWithoutSource(container)).toEqual([]);
    });

    it("groups each message's steps between its own input and response, every event once", async () => {
      const { events, messages } = twoTurns();
      answering(replaying(events, messages));
      episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
      await renderSession(REPLAY_ID);

      const kinds = rowKinds();
      const step = "trace-step";
      const turn = RECORDED_FRAMES.length;
      expect(kinds).toEqual([
        "transcript-input",
        ...Array<string>(turn).fill(step),
        "transcript-response",
        "transcript-input",
        ...Array<string>(turn).fill(step),
        "transcript-response",
      ]);
      const inputs = within(panel(REPLAY_TITLE)).getAllByTestId("transcript-input");
      expect(inputs.map((row) => row.textContent)).toEqual(["user input:ping", "user input:again"]);
      expect(within(panel(REPLAY_TITLE)).getAllByTestId("transcript-response")).toHaveLength(2);
      expect(replayedSentences()).toEqual([...sentencesOf(RECORDED_FRAMES), ...sentencesOf(RECORDED_FRAMES)]);
    });

    it("says why an input is absent, and why a live message has no response", async () => {
      const { events, messages } = twoTurns();
      const parsed = JSON.parse(messages) as { messages: { text: string | null }[] };
      const first = parsed.messages[0] as Record<string, unknown>;
      first.text = null;
      first.text_reason = "recorded before the router";
      const second = parsed.messages[1] as Record<string, unknown>;
      second.last_seq = null;
      second.reply = null;
      second.reply_reason = "the message is still running";
      answering(replaying(events, JSON.stringify(parsed)));
      episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
      await renderSession(REPLAY_ID);

      const [firstInput] = within(panel(REPLAY_TITLE)).getAllByTestId("transcript-input");
      expect(firstInput?.textContent).toBe("user input:recorded before the router");
      expect(panel(REPLAY_TITLE).textContent).not.toContain("unknown");
      const responses = within(panel(REPLAY_TITLE)).getAllByTestId("transcript-response");
      expect(responses[1]?.textContent).toBe("response:the message is still running");
      expect(within(responses[1] as HTMLElement).queryByTestId("output-view")).toBeNull();
    });

    it("renders events before every message's first_seq first, ungrouped", async () => {
      const { events, messages } = twoTurns();
      const parsed = JSON.parse(messages) as { messages: { first_seq: number }[] };
      (parsed.messages[0] as { first_seq: number }).first_seq = 3;
      answering(replaying(events, JSON.stringify(parsed)));
      episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
      await renderSession(REPLAY_ID);

      expect(rowKinds().slice(0, 3)).toEqual(["trace-step", "trace-step", "transcript-input"]);
      expect(replayedSentences()).toHaveLength(2 * RECORDED_FRAMES.length);
    });

    it("quotes the messages read's own 404 in a Transcript panel when it fails", async () => {
      answering({
        ...replaying(EVENTS_BODY),
        [proxied(sessionMessagesRoute(REPLAY_ID))]: { status: 404, text: recorded("notfound.sessions.messages").text },
      });
      episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
      await renderSession(REPLAY_ID);

      expect(within(panel(REPLAY_TITLE)).getByRole("region", { name: TRANSCRIPT_TITLE })).toBeTruthy();
      expect(cursors).toEqual(["8"]);
    });
  });

  // ---- The source behind a figure, and the states a page wears over time --------------------------

  describe("every figure on either page can say where it came from (FR-51, `D-4`)", () => {
    it("opens the listing's own response, at the gateway's URL, with its `--json` twin", async () => {
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text } });
      await renderSessions();

      const route = listingRoute(DEFAULT_LIMIT);
      const sent = JSON.parse(listing.text) as { sessions: { first_event: string }[] };
      const trigger = figureAt(panel(PANEL_TITLE), route, ".sessions[0].first_event");
      fireEvent.click(trigger);

      // Labelled "Where this came from", never the route - and no endpoint string anywhere in it.
      const drawer = screen.getByRole("dialog");
      expect(within(drawer).getByRole("heading").textContent).toBe("Where this came from");
      expect(drawer.textContent).not.toContain(route);
      expect(drawer.textContent).not.toContain("GET");
      expect(within(drawer).getByTestId("cli-twin").textContent).toBe(cliTwinFor(route));
      expect(within(drawer).getByTestId("cli-twin").textContent).toBe("harnessed sessions list --json");

      // The record in the drawer is the body the figures were read out of, character for character.
      expect(within(drawer).getByTestId("record-view")).toBeTruthy();
      expect(within(drawer).getByTestId("exact-value").textContent).toBe(sent.sessions[0]?.first_event);
      expect(within(drawer).getByTestId("cited").textContent).toBe(sent.sessions[0]?.first_event);

      // Tab at either edge stays inside the drawer, and Esc closes it wherever focus went.
      fireEvent.keyDown(drawer, { key: "Tab" });
      fireEvent.keyDown(drawer, { key: "Tab", shiftKey: true });
      fireEvent.keyDown(drawer, { key: "a" });
      fireEvent.keyDown(document, { key: "Escape" });
      expect(screen.queryByRole("dialog")).toBeNull();
      log.push("/sessions: a figure opens the listing's own record and the CLI twin");
    });

    it("opens the same drawer from a replayed row, over the replay's own response", async () => {
      answering(replaying(EVENTS_BODY));
      episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
      await renderSession(REPLAY_ID);

      // `loop.route.decided`'s sentence names the handler it routed to: a payload figure of the
      // replay's own response, at its full path in that body (no `seq` or `name` is drawn to open).
      const route = eventsRoute(REPLAY_ID);
      const sent = JSON.parse(EVENTS_BODY) as { events: { payload: { handler?: string } }[] };
      const handler = sent.events[1]?.payload.handler;
      const trigger = figureAt(panel(REPLAY_TITLE), route, ".events[1].payload.handler");
      expect(trigger.textContent).toBe(handler);
      fireEvent.keyDown(trigger, { key: "Enter" });
      const drawer = screen.getByRole("dialog");
      expect(within(drawer).getByRole("heading").textContent).toBe("Where this came from");
      expect(drawer.textContent).not.toContain(route);
      expect(within(drawer).getByTestId("record-view")).toBeTruthy();
      expect(within(drawer).getByTestId("exact-value").textContent).toBe(handler);
      expect(within(drawer).getByTestId("cited").textContent).toBe(handler);
      // A session's events have a `--json` twin since ticket 232, and the drawer offers it.
      expect(cliTwinFor(route)).toBe("harnessed sessions events <session_id> --json");
      expect(within(drawer).getByTestId("cli-twin").textContent).toBe(cliTwinFor(route));

      fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
      expect(screen.queryByRole("dialog")).toBeNull();
      log.push("/sessions?session=: a replayed row's chip opens the replay's bytes, and no twin is faked");
    });

    it("says once, and then keeps counting, that the listing is five minutes old", async () => {
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text } });
      await renderSessions();

      expect(within(panel(PANEL_TITLE)).getByTestId("panel-announcement").textContent).toBe("");
      await settle(STALE_AFTER_MINUTES * MINUTE_MS);
      const announced = within(panel(PANEL_TITLE)).getByTestId("panel-announcement").textContent ?? "";
      expect(announced).toBe(`Updated ${STALE_AFTER_MINUTES} min ago`);

      // The badge keeps counting; the announcement is frozen at the crossing, so it is said once.
      await settle(MINUTE_MS);
      expect(within(panel(PANEL_TITLE)).getByTestId("panel-announcement").textContent).toBe(announced);
      expect(panel(PANEL_TITLE).textContent).toContain("Updated 6 min ago");
      log.push("/sessions: the five-minute crossing is announced once and the badge keeps counting");
    });

    it("quotes the gateway's own 500 and reads it again on `Refetch`", async () => {
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: { status: 500, text: STORAGE_ERROR } });
      await renderSessions();

      const failed = within(panel(PANEL_TITLE)).getByRole("alert");
      expect(failed.textContent).toContain("StorageError");
      expect(failed.textContent).toContain("no such table: events");
      expect(within(panel(PANEL_TITLE)).queryByRole("table")).toBeNull();

      // `Refetch` is a new read, not a re-render of the old answer (`D-6`).
      const before = asked.filter((call) => call.url === proxied(listingRoute(DEFAULT_LIMIT))).length;
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text } });
      fireEvent.click(within(failed).getByRole("button", { name: "Refetch" }));
      await flushed();
      const after = asked.filter((call) => call.url === proxied(listingRoute(DEFAULT_LIMIT))).length;
      expect(after).toBe(before + 1);
      expect(within(panel(PANEL_TITLE)).getByRole("table")).toBeTruthy();
      log.push("/sessions: a 500 is the gateway's own body, and `Refetch` is one more read");
    });
  });

  // ---- What a live row shows, and what a refused subscription shows -------------------------------

  describe("a streamed row shows its payload, and a refusal stops the page (FR-59)", () => {
    it("reads each live row as its event's sentence, and expands to every payload field", async () => {
      answering(replaying(shortReplay(1)));
      const after = cutAt("1").after;
      episodes = [{ kind: "stream", chunks: [after.join("")], ending: "close" }];
      const { container } = await renderSession(REPLAY_ID);

      expect(cursors).toEqual(["1"]);
      expect(seqOf(after)).toEqual(["2", "3", "4", "5", "6", "7", "8"]);
      expect(streamedSentences()).toEqual(sentencesOf(after));
      expect(statusText()).toBe(DONE);

      // `retrieval.gate.decided` says in words whether memory was needed, read off its own
      // `retrieve` boolean - `false` on this recording - and never its raw event name.
      const gateFrame = frameFrom(after[1]);
      expect(gateFrame.name.text).toBe("retrieval.gate.decided");
      expect(gateFrame.data).toBe(GATE_PAYLOAD);
      expect(gateFrame.payload.at(".retrieve").text).toBe("false");
      const gate = streamedSteps()[1];
      expect(within(gate).getByTestId("trace-sentence").textContent).toBe("Checked memory: not needed");
      expect(gate.textContent).not.toContain(gateFrame.name.text);

      // A streamed row carries no `ts`: the frame has three lines and the dashboard adds none.
      expect(within(gate).getByTestId("trace-step-time").textContent).toBe("");

      // `loop.guardrail.tripped` reads as the guardrail that tripped, that frame's own characters.
      const guardrailFrame = frameFrom(after[4]);
      expect(guardrailFrame.name.text).toBe("loop.guardrail.tripped");
      const guardrail = streamedSteps()[4];
      const guardrailSentence = within(guardrail).getByTestId("trace-sentence");
      expect(guardrailSentence.textContent).toBe(guardrailFrame.payload.at(".guardrail").text);
      expect(guardrailSentence.querySelector("[data-figure]")?.getAttribute("data-path")).toBe(
        ".guardrail",
      );

      // Expanded, the row lists every field the `data:` line holds, each its own characters.
      const gateSpelled = sourceCharactersByPath(GATE_PAYLOAD);
      fireEvent.click(within(gate).getByRole("button", { name: "Expand" }));
      expect(fieldsOf(gate)).toEqual(
        gateFrame.paths.map((path) => ({ path, shown: gateSpelled.get(path) })),
      );
      expect(gateFrame.paths).toEqual([".answer", ".failed_open", ".reason", ".retrieve"]);
      fireEvent.click(within(gate).getByRole("button", { name: "Collapse" }));
      expect(within(gate).queryByTestId("trace-step-fields")).toBeNull();

      // The guardrail's `stop_reason` is in its fields, as the ledger spelled it (`end_turn`).
      fireEvent.click(within(guardrail).getByRole("button", { name: "Expand" }));
      expect(fieldsOf(guardrail)).toContainEqual({ path: ".stop_reason", shown: "end_turn" });
      const guardrailSpelled = sourceCharactersByPath(guardrailFrame.data);
      expect(fieldsOf(guardrail)).toEqual(
        guardrailFrame.paths.map((path) => ({ path, shown: guardrailSpelled.get(path) })),
      );
      fireEvent.click(within(guardrail).getByRole("button", { name: "Collapse" }));
      expect(figuresWithoutSource(container)).toEqual([]);
      log.push("/sessions?session=: a live row is its event's sentence and expands to every payload field");
    });

    it("quotes a refused subscription and retries nothing after it", async () => {
      answering(replaying(shortReplay(2)));
      episodes = [{ kind: "refused", status: 400, body: badCursor }];
      await renderSession(REPLAY_ID);

      expect(statusText()).toBe(COULD_NOT_CONNECT);
      expect(statusText()).not.toContain("400");
      const refusal = screen.getByTestId("stream-refusal");
      expect(refusal.textContent).toContain("The gateway refused this subscription");
      // The refusal's own body, quoted whole in the refusal's block, never summarised.
      expect(within(refusal).getByTestId("raw-body").textContent).toBe(badCursor);

      // A refusal is the gateway saying it will refuse the next one too, so there is no next one.
      const opened = cursors.length;
      await settle(BACKOFF_MS[3] as number);
      await settle(BACKOFF_MS[3] as number);
      expect(cursors.length).toBe(opened);
      log.push("/sessions?session=: a refused subscription is quoted and retried 0 times");
    });

    it("waits 1 s, then 2 s, then 4 s while the gateway will not answer", async () => {
      answering(replaying(shortReplay(1)));
      episodes = [{ kind: "stream", chunks: [], ending: "drop" }];
      await renderSession(REPLAY_ID);

      expect(statusText()).toBe(RECONNECTING);
      const waits = [BACKOFF_MS[0], BACKOFF_MS[1], BACKOFF_MS[2]] as number[];
      let opened = cursors.length;
      for (const wait of waits) {
        await settle(wait);
        await flushed();
        expect(cursors.length).toBe(opened + 1);
        expect(cursors.at(-1)).toBe("1");
        opened = cursors.length;
      }
      log.push("/sessions?session=: a gateway that will not answer is retried at 1 s, 2 s and 4 s");
    });

    it("does not pull the log back down while the operator has scrolled up", async () => {
      answering(replaying(shortReplay(1)));
      const cut = cutAt("4");
      episodes = [
        { kind: "stream", chunks: [RECORDED_FRAMES.slice(1, 4).join("")], ending: "drop" },
        { kind: "stream", chunks: [cut.after.join("")], ending: "close" },
      ];
      await renderSession(REPLAY_ID);

      const logList = screen.getByTestId("stream-log");
      Object.defineProperty(logList, "scrollHeight", { value: 800, configurable: true });
      Object.defineProperty(logList, "clientHeight", { value: 400, configurable: true });
      logList.scrollTop = 0;
      fireEvent.scroll(logList);

      // The rows the operator had not seen still arrive; the view he chose is still his.
      await settle(BACKOFF_MS[0] as number);
      await flushed();
      expect(streamedSentences()).toEqual(sentencesOf(RECORDED_FRAMES.slice(1)));
      expect(logList.scrollTop).toBe(0);
      log.push("/sessions?session=: rows keep arriving while a scrolled-up log stays where it was put");
    });
  });

  // ---- The rest of the vocabulary a live row can carry, and the two bodies that are not figures --

  describe("the states these two pages can still be put in (FR-59, `D-2`, `D-3`)", () => {
    it("reads an unknown name verbatim, a bare finish as its word, and a recorded null as `unknown`", async () => {
      // The replay is the whole recording, so these frames are the rows that land after it. Their
      // payload shapes are the ones no seeded session produces, written in `gateway/sse.py::frame`'s
      // four lines by `tests/support/stream-drops.ts` (decision 33).
      answering(replaying(EVENTS_BODY));
      episodes = [{ kind: "stream", chunks: [VOCABULARY_FRAMES.join("")], ending: "close" }];
      const { container } = await renderSession(REPLAY_ID);

      expect(cursors).toEqual(["8"]);
      expect(seqOf(VOCABULARY_FRAMES)).toEqual(["9", "10", "11", "12", "13"]);
      expect(streamedSteps()).toHaveLength(VOCABULARY_FRAMES.length);
      expect(streamedSentences()).toEqual(sentencesOf(VOCABULARY_FRAMES));

      // `eval.case.finished` has no sentence and is in no frozen taxonomy, so both case rows show
      // the name as the frame spelled it, in mono - never a pass or a fail it did not say in words.
      const [passed, failed, truncated, finished, nulled] = streamedSteps();
      for (const step of [passed, failed]) {
        expect(within(step).getByTestId("trace-verbatim").textContent).toBe("eval.case.finished");
      }
      expect(within(passed).queryByTestId("pass-mark-word")).toBeNull();
      // Expanded, each verdict is its own `passed` characters, `true` and `false` respectively.
      fireEvent.click(within(passed).getByRole("button", { name: "Expand" }));
      fireEvent.click(within(failed).getByRole("button", { name: "Expand" }));
      expect(fieldsOf(passed)).toContainEqual({ path: ".passed", shown: "true" });
      expect(fieldsOf(failed)).toContainEqual({ path: ".passed", shown: "false" });

      // A `run.finished` with no `iterations` is the bare word "Finished", whatever its stop reason
      // was: the sentence carries no figure, and the stop reason waits in the expanded fields.
      for (const step of [truncated, finished, nulled]) {
        const sentence = within(step).getByTestId("trace-sentence");
        expect(sentence.textContent).toBe("Finished");
        expect(sentence.querySelectorAll("[data-figure]")).toHaveLength(0);
      }
      fireEvent.click(within(truncated).getByRole("button", { name: "Expand" }));
      expect(fieldsOf(truncated)).toEqual([
        { path: ".guardrail", shown: "unknown" },
        { path: ".stop_reason", shown: "max_tokens" },
      ]);

      // A `null` the payload spelled is the word `unknown`, never a blank and never a zero.
      fireEvent.click(within(nulled).getByRole("button", { name: "Expand" }));
      expect(fieldsOf(nulled)).toEqual([{ path: ".stop_reason", shown: "unknown" }]);
      expect(nulled.textContent).not.toContain("null");
      expect(figuresWithoutSource(container)).toEqual([]);
      log.push("/sessions?session=: an unknown name is verbatim, a bare finish is `Finished`, a null is `unknown`");
    });

    it("reports the browser's own refusal when the dashboard itself cannot be reached", async () => {
      // Not a status: a `fetch` that rejects is the one other thing that means unreachable
      // (MUST rule 23), and the cause shown is the one the browser gave.
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: "rejects" });
      const { container } = await renderSessions();

      const body = panel(PANEL_TITLE).textContent ?? "";
      expect(body).toContain("ECONNREFUSED");
      expect(body).not.toContain(GATEWAY);
      expect(body).toContain("harnessed serve");
      expect(renderedFigures(panel(PANEL_TITLE))).toEqual([]);
      expect(figuresWithoutSource(container)).toEqual([]);
      log.push("/sessions: a rejected fetch is unreachable, with the cause the browser gave");
    });

    it("quotes a failing body that is not the error contract as itself", async () => {
      // Decision 24 says every failure is `{"error","message"}`. A body that is not gets quoted
      // whole under a class name of its own rather than being parsed into fields it does not have.
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: { status: 502, text: NOT_THE_CONTRACT } });
      await renderSessions();

      const failed = within(panel(PANEL_TITLE)).getByRole("alert");
      expect(failed.textContent).toContain("502");
      expect(failed.textContent).toContain(NOT_THE_CONTRACT);
      log.push("/sessions: a body that is not decision 24's contract is quoted whole, not parsed");
    });

    it("will not open a session page from an empty lookup", async () => {
      answering({ [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text } });
      await renderSessions();

      const lookup = screen.getByRole("region", { name: LOOKUP_LABEL });
      const field = within(lookup).getByLabelText("session id");
      fireEvent.submit(field.closest("form") as HTMLFormElement);
      expect(pushed).toEqual([]);
      log.push("/sessions: the one submission that could go nowhere cannot be made");
    });
  });
});

describe("runs", () => {
  const DASHBOARD = resolve(import.meta.dirname, "../..");
  const REPO = resolve(DASHBOARD, "..");
  const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
  const EVIDENCE = resolve(REPO, "evidence/106/runs.txt");
  const RUNS_HTML = resolve(REPO, "evidence/106/runs.html");
  const DETAIL_HTML = resolve(REPO, "evidence/106/run-detail.html");
  const CASE_RUN_HTML = resolve(REPO, "evidence/106/case-run-row.html");
  const TRACE_PANELS_HTML = resolve(REPO, "evidence/183/trace-panels.html");

  /** The gateway this test's shell is pointed at. Not the operator's 8756 (decision 30). */
  const GATEWAY = GATEWAY_ORIGIN;



  /** The lines this run writes to `evidence/106/runs.txt`. */
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

  const listing = recorded("runs.listing");
  const emptyListing = recorded("empty.runs.listing");
  const limitRefused = recorded("badrequest.runs.limit");
  const sessionRun = recorded("runs.session");
  const caseRun = recorded("runs.case-run");
  const trace = recorded("runs.trace");
  const noSuchRun = recorded("notfound.runs.run");
  const noSuchTrace = recorded("notfound.runs.trace");
  const health = recorded("health");

  /** The two keys the recordings hold, and the key none of them does. */
  const SESSION_KEY = "run_0000000002NEHPN093CBQMNNV3";
  const CASE_RUN_KEY = "eval_000000000HB774KP7M6WX8HT48:holdout-003-iso-date:1";
  const UNKNOWN_KEY = "run_00000000000000000000000000";

  /**
   * A turn that ran out of tokens, in a run written before migration 006 recorded usage. No
   * recording holds either: the fake provider always finishes its turn, and every seeded run is
   * newer than the migration. `design.md` gives both a state, so the body is written here in the
   * shape the emitter sends rather than a recorded one being edited (MUST rule 24).
   */
  const TRUNCATED_RUN = [
    '{"finished_at":"2026-01-01T12:00:00Z","iterations":4,"key_kind":"session",',
    '"reply":"the answer is",',
    `"run_key":"${SESSION_KEY}","started_at":"2026-01-01T12:00:00Z",`,
    '"stop_reason":"max_tokens","tool_calls":2,"usage":null}',
  ].join("");

  /**
   * A trace of two turns and three tool results, one of which failed. The recorded trace is a
   * one-turn `ping` with no tool call at all, so "each iteration, in order, each tool result with
   * its `ok`" has nothing it could fail against without this.
   */
  const RICH_TRACE = [
    "[",
    '{"at":"2026-01-01T12:00:00Z","iteration":1,',
    '"request":"{\\"messages\\":[{\\"content\\":\\"read the file\\",\\"role\\":\\"user\\"}]}",',
    '"response":{"content":"reading it now","stop_reason":"tool_use"},"retrieval":null,',
    `"run_id":"${SESSION_KEY}",`,
    '"tool_results":[',
    '{"arguments":"{\\"path\\":\\"notes.md\\"}","arguments_unparseable":false,',
    '"call_id":"call_one","ok":true,',
    '"output":"the notes\\nline two\\nline three\\nline four\\nline five","tool":"fs_read"},',
    '{"arguments":"{\\"path\\":\\"missing.md\\"}","arguments_unparseable":false,',
    '"call_id":"call_two","ok":false,"output":"no such file","tool":"fs_read"}]},',
    '{"at":"2026-01-01T12:00:02Z","iteration":2,',
    '"request":"{\\"messages\\":[{\\"content\\":\\"the notes\\",\\"role\\":\\"tool\\"}]}",',
    '"response":{"content":"done","stop_reason":"end_turn"},"retrieval":null,',
    `"run_id":"${SESSION_KEY}",`,
    '"tool_results":[',
    '{"arguments":"{}","arguments_unparseable":false,',
    '"call_id":"call_three","ok":true,"output":"written","tool":"fs_write"}]}',
    "]",
  ].join("");

  /**
   * A listing row the journal wrote before migration 007 recorded a stop reason. `design.md` gives
   * the Runs list this state by name ("`stop_reason: null` renders `unknown`") and no recording holds
   * it, so it is written here in the shape the emitter sends (MUST rule 24).
   */
  const LISTING_WITH_A_NULL = [
    '{"runs":[{"iterations":1,"key_kind":"session",',
    `"run_key":"${SESSION_KEY}","started_at":"2026-01-01T12:00:00Z","stop_reason":null},`,
    '{"iterations":1,"key_kind":"case_run",',
    `"run_key":"${CASE_RUN_KEY}","started_at":"2026-01-01T12:00:00Z","stop_reason":"end_turn"}]}`,
  ].join("");

  /** The same absence on the detail page, where the sentence is keyed on `.stop_reason`. */
  const RUN_WITH_A_NULL_STOP_REASON = [
    '{"finished_at":"2026-01-01T12:00:00Z","iterations":1,"key_kind":"session","reply":"pong",',
    `"run_key":"${SESSION_KEY}","started_at":"2026-01-01T12:00:00Z","stop_reason":null,`,
    '"tool_calls":0,"usage":{"cached_input_tokens":0,"input_tokens":3,"output_tokens":1}}',
  ].join("");

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

  /** Stub `fetch` with one answer per gateway route, and hand back the spy so a test can read the URLs. */
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

  /** The recorded `/health` the shell's pill polls, so no test here is about the pill. */
  const HEALTH_URL = "/v1/health";
  const HEALTH_OK: Answer = { status: health.code, text: health.text };

  /** The proxy URL a gateway route is read through. */
  function proxied(route: string): string {
    return route;
  }

  /** The listing route for a limit, as the panel spells it. */
  function listingRoute(limit: string): string {
    return `/v1/runs?limit=${limit}`;
  }

  /** The two routes the detail page reads for a key. */
  function runRoute(key: string): string {
    return `/v1/runs/${key}`;
  }

  function traceRoute(key: string): string {
    return `/v1/runs/${key}/trace`;
  }

  /** Let React settle a read. Every state change after this is a render, never a request. */
  async function settle(): Promise<void> {
    await act(async () => {});
  }

  /** The shell as `app/layout.tsx` mounts it, around whatever a page renders. */
  function shell(children: ReactNode) {
    return <AppShell>{children}</AppShell>;
  }

  /** `/runs` at a limit, rendered as the server renders it. */
  async function renderRuns(limit?: string) {
      const page = pageAt(TracesPage, limit === undefined ? {} : { limit });
    const rendered = render(shell(page));
    await settle();
    return rendered;
  }

  /** `/runs?key=`, rendered as the server renders it (FR-64). */
  async function renderDetail(runKey: string) {
    const page = pageAt(TracesPage, { key: runKey });
    const rendered = render(shell(page));
    await settle();
    return rendered;
  }

  /** The panel whose heading is `title`, so one page's two panels never answer for each other. */
  function panel(title: string): HTMLElement {
    return screen.getByRole("region", { name: title });
  }

  /**
   * The figure at a route and path, found by its provenance pair rather than by its `title`:
   * `SourceChip` carries the same `route -> path` title, and only the figure carries `data-path`.
   */
  function figureAt(scope: HTMLElement, route: string, path: string): HTMLElement {
    const found = queryFigureAt(scope, route, path);
    if (found === null) throw new Error(`no figure for ${route} -> ${path}`);
    return found;
  }

  function queryFigureAt(scope: HTMLElement, route: string, path: string): HTMLElement | null {
    return scope.querySelector<HTMLElement>(`[data-route="${route}"][data-path="${path}"]`);
  }

  /**
   * `stop_reason` and `key_kind` render `HUMAN_WORDS`'s word rather than the ledger's own characters
   * (`D-1`, `D-7`) - never for a `null`, which is still the `unknown` treatment `divergences` already
   * knows about. A figure at one of these paths is checked separately, below, rather than against the
   * wire-text walk that every other figure still owes FR-45.
   */
  const HUMANIZED_FIELD = /(^|\.)(stop_reason|key_kind)$/;

  /** Every non-humanized figure of a tree, checked against the bytes of the bodies it came from. */
  function faithful(container: ParentNode, bodies: Record<string, string>): void {
    const figures = renderedFigures(container);
    expect(figures.length).toBeGreaterThan(0);
    expect(figuresWithoutSource(container)).toEqual([]);
    expect(divergences(plainFigures(container, figures, bodies), new Map(Object.entries(bodies)))).toEqual([]);
    humanWordsAreFaithful(container, figures, bodies);
  }

  /** Every figure the wire-text walk still owes an exact match, humanized fields set aside. */
  function plainFigures(
    container: ParentNode,
    figures: readonly RenderedFigure[],
    bodies: Record<string, string>,
  ): RenderedFigure[] {
    return figures.filter((figure) => !isHumanizedNonNull(figure, bodies));
  }

  /** Is this figure a humanized field whose displayed text is a human word, not the ledger's own? */
  function isHumanizedNonNull(figure: RenderedFigure, bodies: Record<string, string>): boolean {
    if (!HUMANIZED_FIELD.test(figure.path)) return false;
    const text = bodies[figure.route];
    return text !== undefined && !nullPaths(text).has(figure.path);
  }

  /**
   * For every humanized figure, the ledger's own word still travels verbatim - in the figure's own
   * tooltip (`D-1`, `D-7`) - even though the displayed characters are the human word rather than the
   * wire's own.
   */
  function humanWordsAreFaithful(
    container: ParentNode,
    figures: readonly RenderedFigure[],
    bodies: Record<string, string>,
  ): void {
    for (const figure of figures) {
      if (!isHumanizedNonNull(figure, bodies)) continue;
      const text = bodies[figure.route] ?? "";
      const raw = sourceCharactersByPath(text).get(figure.path);
      const node = container.querySelector(
        `[data-route="${figure.route}"][data-path="${figure.path}"]`,
      );
      expect(node?.getAttribute("title"), `${figure.route} ${figure.path} lost its own word`).toContain(
        raw,
      );
    }
  }

  /**
   * `RecordView`'s "Show fields" disclosures (ticket 183): a field two levels deep or more starts
   * collapsed, and opening one can reveal another beneath it, so this clicks every visible toggle
   * and re-queries until none remain.
   */
  function revealRecordFields(scope: HTMLElement): void {
    let toggles = within(scope).queryAllByRole("button", { name: "Show fields" });
    while (toggles.length > 0) {
      for (const toggle of toggles) fireEvent.click(toggle);
      toggles = within(scope).queryAllByRole("button", { name: "Show fields" });
    }
  }

  /**
   * Every scalar `RecordView` would render out of a parsed JSON value, in the same text a
   * source-aware parse gives it (a string's own characters; anything else, `String(value)`). The
   * trace's request and arguments are plain JSON with no exotic number literals, so this plain
   * parse is faithful enough for the assertion it stands in for.
   */
  function leafTexts(value: unknown): string[] {
    if (value === null) return ["null"];
    if (Array.isArray(value)) return value.flatMap(leafTexts);
    if (typeof value === "object") return Object.values(value as Record<string, unknown>).flatMap(leafTexts);
    return [String(value)];
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

  // ---- `/runs`: the listing ----------------------------------------------------------------------

  describe("/runs lists what the journal holds, in the gateway's order (FR-55)", () => {
    it("shows every row with its kind, start, iterations and stop reason, in the sent order", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      });
      const { container } = await renderRuns();

      const table = within(panel("Runs")).getByRole("table");
      const headers = within(table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent);
      expect(headers).toEqual(["run key", "kind", "started", "iterations", "stop reason"]);

      const sent = JSON.parse(listing.text) as { runs: { run_key: string; key_kind: string }[] };
      const rows = within(table).getAllByRole("row").slice(1);
      expect(rows).toHaveLength(sent.runs.length);
      expect(rows.length).toBeGreaterThan(10);

      // The order is the response's order, row for row, and the link is the row's own key verbatim.
      // The kind reads in human words, with the ledger's own word kept in the figure's own tooltip
      // (`D-1`, `D-7`) rather than as the visible characters.
      for (const [index, row] of rows.entries()) {
        const expected = sent.runs[index];
        const key = within(row).getByRole("link");
        expect(key.textContent).toBe(expected?.run_key);
        expect(key.getAttribute("href")).toBe(runHref(expected?.run_key ?? ""));
        const kind = figureAt(row, listingRoute(DEFAULT_LIMIT), `.runs[${index}].key_kind`);
        expect(kind.textContent).toBe(humanWord("key_kind", expected?.key_kind ?? ""));
        expect(kind.getAttribute("title")).toContain(expected?.key_kind);
      }

      faithful(container, { [listingRoute(DEFAULT_LIMIT)]: listing.text });
      mkdirSync(dirname(RUNS_HTML), { recursive: true });
      writeFileSync(RUNS_HTML, container.innerHTML, "utf8");
      log.push(`/runs: ${rows.length} rows in the gateway's order, each linked by its own run key`);
    });

    it("offers 20, 50 and 100, keeps the choice in the URL and says what it is showing", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      });
      await renderRuns();

      expect([...LIMITS]).toEqual(["20", "50", "100"]);
      const control = screen.getByTestId("limit-control");
      const choices = within(control).getAllByRole("radio") as HTMLInputElement[];
      expect(choices.map((choice) => choice.value)).toEqual([...LIMITS]);
      expect(choices.filter((choice) => choice.checked)).toHaveLength(1);
      expect((within(control).getByLabelText(DEFAULT_LIMIT) as HTMLInputElement).checked).toBe(true);

      expect(screen.getByTestId("runs-footer").textContent).toBe(footerLine(DEFAULT_LIMIT));
      expect(screen.getByTestId("runs-footer").textContent).toBe(
        "showing up to 20, newest first, as the gateway ordered them",
      );

      // The choice is a navigation: it goes into the URL, and the URL is the only place it lives.
      fireEvent.click(within(control).getByLabelText("50"));
      expect(pushed).toEqual(["/traces?limit=50"]);
      cleanup();

      // And the page served at that URL reads the gateway at that limit and says so.
      const spy = answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute("50"))]: { status: listing.code, text: listing.text },
      });
      await renderRuns("50");
      expect(spy.mock.calls.map((call) => call[0])).toContain(proxied(listingRoute("50")));
      expect(screen.getByTestId("runs-footer").textContent).toBe(
        "showing up to 50, newest first, as the gateway ordered them",
      );
      log.push("/runs: the 20/50/100 control is URL state; the footer names the cap and the order");
    });

    it("names the route and `harnessed run` where the journal is empty", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: {
          status: emptyListing.code,
          text: emptyListing.text,
        },
      });
      const { container } = await renderRuns();

      const body = panel("Runs").textContent ?? "";
      expect(body).toContain("No runs recorded yet.");
      expect(body).not.toContain(listingRoute(DEFAULT_LIMIT));
      expect(body).toContain("harnessed run");
      expect(within(panel("Runs")).queryByRole("table")).toBeNull();
      expect(figuresWithoutSource(container)).toEqual([]);
      log.push("/runs: zero rows name the route that answered and the command that makes some");
    });

    it("shows the gateway's own refusal when the URL asks for a limit it will not serve", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute("nought"))]: { status: limitRefused.code, text: limitRefused.text },
      });
      await renderRuns("nought");

      const body = panel("Runs").textContent ?? "";
      expect(body).toContain("LimitError");
      expect(body).toContain("limit 'nought' is not a positive integer.");
      log.push("/runs: a limit the gateway refuses is its own 400, quoted, never a client guess");
    });

    it("renders no figure from memory when the gateway cannot be reached", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: "rejects",
      });
      const { container } = await renderRuns();

      expect(within(panel("Runs")).getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
      expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
      log.push("/runs: an unreachable gateway leaves 0 figures on the page (`D-6`)");
    });
  });

  // ---- `/runs?key=`: the two panels -----------------------------------------------------------

  describe("/runs?key= holds one run and its trace, from two routes (FR-55)", () => {
    it("reads the run from /runs/{run_key} and the trace from /runs/{run_key}/trace", async () => {
      const spy = answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: sessionRun.code, text: sessionRun.text },
        [proxied(traceRoute(SESSION_KEY))]: { status: trace.code, text: trace.text },
      });
      const { container } = await renderDetail(SESSION_KEY);

      const asked = spy.mock.calls.map((call) => call[0]);
      expect(asked).toContain(proxied(runRoute(SESSION_KEY)));
      expect(asked).toContain(proxied(traceRoute(SESSION_KEY)));
      expect(panel("Run")).toBeTruthy();
      expect(panel("Trace")).toBeTruthy();

      faithful(container, {
        [runRoute(SESSION_KEY)]: sessionRun.text,
        [traceRoute(SESSION_KEY)]: trace.text,
      });
      mkdirSync(dirname(DETAIL_HTML), { recursive: true });
      writeFileSync(DETAIL_HTML, container.innerHTML, "utf8");
      log.push("/runs?key=: two panels over two routes, every figure faithful to its own body");
    });

    it("shows usage as its three counts, each carrying its own path", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: sessionRun.code, text: sessionRun.text },
        [proxied(traceRoute(SESSION_KEY))]: { status: trace.code, text: trace.text },
      });
      await renderDetail(SESSION_KEY);

      const run = panel("Run");
      const sent = JSON.parse(sessionRun.text) as { usage: Record<string, number> };
      expect(Object.keys(sent.usage)).toHaveLength(3);
      for (const [field, count] of Object.entries(sent.usage)) {
        const figure = figureAt(run, runRoute(SESSION_KEY), `.usage.${field}`);
        expect(figure.textContent).toBe(String(count));
      }
      log.push("/runs?key=: usage is three counts, each one click from `.usage.<field>`");
    });

    it("says unknown, and why, where the run recorded no usage and ran out of tokens", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: 200, text: TRUNCATED_RUN },
        [proxied(traceRoute(SESSION_KEY))]: { status: trace.code, text: trace.text },
      });
      const { container } = await renderDetail(SESSION_KEY);

      const run = panel("Run");
      const reason = nullReasonFor(runRoute(SESSION_KEY), ".usage");
      expect(reason).toBe("recorded before Harnessed measured this, so a partial sum would understate the run");
      const usage = figureAt(run, runRoute(SESSION_KEY), ".usage");
      expect(usage.textContent).toBe("unknown");
      expect(run.textContent).toContain(reason);
      // The absence is never a zero, and the counts that were not sent are not invented.
      expect(queryFigureAt(run, runRoute(SESSION_KEY), ".usage.input_tokens")).toBeNull();
      expect(run.textContent).not.toContain("0 tokens");

      // And the turn that was cut off says so in words, not in a colour alone - the human word is
      // what shows, with the ledger's own `max_tokens` kept in the figure's tooltip (`D-1`, `D-7`).
      expect(run.textContent).toContain(humanWord("stop_reason", "max_tokens"));
      expect(run.textContent).toContain(TRUNCATED);
      expect(TRUNCATED).toBe("truncated");
      expect(figureAt(run, runRoute(SESSION_KEY), ".stop_reason").getAttribute("title")).toContain(
        "max_tokens",
      );

      faithful(container, {
        [runRoute(SESSION_KEY)]: TRUNCATED_RUN,
        [traceRoute(SESSION_KEY)]: trace.text,
      });
      log.push("/runs?key=: `usage: null` is one explained `unknown`; `max_tokens` reads truncated");
    });

    it("walks the trace turn by turn: request, response and each tool result with its ok", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: sessionRun.code, text: sessionRun.text },
        [proxied(traceRoute(SESSION_KEY))]: { status: 200, text: RICH_TRACE },
      });
      const { container } = await renderDetail(SESSION_KEY);

      const turns = within(panel("Trace")).getAllByTestId("trace-turn");
      const sent = JSON.parse(RICH_TRACE) as {
        iteration: number;
        request: string;
        response: { content: string; stop_reason: string };
        tool_results: { call_id: string; tool: string; ok: boolean; output: string }[];
      }[];
      expect(turns).toHaveLength(sent.length);

      for (const [index, turn] of turns.entries()) {
        const expected = sent[index];
        if (expected === undefined) throw new Error("the trace lost a turn");
        // The request renders through `RecordView` (kind `record`): every disclosure two levels
        // deep or more is revealed, so a value nested inside `messages[0]` is on the page to check,
        // rather than compared against the four collapsed lines `JsonBlock` used to start with.
        revealRecordFields(turn);
        const text = turn.textContent ?? "";
        expect(text).toContain(String(expected.iteration));
        for (const value of leafTexts(JSON.parse(expected.request) as unknown)) {
          expect(text).toContain(value);
        }
        expect(text).toContain(expected.response.content);
        // The stop reason reads in human words; the ledger's own word stays reachable in the
        // figure's own tooltip (`D-1`, `D-7`) rather than as the visible characters.
        const stopFigure = figureAt(
          turn,
          traceRoute(SESSION_KEY),
          `[${index}].response.stop_reason`,
        );
        expect(stopFigure.textContent).toBe(humanWord("stop_reason", expected.response.stop_reason));
        expect(stopFigure.getAttribute("title")).toContain(expected.response.stop_reason);
        const results = within(turn).getAllByTestId("tool-result");
        expect(results).toHaveLength(expected.tool_results.length);
        for (const [row, result] of results.entries()) {
          const call = expected.tool_results[row];
          if (call === undefined) throw new Error("the trace lost a tool result");
          const shown = result.textContent ?? "";
          expect(shown).toContain(call.tool);
          expect(shown).toContain(call.call_id);
          expect(shown).toContain(call.output);
          expect(shown).toContain(`ok: ${String(call.ok)}`);
        }
      }

      faithful(container, {
        [runRoute(SESSION_KEY)]: sessionRun.text,
        [traceRoute(SESSION_KEY)]: RICH_TRACE,
      });
      mkdirSync(dirname(TRACE_PANELS_HTML), { recursive: true });
      writeFileSync(TRACE_PANELS_HTML, panel("Trace").innerHTML, "utf8");
      log.push(`trace: ${turns.length} turns in order, 3 tool results, each with its own \`ok\``);
      log.push("trace: request, response, arguments and output each render through `OutputView`");
    });

    it("says why a trace carries no retrieval, and never that there was none", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: sessionRun.code, text: sessionRun.text },
        [proxied(traceRoute(SESSION_KEY))]: { status: trace.code, text: trace.text },
      });
      await renderDetail(SESSION_KEY);

      const turn = within(panel("Trace")).getAllByTestId("trace-turn").at(0) as HTMLElement;
      const retrieval = figureAt(turn, traceRoute(SESSION_KEY), "[0].retrieval");
      expect(retrieval.textContent).toBe("unknown");
      expect(turn.textContent).toContain("the gateway does not pass retrieval to the trace");
      expect(turn.textContent).not.toContain("no retrieval");
      expect(nullReasonFor(traceRoute(SESSION_KEY), "[0].retrieval")).toBe(
        "the gateway does not pass retrieval to the trace",
      );
      log.push("trace: `retrieval: null` is `unknown` with `[GAP G-5]`'s sentence, never `none`");
    });

    it("links a session run to its events, using the run key exactly as the gateway sent it", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: sessionRun.code, text: sessionRun.text },
        [proxied(traceRoute(SESSION_KEY))]: { status: trace.code, text: trace.text },
      });
      await renderDetail(SESSION_KEY);

      const run = panel("Run");
      const kind = figureAt(run, runRoute(SESSION_KEY), ".key_kind");
      expect(kind.textContent).toBe(humanWord("key_kind", "session"));
      expect(kind.getAttribute("title")).toContain("session");
      const events = within(run).getByTestId("session-events-link");
      const sent = JSON.parse(sessionRun.text) as { run_key: string };
      expect(events.getAttribute("href")).toBe(sessionHref(sent.run_key));
      expect(sessionHref(sent.run_key)).toBe(sessionHref(SESSION_KEY));
      log.push("/runs?key=: a session links to `/sessions?session=<run_key>`, the field verbatim");
    });

    it("names a case run as one case of an eval run and offers no link to that run (G-4)", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(CASE_RUN_KEY))]: { status: caseRun.code, text: caseRun.text },
        [proxied(traceRoute(CASE_RUN_KEY))]: { status: noSuchTrace.code, text: noSuchTrace.text },
      });
      const { container } = await renderDetail(CASE_RUN_KEY);

      const run = panel("Run");
      const kind = figureAt(run, runRoute(CASE_RUN_KEY), ".key_kind");
      expect(kind.textContent).toBe(humanWord("key_kind", "case_run"));
      expect(kind.getAttribute("title")).toContain("case_run");
      expect(within(run).getByTestId("run-kind-note").textContent).toContain(
        "one case of an eval run",
      );
      expect(within(run).queryByTestId("session-events-link")).toBeNull();

      // No link out of this panel goes to an eval run, and no id was split to try (MUST rule 25).
      for (const link of within(run).queryAllByRole("link")) {
        expect(link.getAttribute("href")).not.toContain("/evals/");
      }
      // The key is rendered whole: the `:` separators are still in the figure on the page.
      expect(figureAt(run, runRoute(CASE_RUN_KEY), ".run_key").textContent).toBe(CASE_RUN_KEY);

      mkdirSync(dirname(CASE_RUN_HTML), { recursive: true });
      writeFileSync(CASE_RUN_HTML, container.innerHTML, "utf8");
      log.push("/runs?key=: a `case_run` is named as one case, with no eval run link (`[GAP G-4]`)");
    });

    it("shows both panels' 404 bodies where the key is one the ledgers never held", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(UNKNOWN_KEY))]: { status: noSuchRun.code, text: noSuchRun.text },
        [proxied(traceRoute(UNKNOWN_KEY))]: { status: noSuchTrace.code, text: noSuchTrace.text },
      });
      const { container } = await renderDetail(UNKNOWN_KEY);

      expect(screen.getAllByRole("alert").length).toBeGreaterThan(1);
      const run = panel("Run").textContent ?? "";
      const traced = panel("Trace").textContent ?? "";
      expect(run).toContain("SurfaceNotFound");
      expect(run).toContain(
        "no loop run under 'run_00000000000000000000000000': the run journal holds no turn for that session key.",
      );
      expect(traced).toContain("SurfaceNotFound");
      expect(traced).toContain(
        "no loop run 'run_00000000000000000000000000': the ledgers hold nothing under that id.",
      );
      expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
      log.push("/runs?key=: an unheld key is both panels' `AbsentState`, quoting the gateway");
    });
  });

  // ---- The page walk both pages owe FR-51 ---------------------------------------------------------

  describe("every figure on either page says where it came from (FR-51)", () => {
    it("leaves 0 figures without a source and 0 characters that differ from the wire", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      });
      const listed = await renderRuns();
      const onList = renderedFigures(listed.container);
      faithful(listed.container, { [listingRoute(DEFAULT_LIMIT)]: listing.text });
      cleanup();

      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(CASE_RUN_KEY))]: { status: caseRun.code, text: caseRun.text },
        [proxied(traceRoute(CASE_RUN_KEY))]: { status: 200, text: RICH_TRACE },
      });
      const detail = await renderDetail(CASE_RUN_KEY);
      const onDetail = renderedFigures(detail.container);
      faithful(detail.container, {
        [runRoute(CASE_RUN_KEY)]: caseRun.text,
        [traceRoute(CASE_RUN_KEY)]: RICH_TRACE,
      });

      expect(onList.length).toBeGreaterThan(40);
      expect(onDetail.length).toBeGreaterThan(20);
      log.push(
        `walk: ${onList.length} figures on /runs and ${onDetail.length} on /runs?key=, 0 unsourced`,
      );
    });

    it("opens the response a figure came from, from the figure itself, and closes on Esc", async () => {
      vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: sessionRun.code, text: sessionRun.text },
        [proxied(traceRoute(SESSION_KEY))]: { status: trace.code, text: trace.text },
      });
      const { container } = await renderDetail(SESSION_KEY);

      const trigger = figureAt(panel("Run"), runRoute(SESSION_KEY), ".run_key");
      fireEvent.keyDown(trigger, { key: "Enter" });
      const dialog = screen.getByRole("dialog");

      // Labelled "Where this came from", never the route - and no endpoint string anywhere in it.
      expect(within(dialog).getByRole("heading").textContent).toBe("Where this came from");
      expect(dialog.textContent).not.toContain(runRoute(SESSION_KEY));
      expect(dialog.textContent).not.toContain("GET");
      expect(within(dialog).getByTestId("cli-twin").textContent).toBe("harnessed run --json");
      expect(dialog.getAttribute("data-motion")).toBe("slide");

      // The record is this response's own bytes, with the figure's path highlighted inside them.
      expect(within(dialog).getByTestId("record-view")).toBeTruthy();
      expect(within(dialog).getByTestId("exact-value").textContent).toBe(SESSION_KEY);
      expect(within(dialog).getByTestId("cited").textContent).toBe(SESSION_KEY);
      expect(figuresWithoutSource(container)).toEqual([]);

      // The trap steers the two edges and nothing else, and Esc hands focus back to the trigger.
      const controls = within(dialog).getAllByRole("button");
      const first = controls.at(0) as HTMLElement;
      const last = controls.at(-1) as HTMLElement;
      last.focus();
      fireEvent.keyDown(dialog, { key: "Tab" });
      expect(document.activeElement).toBe(first);
      fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
      expect(document.activeElement).toBe(last);
      fireEvent.keyDown(dialog, { key: "ArrowDown" });

      // The CLI twin is the one thing an operator takes away from here, so it is copyable.
      const written: string[] = [];
      vi.stubGlobal("navigator", {
        clipboard: {
          writeText: async (line: string) => {
            written.push(line);
          },
        },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Copy" }));
      expect(written).toEqual(["harnessed run --json"]);

      fireEvent.keyDown(document, { key: "Escape" });
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(document.activeElement).toBe(trigger);
      log.push("/runs?key=: a figure opens the record it was read out of, with its CLI twin");
    });
  });

  // ---- The seven states design.md lets no panel skip -----------------------------------------------

  describe("both panels wear every state, and remember nothing between them (design.md)", () => {
    it("is busy before an answer and holds the answer after it", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: sessionRun.code, text: sessionRun.text },
        [proxied(traceRoute(SESSION_KEY))]: { status: trace.code, text: trace.text },
      });
      const page = pageAt(TracesPage, { key: SESSION_KEY });
      render(shell(page));

      expect(panel("Run").getAttribute("aria-busy")).toBe("true");
      expect(screen.getAllByTestId("skeleton-row").length).toBeGreaterThan(0);

      await settle();
      expect(panel("Run").getAttribute("aria-busy")).toBe("false");
      expect(screen.queryAllByTestId("skeleton-row")).toEqual([]);
      log.push("panels: `loading` is the busy skeleton, and the answer replaces it");
    });

    it("says which route, status and byte count it is showing, on the dashboard's own clock", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      });
      await renderRuns();

      const footer = panel("Runs").textContent ?? "";
      expect(footer).toContain("Updated 0 min ago");
      expect(footer).not.toContain(listingRoute(DEFAULT_LIMIT));
      expect(footer).not.toContain("GET");
      expect(footer).not.toContain("200");
      expect(footer).not.toContain("bytes");
      expect(footer).not.toContain("fetched");
      log.push("panels: the footer says only how long ago it fetched, on the dashboard's own clock");
    });

    // Slow by nature: renders the page twice across a five-minute fake-clock jump; 3.6 s on the operator's PC.
    it("goes stale five minutes after it fetched, and a refetch clears the badge", { timeout: 15_000 }, async () => {
      vi.useFakeTimers();
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      });
      const page = pageAt(TracesPage, {});
      render(shell(page));

      // Settle the read first: the elapsed time this test means is time after the answer arrived.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
      expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
      });
      const badge = screen.getByTestId("stale-badge");
      expect(badge.textContent).toBe("Updated 5 min ago");
      expect(badge.getAttribute("data-warn")).toBe("true");
      expect(within(panel("Runs")).getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

      // A minute more moves the badge and leaves the announcement where it crossed (ticket 103).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60 * 1000);
      });
      expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 6 min ago");
      expect(within(panel("Runs")).getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
      expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");
      expect(within(panel("Runs")).getByTestId("panel-announcement").textContent).toBe("");
      log.push("panels: `stale` at five minutes, announced once, cleared by `Refresh`");
    });

    it("is unreachable where the browser's own fetch is refused, not only where a header says so", async () => {
      answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(listingRoute(DEFAULT_LIMIT))]: "rejects" });
      const { container } = await renderRuns();

      expect(within(panel("Runs")).getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
      expect(within(panel("Runs")).getByTestId("unreachable-cause").textContent).toBe("ECONNREFUSED");
      expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
      log.push("panels: a refused browser fetch is `unreachable` too, with the cause it gave");
    });

    it("quotes a body that is not the error contract as itself, and offers the read again", async () => {
      const spy = answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: 500, text: "the gateway fell over" },
      });
      await renderRuns();

      const body = panel("Runs").textContent ?? "";
      expect(body).toContain("UnreadableBody");
      expect(body).toContain("the gateway fell over");

      // The state's own `Refetch` is the first of the two: the panel's footer offers one as well.
      const before = spy.mock.calls.length;
      await act(async () => {
        fireEvent.click(
          within(panel("Runs")).getAllByRole("button", { name: "Refetch" }).at(0) as HTMLElement,
        );
      });
      expect(spy.mock.calls.length).toBeGreaterThan(before);
      log.push("panels: a body outside decision 24's contract is quoted as itself, and read again");
    });
  });

  // ---- The absences `design.md` gives these two pages by name --------------------------------------

  describe("a stop reason the ledger never recorded is unknown, and says why (`D-2`)", () => {
    it("renders a listing row's null as `unknown` with the migration sentence, never as a blank", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: 200, text: LISTING_WITH_A_NULL },
      });
      const { container } = await renderRuns();

      const table = within(panel("Runs")).getByRole("table");
      const [first, second] = within(table).getAllByRole("row").slice(1);
      const unknown = figureAt(
        first as HTMLElement,
        listingRoute(DEFAULT_LIMIT),
        ".runs[0].stop_reason",
      );
      expect(unknown.textContent).toBe("unknown");
      expect(first?.textContent).toContain("recorded before Harnessed measured this");
      expect(first?.textContent).not.toContain(TRUNCATED);
      // The row beside it recorded one, and reads as the human word - the journal's own spelling
      // stays reachable in the figure's own tooltip (`D-1`, `D-7`), never as the visible text.
      const recorded = figureAt(second as HTMLElement, listingRoute(DEFAULT_LIMIT), ".runs[1].stop_reason");
      expect(recorded.textContent).toBe(humanWord("stop_reason", "end_turn"));
      expect(recorded.getAttribute("title")).toContain("end_turn");

      faithful(container, { [listingRoute(DEFAULT_LIMIT)]: LISTING_WITH_A_NULL });
      log.push("/runs: a row with no stop reason says `unknown` and the migration it predates");
    });

    it("renders the same null on the detail page, keyed on its own path", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: 200, text: RUN_WITH_A_NULL_STOP_REASON },
        [proxied(traceRoute(SESSION_KEY))]: { status: trace.code, text: trace.text },
      });
      const { container } = await renderDetail(SESSION_KEY);

      const run = panel("Run");
      expect(figureAt(run, runRoute(SESSION_KEY), ".stop_reason").textContent).toBe("unknown");
      expect(run.textContent).toContain("recorded before Harnessed measured this");
      faithful(container, {
        [runRoute(SESSION_KEY)]: RUN_WITH_A_NULL_STOP_REASON,
        [traceRoute(SESSION_KEY)]: trace.text,
      });
      log.push("/runs?key=: the same absence, keyed on `.stop_reason` rather than on a row");
    });

    it("opens a source on every panel, and says nothing about a CLI twin where there is none", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: { status: sessionRun.code, text: sessionRun.text },
        [proxied(traceRoute(SESSION_KEY))]: { status: 200, text: RICH_TRACE },
      });
      await renderDetail(SESSION_KEY);

      // `harnessed runs trace` prints a trace since ticket 232, so the drawer offers it beneath the
      // record.
      const trigger = figureAt(panel("Trace"), traceRoute(SESSION_KEY), "[0].iteration");
      // Enter opens a figure; nothing else does, so it is not a trap for any other key.
      fireEvent.keyDown(trigger, { key: "ArrowRight" });
      expect(screen.queryByRole("dialog")).toBeNull();
      fireEvent.click(trigger);
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByTestId("cli-twin").textContent).toBe(
        "harnessed runs trace <run_key> --json",
      );
      // No motion setting to read is an operator who has not asked for motion (design.md).
      expect(dialog.getAttribute("data-motion")).toBe("instant");
      // The record is this response's own bytes: a trace arrives as one line and stays one line.
      expect(within(dialog).getByTestId("record-view")).toBeTruthy();
      expect(within(dialog).getByTestId("cited").textContent).toBe("1");
      fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
      expect(screen.queryByRole("dialog")).toBeNull();
      log.push("trace: its own source opens with the record and no twin, because none exists");
    });
  });

  // ---- What the shell says while a runs panel reads ------------------------------------------------

  describe("the pill speaks for its own poll, and the panel for its own read (decision 34)", () => {
    it("says the gateway answers while the listing renders", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      });
      await renderRuns();

      const pill = screen.getByTestId("gateway-status-pill");
      expect(pill.getAttribute("data-state")).toBe("reachable");
      expect(pill.textContent).toContain("gateway reachable");
      expect(screen.getByTestId("pill-announcement").textContent).toBe("gateway reachable");

      // And it opens the answer it is speaking for, which cites no path: it speaks for the whole.
      fireEvent.click(pill);
      const dialog = screen.getByRole("dialog");
      const sent = JSON.parse(health.text) as { route_count: number };
      expect(within(dialog).getByTestId("record-view").textContent).toContain(String(sent.route_count));
      expect(within(dialog).queryByTestId("cited")).toBeNull();
      log.push("/runs: the pill reads `gateway reachable` and opens the `/health` it read");
    });

    it("keeps saying what its own poll found when a data route is the thing that failed", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: "rejects",
      });
      await renderRuns();

      // The panel lost the gateway; the pill's own 15 s poll found it, and says so (MUST rule 12:
      // a window that reported the panel's failure as the gateway's would be reporting a guess).
      expect(within(panel("Runs")).getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
      expect(screen.getByTestId("gateway-status-pill").getAttribute("data-state")).toBe("reachable");
      log.push("/runs: a failed data read never rewrites the pill, which reads its own poll");
    });

    it("says unreachable, and then error, when it is the poll that fails", async () => {
      answersWith({
        [HEALTH_URL]: "rejects",
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      });
      await renderRuns();

      const pill = screen.getByTestId("gateway-status-pill");
      expect(pill.getAttribute("data-state")).toBe("unreachable");
      expect(pill.textContent).toContain("gateway unreachable");
      // With nothing to show, the pill opens no drawer at all rather than an empty one.
      expect((pill as HTMLButtonElement).disabled).toBe(true);
      cleanup();

      answersWith({
        [HEALTH_URL]: { status: 500, text: '{"error":"InternalError","message":"it failed"}' },
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      });
      await renderRuns();
      const failed = screen.getByTestId("gateway-status-pill");
      expect(failed.getAttribute("data-state")).toBe("error");
      expect(failed.textContent).toContain("gateway error 500");
      log.push("/runs: the pill's four words are its own poll's, never a panel's");
    });
  });

  // ---- The shell these two pages are served inside -------------------------------------------------

  describe("the pages name the gateway the operator's environment names", () => {
    it("puts the relative origin in the line an operator copies", async () => {
      // `AppShell` provides `/v1` to every panel on the page (decisions 43, 44), and every `fetch`
      // it drives is at that relative path. No rendered text names an endpoint any more (`D-9`)
      // and `UnreachableState` no longer names the URL it tried either (FR-126).
      const spy = answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(runRoute(SESSION_KEY))]: "rejects",
        [proxied(traceRoute(SESSION_KEY))]: { status: trace.code, text: trace.text },
      });
      const page = pageAt(TracesPage, { key: SESSION_KEY });
      render(
        <AppShell>{page}</AppShell>,
      );
      await settle();

      const drawn = panel("Run").textContent ?? "";
      expect(within(panel("Run")).getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
      expect(drawn).not.toContain(runRoute(SESSION_KEY));
      expect(drawn).not.toContain("http://");
      expect(drawn).not.toContain(window.location.origin);

      const asked = spy.mock.calls.map((call) => call[0] as string);
      expect(asked).toContain(proxied(runRoute(SESSION_KEY)));
      for (const url of asked) {
        expect(url.startsWith(GATEWAY)).toBe(true);
        expect(url).not.toContain("http://");
      }
      log.push("/runs?key=: every read, and the line an operator sees, name the relative /v1 origin");
    });

    it("is navigable where the sidebar has become a menu button", async () => {
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      });
      await renderRuns();

      const menu = screen.getByRole("button", { name: "Menu" });
      const nav = screen.getByRole("navigation");
      expect(nav.getAttribute("data-open")).toBe("false");
      fireEvent.click(menu);
      expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("true");
      expect(within(screen.getByRole("navigation")).getByRole("link", { name: "Traces" })).toBeTruthy();
      log.push("/runs: the page is reachable from the narrow menu as well as from the sidebar");
    });
  });

  // ---- Evidence ----------------------------------------------------------------------------------

  describe("evidence", () => {
    it("writes the manifest this ticket declares", () => {
      expect(log.length).toBeGreaterThan(10);
      mkdirSync(dirname(EVIDENCE), { recursive: true });
      writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
    });
  });
});

// ---- Ticket 206: one page for both ---------------------------------------------------------------

describe("/traces stacks the sessions and the runs, and the old pages forward to it (ticket 206)", () => {
  const GATEWAY_FIXTURES_206 = resolve(import.meta.dirname, "../fixtures/gateway");

  function body(name: string): { status: number; text: string } {
    const meta = JSON.parse(readFileSync(resolve(GATEWAY_FIXTURES_206, `${name}.meta.json`), "utf8")) as {
      status: number;
    };
    return { status: meta.status, text: readFileSync(resolve(GATEWAY_FIXTURES_206, `${name}.body`), "utf8") };
  }

  let asked: string[] = [];

  function serve(routes: Record<string, string>): void {
    asked = [];
    vi.stubGlobal("matchMedia", (media: string) => ({ matches: false, media }));
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        asked.push(url);
        const found = routes[url];
        if (found === undefined) return Promise.reject(new Error(`no answer stubbed for ${url}`));
        const recorded = body(found);
        return Promise.resolve(
          new Response(recorded.text, { status: recorded.status, headers: { "content-type": "application/json" } }),
        );
      }),
    );
  }

  async function settled(): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }

  beforeEach(() => {
    vi.useFakeTimers();
    pushed = [];
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("lists Traces in the nav in its place, and 0 entries named Runs or Sessions", async () => {
    serve({ "/health": "health", "/v1/sessions?limit=20": "sessions.listing", "/v1/runs?limit=20": "runs.listing" });
    render(<AppShell>{pageAt(TracesPage, {})}</AppShell>);
    await settled();
    const labels = within(screen.getByRole("navigation"))
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(labels).toEqual([
      "Overview",
      "Console",
      "Evals",
      "Memory",
      "Traces",
      "Models",
    ]);
    expect(labels.filter((label) => label === "Runs" || label === "Sessions")).toHaveLength(0);
  });

  it("shows a Sessions section and a Runs section, driven by the one ?limit=", async () => {
    serve({
      "/health": "health",
      "/v1/sessions?limit=50": "sessions.listing",
      "/v1/runs?limit=50": "runs.listing",
    });
    render(<AppShell>{pageAt(TracesPage, { limit: "50" })}</AppShell>);
    await settled();
    expect(within(screen.getByRole("region", { name: "Sessions" })).getByRole("table")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Runs" })).getByRole("table")).toBeTruthy();
    expect(asked).toContain("/v1/sessions?limit=50");
    expect(asked).toContain("/v1/runs?limit=50");
    expect(screen.getAllByTestId("limit-control")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: "Traces" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Open a session" })).toBeTruthy();
    fireEvent.click(within(screen.getByTestId("limit-control")).getByLabelText("100"));
    expect(pushed).toEqual(["/traces?limit=100"]);
  });

  it("points every listing link at /traces with the same parameter", () => {
    expect(listingHref("50")).toBe("/traces?limit=50");
    expect(sessionHref("run_A:b")).toBe("/traces?session=run_A%3Ab");
    expect(runHref("case:1:2:3")).toBe("/traces?key=case%3A1%3A2%3A3");
  });

  it.each([
    ["/sessions", SessionsForward, { session: "run_X" }, "/traces?session=run_X"],
    ["/sessions", SessionsForward, { limit: "50" }, "/traces?limit=50"],
    ["/sessions", SessionsForward, {}, "/traces"],
    ["/runs", RunsForward, { key: "case:1:2:3" }, "/traces?key=case%3A1%3A2%3A3"],
    ["/runs", RunsForward, {}, "/traces"],
  ])("%s forwards to %s with the same query, after rendering a Moved to Traces link", (_from, Page, query, to) => {
    render(pageAt(Page, query));
    const link = screen.getByRole("link", { name: "Moved to Traces" });
    expect(link.getAttribute("href")).toBe(to);
    expect(pushed).toEqual([to]);
  });
});

// ---- Ticket 254: Schedule and Tasks above Sessions (FR-155, UJ-15, UJ-27) -------------------------------

describe("Traces lists schedules and tasks above sessions (FR-155)", () => {
  const FIXTURES = resolve(import.meta.dirname, "../fixtures/gateway");
  const body = (name: string) => readFileSync(resolve(FIXTURES, `${name}.body`), "utf8");

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-01-01T14:00:00Z"));
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  async function renderTraces(routes: Record<string, string>) {
    const served: Record<string, string> = { "/v1/health": body("health"), ...routes };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const text = served[url];
        if (text === undefined) throw new Error(`no answer stubbed for ${url}`);
        return {
          status: 200,
          ok: true,
          headers: { get: () => null },
          text: async () => text,
        } as unknown as Response;
      }),
    );
    const rendered = render(<AppShell>{pageAt(TracesPage)}</AppShell>);
    await act(async () => {});
    return rendered;
  }

  it("puts ScheduleStrip first, then Tasks, then Sessions", async () => {
    await renderTraces({
      "/v1/schedules": body("tasked.schedules.listing"),
      "/v1/tasks?limit=20": body("tasked.tasks.listing"),
      "/v1/sessions?limit=20": body("sessions.listing"),
    });
    const [schedule, tasks, sessions] = ["Schedule", "Tasks", "Sessions"].map((name) =>
      screen.getByRole("region", { name }),
    );
    expect(schedule.compareDocumentPosition(tasks) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(tasks.compareDocumentPosition(sessions) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("reads a schedule's served words, installed state, last run, next due and the copy line", async () => {
    await renderTraces({ "/v1/schedules": body("tasked.schedules.listing") });
    const row = screen.getByTestId("schedule-row");
    expect(row.textContent).toContain("Every 6 h");
    expect(row.textContent).toContain("Installed in Task Scheduler");
    expect(row.textContent).toMatch(/last ran \d\d:\d\d/);
    expect(row.textContent).toMatch(/next due \d\d:\d\d/);
    expect(screen.getByTestId("schedule-command").textContent).toBe("harnessed schedule list --json");
    expect(screen.queryByTestId("schedule-none")).toBeNull();
  });

  it("words the launchd and systemd backends a schedule is recorded under", async () => {
    const first = await renderTraces({ "/v1/schedules": body("tasked.schedules.launchd") });
    expect(screen.getByTestId("schedule-row").textContent).toContain("launchd");
    first.unmount();
    await renderTraces({ "/v1/schedules": body("tasked.schedules.systemd") });
    expect(screen.getByTestId("schedule-row").textContent).toContain("systemd user timer");
  });

  it("says Not installed for an uninstalled schedule, and offers the add command for none", async () => {
    const uninstalled = JSON.stringify({
      schedules: [
        {
          installed: "false",
          last_run_at: null,
          next_due_at: null,
          rule_kind: "every",
          rule_value: "6h",
          rule_words: "Every 6 h",
          schedule_id: "sch_x",
          where: "task_scheduler",
        },
      ],
    });
    const first = await renderTraces({ "/v1/schedules": uninstalled });
    const row = screen.getByTestId("schedule-row");
    expect(row.textContent).toContain("Not installed");
    expect(row.textContent).not.toContain("next due");
    // Ticket 272: the word is a Value figure inside the warn span, and the figure takes the span's tone.
    const figure = within(row).getByText("Not installed").closest("[data-figure]");
    expect(figure).not.toBeNull();
    expect(figure?.closest("span[class*='warn']")).not.toBeNull();
    expect(readFileSync(resolve(__dirname, "../../components/ScheduleStrip.module.css"), "utf8")).toMatch(
      /\.warn \{[^}]*--value-tone: var\(--color-warn\)/,
    );
    expect(readFileSync(resolve(__dirname, "../../components/Value.module.css"), "utf8")).toMatch(
      /\.figure \{[^}]*color: var\(--value-tone, var\(--color-fg\)\)/,
    );
    first.unmount();
    await renderTraces({ "/v1/schedules": body("empty.schedules") });
    expect(screen.getByTestId("schedule-none").textContent).toBe(
      "No schedule. Add one with harnessed schedule add --every 6h",
    );
  });

  it("lists tasks as served with their words, handler, outcome, cost, time and an Open link", async () => {
    await renderTraces({ "/v1/tasks?limit=20": body("tasked.tasks.listing") });
    const rows = screen.getAllByTestId("task-row");
    expect(rows).toHaveLength(
      (JSON.parse(body("tasked.tasks.listing")) as { tasks: unknown[] }).tasks.length,
    );
    expect(rows[0].textContent).toContain("summarise the repository");
    expect(rows[0].textContent).toContain("Done");
    expect(rows[0].textContent).toContain("Finished");
    expect(rows[0].textContent).toContain("started by a schedule");
    expect(rows[0].textContent).toContain("agent");
    expect(rows[0].textContent).toContain("Routed");
    expect(rows[0].textContent).toContain("$0.012000");
    expect(rows[0].textContent).toContain("2 h ago");
    expect(within(rows[0]).getByRole("link", { name: "Open summarise the repository" }).getAttribute("href")).toBe(
      "/console?session=run_00000000000000000TASKDONE",
    );
    expect(rows[1].textContent).toContain("Paused: the router was not sure");
    expect(rows[2].textContent).toContain("Waiting for your answer to an agent's request");
    expect(rows[1].textContent).toContain("started by you, by hand");
  });

  it("reads a queued task as Not started yet, with no Open link", async () => {
    const queued = JSON.parse(body("tasked.tasks.listing")) as { tasks: Record<string, unknown>[] };
    queued.tasks = [{ ...queued.tasks[0], state: "queued", handler: null, outcome: null, stop_reason: null }];
    await renderTraces({ "/v1/tasks?limit=20": JSON.stringify(queued) });
    const row = screen.getByTestId("task-row");
    expect(row.textContent).toContain("Queued");
    expect(row.textContent).toContain("Not started yet");
    expect(within(row).queryByRole("link")).toBeNull();
  });

  it("offers `harnessed task add --help` for no tasks, with no placeholder", async () => {
    await renderTraces({ "/v1/tasks?limit=20": body("empty.tasks") });
    expect(screen.getByTestId("tasks-empty").textContent).toContain("No tasks yet. Queue one with:");
    expect(screen.getByTestId("tasks-command").textContent).toBe("harnessed task add --help");
  });

  it("words a one-event consolidation session as Memory consolidated", async () => {
    await renderTraces({ "/v1/sessions?limit=20": body("sessions.listing.consolidated") });
    const table = screen.getByRole("table", { name: /Sessions/ });
    expect(within(table).getAllByText("Memory consolidated").length).toBeGreaterThanOrEqual(2);
  });

  const TABBABLE = "button, a[href], [tabindex]:not([tabindex='-1'])";

  it("makes a task row one Tab stop, its headline figure; the rest take tabindex -1 (decision 103)", async () => {
    await renderTraces({ "/v1/tasks?limit=20": body("tasked.tasks.listing") });
    for (const row of screen.getAllByTestId("task-row")) {
      const stops = [...row.querySelectorAll<HTMLElement>(TABBABLE)].filter((el) => el.getAttribute("tabindex") !== "-1");
      expect(stops).toHaveLength(1);
      expect(stops[0].textContent).toBe(row.querySelector("p")?.textContent);
    }
  });

  it("says Failed once in a failed task's row (ticket 305, H2)", async () => {
    await renderTraces({ "/v1/tasks?limit=20": body("tasked.tasks.failed") });
    const row = screen.getByTestId("task-row");
    expect(row.textContent?.match(/failed/gi) ?? []).toHaveLength(1);
  });

  it("says Failed once in a failed row whose stop reason is an error (ticket 305, H2)", async () => {
    const failed = JSON.parse(body("tasked.tasks.failed")) as { tasks: Record<string, unknown>[] };
    failed.tasks[0].stop_reason = "raised";
    await renderTraces({ "/v1/tasks?limit=20": JSON.stringify(failed) });
    const row = screen.getByTestId("task-row");
    expect(row.textContent?.match(/failed/gi) ?? []).toHaveLength(1);
  });

  it("makes a session row one Tab stop, its id link, with its figures at tabindex -1", async () => {
    await renderTraces({ "/v1/sessions?limit=20": body("sessions.listing.consolidated") });
    const table = screen.getByRole("table", { name: /Sessions/ });
    for (const row of within(table).getAllByRole("row").slice(1)) {
      const stops = [...row.querySelectorAll<HTMLElement>(TABBABLE)].filter((el) => el.getAttribute("tabindex") !== "-1");
      expect(stops).toHaveLength(1);
      expect(stops[0].tagName).toBe("A");
    }
  });
});
