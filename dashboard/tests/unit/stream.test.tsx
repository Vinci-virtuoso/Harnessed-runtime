// @vitest-environment jsdom
/**
 * FR-59, the stream half of UJ-11: `lib/stream.ts`, `StreamPanel` and `TraceStep` (FR-129).
 *
 * The question this file answers is the one the operator asks when his network blinks: **did I
 * lose a frame, and did I read one twice?** So every assertion here is about the two ends of a
 * connection - the cursor it opens with and the rows it renders after it comes back:
 *   - the handoff: a replay ends at `seq` N, and the stream opens with `Last-Event-ID` equal to
 *     that leaf's own characters, so the first streamed row is the next one and no row is shown
 *     twice;
 *   - the drop: a connection cut at `seq` N of `total` renders exactly `total - N` new rows, the
 *     first at N + 1, with no duplicate and no gap;
 *   - the retries: 1 s, 2 s, 4 s then 10 s at most on a fake clock, saying `Reconnecting` while
 *     it waits and sending the cursor it holds, and a 400 or 404 that stops the retries and
 *     quotes the body;
 *   - the close: a terminal close only where the finish pairs with the session's `seq` 1 start
 *     (decision 17, MUST rule 20), and the idle ceiling otherwise - which is why the improve
 *     sequence stays `Live` past its inner `eval.run.finished`.
 *
 * **0 cursors or `seq`s are drawn** (FR-129 AC2), so a row is known by the sentence it reads as:
 * each recorded frame's own `TraceStep`, rendered alone, is the identity a logged row is compared
 * against, and the recorded frames' sentences are checked to be distinct first, so "no duplicate
 * and no gap" is still a claim that can fail. The cursor itself is read off the `Last-Event-ID`
 * each subscription sent, which is where it always was.
 *
 * The frames are the recorded ones (decision 33). `tests/support/stream-drops.ts` cuts
 * `tests/fixtures/gateway/sessions.stream.sse` into per-frame slices and re-joins them into the
 * chunk sequences a dropped connection delivers; nothing here edits a recorded file (MUST rule
 * 24). The one hand-written input is the improve session's frames, which no recording holds and
 * which decision 33 allows for exactly this: it is written in that module, in `gateway/sse.py`'s
 * own four-line shape, and marked as written.
 *
 * The panel is rendered inside `AppShell`, the way `app/layout.tsx` mounts it, and beside the
 * real `ReplayPanel` over the recorded `/events` body - which is what makes "0 duplicated rows at
 * the handoff" a claim about what the operator sees rather than about one component's state. The
 * clock is fake throughout, so a backoff schedule is exact.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { sessionMessagesRoute } from "../../components/Conversation";
import { type Handoff, ReplayPanel } from "../../app/traces/ReplayPanel";
import { AppShell } from "../../components/AppShell";
import { HEALTH_ROUTE } from "../../components/GatewayStatusPill";
import { StreamPanel, statusWord } from "../../components/StreamPanel";
import { TraceStep } from "../../components/TraceStep";
import {
  ANNOUNCE_INTERVAL_MS,
  BACKOFF_MS,
  IDLE_CEILING,
  LAST_EVENT_ID,
  type StreamSignal,
  atBottom,
  closeReason,
  frameOf,
  nextBackoffMs,
  parseFrames,
  payloadPaths,
  readStream,
  statusLabel,
  streamRoute,
} from "../../lib/stream";
import { STALE_AFTER_MINUTES, STALE_TICK_MS, now } from "../../lib/clock";
import { cliTwinFor } from "../../lib/cli-twins";
import { GATEWAY_ORIGIN } from "../../lib/fetch-panel";
import { humanTimestamp } from "../../lib/humanize";
import { absentTextFor, emptyTextFor, nullReasonFor } from "../../lib/null-reasons";
import { VERBATIM } from "../../lib/trace-steps";
import { parseWire } from "../../lib/wire";

import { figuresWithoutSource } from "../support/figures";
import { sourceCharactersByPath } from "../support/source-at";
import {
  EVENTS_BODY,
  EVENTS_ROUTE,
  IMPROVE_FRAMES,
  IMPROVE_START,
  IMPROVE_TERMINAL,
  RECORDED_FRAMES,
  SESSION_ID,
  VOCABULARY_FRAMES,
  cutAt,
  seqOf,
} from "../support/stream-drops";

import type { ReactNode } from "react";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const REPO = resolve(DASHBOARD, "..");
const EVIDENCE = resolve(REPO, "evidence/111/stream.txt");
const LIVE_HTML = resolve(REPO, "evidence/111/stream-live.html");
const RECONNECT_HTML = resolve(REPO, "evidence/111/stream-reconnecting.html");
const REFUSED_HTML = resolve(REPO, "evidence/111/stream-refused.html");
/** Ticket 120's manifest: the arrival-to-DOM measurement and the tree that carries one region. */
const ARRIVAL = resolve(REPO, "evidence/120/arrival.txt");
const LIVE_REGIONS_HTML = resolve(REPO, "evidence/120/live-regions.html");

/** The gateway this test's shell is pointed at. Not the operator's 8756 (decision 30). */
const GATEWAY = GATEWAY_ORIGIN;

/** One minute of the fake clock, for the five-minute stale mark `D-5` sets. */
const MINUTE_MS = 60 * 1000;

/** What a cut connection says for itself, so a cause is never invented by the panel. */
const DROP_CAUSE = "the connection was reset";

/** The lines this run writes to `evidence/111/stream.txt`. */
const log: string[] = [];

/** The lines this run writes to `evidence/120/arrival.txt`. */
const arrivalLog: string[] = [];

/** `gateway/sse.py::POLL_INTERVAL_S`, as a fake-clock gap between two reads of the stream. */
const GATEWAY_POLL_MS = 100;

/**
 * What ticket 112 measured before the split, quoted rather than re-measured.
 *
 * `evidence/112/stream-e2e.txt`: 11 of 28 frames rendered 1037 to 1129 ms after the `ts` the
 * gateway stamped, every one of them arriving mid-cooldown. These are that walk's figures and
 * this file does not reproduce them; they are here so the manifest reads before and after.
 */
const BEFORE_120 = [
  "before (ticket 112, over a real stream, evidence/112/stream-e2e.txt):",
  "  11 of 28 frames rendered 1037 to 1129 ms after the gateway's `ts`;",
  "  every late frame arrived mid-cooldown, every fast one found the chain idle.",
];

const STREAM_ROUTE = streamRoute(SESSION_ID);

/** The recorded 404 bodies, so a refusal is quoted from the gateway and never written here. */
const MESSAGES_BODY = recordedBody("sessions.messages.body");
const EVENTS_NOT_FOUND = recordedBody("notfound.sessions.events.body");
const STREAM_NOT_FOUND = recordedBody("notfound.sessions.stream.body");
const BAD_CURSOR_BODY = recordedBody("badrequest.sessions.stream.cursor.body");

function recordedBody(name: string): string {
  return readFileSync(resolve(DASHBOARD, "tests/fixtures/gateway", name), "utf8");
}

/** The recorded `/health` body the shell's pill reads, so its poll is answered and not scripted. */
const HEALTH_BODY = readFileSync(
  resolve(DASHBOARD, "tests/fixtures/gateway/health.body"),
  "utf8",
);

// ---- The scripted gateway ---------------------------------------------------------------------

/** How one scripted subscription ends: cleanly, cut mid-flight, or still open. */
type Ending = "close" | "drop" | "hold";

/** One scripted answer to one `GET .../stream`: frames and an ending, or a refusal. */
type Episode =
  | { kind: "stream"; chunks: readonly string[]; ending: Ending; gapMs?: number }
  | { kind: "refused"; status: number; body: string };

/** Every `Last-Event-ID` the panel sent, in order, exactly as it sent it. */
let cursors: (string | null)[] = [];
/** Every URL the panel asked for, so "only `/v1`, only GET" is checked and not assumed. */
let asked: { url: string; method: string }[] = [];
let episodes: Episode[] = [];
/** The gateway is not answering at all: every `fetch` rejects, as a browser's would. */
let down = false;
/** The replay route answers 404, so the page's two halves can be refused together. */
let replay404 = false;
/** The replay fetch rejects: the process was never reached at all (MUST rule 23). */
let replayTransportDown = false;
/** The fake clock each scripted chunk was enqueued on, in order (ticket 120's arrival mark). */
let enqueuedAt: number[] = [];

/**
 * One scripted subscription: a chunk per read, then the ending.
 *
 * The chunks are enqueued on demand rather than up front, because erroring a stream discards
 * whatever is still queued - a drop scripted the other way round would deliver no frame at all
 * and the test would prove nothing about resuming.
 *
 * `gapMs` spaces the reads on the fake clock, the way `gateway/sse.py::POLL_INTERVAL_S` spaces
 * them on a real one: without it every chunk of a burst lands in the same instant, and an
 * arrival-to-DOM delay measured across it would be the same instant either way (ticket 120).
 * Each enqueue records the clock it happened on in `enqueuedAt`, so the delay is measured from
 * the arrival rather than from when the test decided to look.
 */
function sseResponse(chunks: readonly string[], ending: Ending, gapMs?: number): Response {
  let at = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (at < chunks.length) {
        const chunk = chunks[at];
        at += 1;
        const deliver = (): void => {
          enqueuedAt.push(Date.now());
          controller.enqueue(new TextEncoder().encode(chunk));
        };
        if (gapMs === undefined) {
          deliver();
          return undefined;
        }
        return new Promise<void>((resolve) => {
          setTimeout(() => {
            deliver();
            resolve();
          }, gapMs);
        });
      }
      if (ending === "close") controller.close();
      if (ending === "drop") controller.error(new Error(DROP_CAUSE));
      // `hold` is a connection that stays open with nothing more to say.
      return ending === "hold" ? new Promise<void>(() => {}) : undefined;
    },
    cancel() {},
  });
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

/** A recorded JSON answer, for the two routes the shell and the replay read. */
function json(body: string): Promise<Response> {
  return Promise.resolve(
    new Response(body, { status: 200, headers: { "content-type": "application/json" } }),
  );
}

/**
 * The replay, the shell's own poll and the stream, each answered by the route that asked.
 *
 * The shell polls `/health` every 15 s (decision 34), so a test that lets half an hour pass sees
 * that poll and no other read: the stream script is reached only by the stream's own route, which
 * is how "0 retries after a refusal" stays a claim about the stream.
 */
function gateway(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = String(input);
  const headers = new Headers(init?.headers);
  asked.push({ url, method: init?.method ?? "GET" });

  if (down) return Promise.reject(new TypeError("Failed to fetch"));
  if (url === EVENTS_ROUTE && replayTransportDown) {
    return Promise.reject(new TypeError("Failed to fetch"));
  }
  if (url === EVENTS_ROUTE) {
    return replay404
      ? Promise.resolve(
          new Response(EVENTS_NOT_FOUND, {
            status: 404,
            headers: { "content-type": "application/json" },
          }),
        )
      : json(EVENTS_BODY);
  }
  if (url === sessionMessagesRoute(SESSION_ID)) return json(MESSAGES_BODY);
  if (url === HEALTH_ROUTE) return json(HEALTH_BODY);
  if (url !== STREAM_ROUTE) {
    return Promise.resolve(new Response("{}", { status: 200 }));
  }

  cursors.push(headers.get(LAST_EVENT_ID));
  const episode = episodes.length > 1 ? (episodes.shift() as Episode) : episodes[0];
  if (episode === undefined) return Promise.resolve(sseResponse([], "hold"));
  if (episode.kind === "refused") {
    return Promise.resolve(
      new Response(episode.body, {
        status: episode.status,
        headers: { "content-type": "application/json" },
      }),
    );
  }
  return Promise.resolve(sseResponse(episode.chunks, episode.ending, episode.gapMs));
}

/** Let the panel's promises and timers run for `ms` of its fake clock. */
async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/**
 * Let the reader finish what it can without moving the clock, so a backoff wait is still ahead.
 *
 * The first batch of a burst is shown at once and only the rest waits for the interval, which is
 * what keeps this from colliding with the 1 s first backoff: advancing a second here would retry
 * the connection before the test could read `reconnecting`.
 */
async function flushed(): Promise<void> {
  await settle();
  await settle();
  await settle();
}

beforeEach(() => {
  vi.useFakeTimers();
  cursors = [];
  asked = [];
  episodes = [];
  down = false;
  replay404 = false;
  replayTransportDown = false;
  enqueuedAt = [];
  handoffs = [];
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
  vi.stubGlobal("fetch", vi.fn(gateway));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

afterAll(() => {
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  mkdirSync(dirname(ARRIVAL), { recursive: true });
  writeFileSync(ARRIVAL, `${[...BEFORE_120, "", ...arrivalLog].join("\n")}\n`, "utf8");
});

// ---- The replay this stream hands off from -----------------------------------------------------

const replay = parseWire(EVENTS_BODY, EVENTS_ROUTE);

/** The `seq` leaf of the last replayed row: the cursor the stream must open with. */
const LAST_REPLAYED = replay.at(".events[7].seq");

/** The name at `seq` 1 of this session, which is the start half of decision 17's pair. */
const REPLAY_START = replay.at(".events[0].name");

/** Every handoff the replay passed up, in order, exactly as it passed it. */
let handoffs: Handoff[] = [];

/** The replay's `onReplayed`, stable across renders so the handoff effect fires once a response. */
function recordHandoff(handoff: Handoff): void {
  handoffs.push(handoff);
}

/** The replay panel of ticket 112, as the session page mounts it, over the recorded body. */
function Replay() {
  return <ReplayPanel sessionId={SESSION_ID} onReplayed={recordHandoff} />;
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
 * attached to the page, so it is the identity a logged row is compared against now that no `seq`
 * is drawn (FR-129 AC2).
 */
function sentencesOf(frames: readonly string[]): string[] {
  return frames.map((text) => {
    const [raw] = parseFrames(text).frames;
    const alone = render(<TraceStep frame={frameOf(raw, STREAM_ROUTE)} />, {
      container: document.createElement("ul"),
    });
    const [sentence] = sentencesIn(alone.container);
    alone.unmount();
    return sentence;
  });
}

/** What the replay panel's steps read as, in order. */
function replayedSentences(): string[] {
  return sentencesIn(screen.queryByTestId("replay-log"));
}

function mount(node: ReactNode) {
  return render(<AppShell>{node}</AppShell>);
}

/**
 * The figure at a route and path, found by its non-rendered `data-route`/`data-path` hooks - the
 * figure is its own trigger now (decision 68), never a `SourceChip` found by a `route -> path`
 * title.
 */
function figureAt(route: string, path: string): HTMLElement {
  const found = document.querySelector(`[data-route="${route}"][data-path="${path}"]`);
  if (found === null) throw new Error(`no figure for ${route} -> ${path}`);
  return found as HTMLElement;
}

/** What the stream panel's steps read as, in the order the log shows them. */
function streamedSentences(): string[] {
  return sentencesIn(screen.queryByTestId("stream-log"));
}

function statusText(): string {
  return screen.getByTestId("stream-status").textContent ?? "";
}

/** The status word for nothing arrived yet, and for a subscription with rows on screen. */
const WAITING = statusWord({ kind: "connecting" }, false);
const LIVE = statusWord({ kind: "live" }, true);
const RECONNECTING = statusWord({ kind: "reconnecting", cursor: "0" }, false);
const DONE = statusWord({ kind: "closed", reason: IDLE_CEILING }, true);
const COULD_NOT_CONNECT = statusWord({ kind: "refused", status: "404", body: "" }, false);

// ---- The frame, and the two leaves that are not JSON --------------------------------------------

describe("the four-line frame", () => {
  it("cuts the recorded stream into whole frames and keeps a half-arrived one back", () => {
    const whole = parseFrames(RECORDED_FRAMES.join(""));
    expect(whole.frames.map((frame) => frame.id)).toEqual([
      "1",
      "2",
      "3",
      "4",
      "5",
      "6",
      "7",
      "8",
    ]);
    expect(whole.rest).toBe("");
    expect(whole.frames[0].event).toBe("run.started");
    expect(whole.frames[0].data).toBe('{"provider":"fake","routing":"on"}');

    // A read can end anywhere. Half of frame 2 is not frame 2, and the next read completes it.
    const half = `${RECORDED_FRAMES[0]}id: 2\nevent: loop.route`;
    const partial = parseFrames(half);
    expect(partial.frames).toHaveLength(1);
    expect(partial.rest).toBe("id: 2\nevent: loop.route");
    expect(parseFrames(`${partial.rest}.decided\ndata: {"a":1}\n\n`).frames).toHaveLength(1);

    // A block that is not a whole frame is not a frame, and does not stop the ones that are.
    expect(parseFrames(`: keep-alive\n\n${RECORDED_FRAMES[0]}`).frames).toHaveLength(1);
    log.push("lib/stream.ts: the recorded stream cuts into 7 whole frames, a partial one waits");
  });

  it("reads `seq`, `name` and every payload figure off the frame's own characters", () => {
    const [raw] = parseFrames(RECORDED_FRAMES[2]).frames;
    const frame = frameOf(raw, STREAM_ROUTE);

    expect(frame.seq.text).toBe("3");
    expect(frame.seq.route).toBe(STREAM_ROUTE);
    expect(frame.name.text).toBe("retrieval.gate.decided");

    // The independent oracle: the characters the `data:` line spells at each path.
    const spelled = sourceCharactersByPath(frame.data);
    expect(frame.paths).toEqual([".answer", ".failed_open", ".reason", ".retrieve"]);
    for (const path of frame.paths) {
      expect(frame.payload.at(path).text).toBe(spelled.get(path));
    }
    expect(frame.payload.at(".failed_open").kind).toBe("boolean");
    expect(payloadPaths("not json at all")).toEqual([]);
    log.push("lib/stream.ts: a frame's `seq`, `name` and payload figures are its own characters");
  });

  it("reads a line with no space after its colon, and an array payload by index", () => {
    // Decision 20's `field: value` allows the single space and does not require it, so a frame
    // written without it is the same frame. A list in the payload is one figure per index, in
    // the order the `data:` line spells them, so nothing in a frame is skipped.
    const written = 'id:9\nevent:eval.run.finished\ndata:{"cases":["a","b"]}\n\n';
    const [raw] = parseFrames(written).frames;
    expect(raw).toEqual({ id: "9", event: "eval.run.finished", data: '{"cases":["a","b"]}' });

    const frame = frameOf(raw, STREAM_ROUTE);
    expect(frame.paths).toEqual([".cases[0]", ".cases[1]"]);
    expect(frame.payload.at(".cases[1]").text).toBe(
      sourceCharactersByPath(frame.data).get(".cases[1]"),
    );
    // A path the payload does not hold is an error and never an empty figure (MUST rule 21).
    expect(() => frame.payload.at(".cases[2]")).toThrow(STREAM_ROUTE);
    log.push("lib/stream.ts: a colon with no space is the same field, and a list is one figure an index");
  });
});

// ---- The backoff table and the pairing ----------------------------------------------------------

describe("the reconnect schedule", () => {
  it("waits 1 s, 2 s, 4 s and then 10 s at most, and starts over after a frame", () => {
    const waits: number[] = [];
    let previous: number | undefined;
    while (waits.length < 6) {
      previous = nextBackoffMs(previous);
      waits.push(previous);
    }
    expect(waits).toEqual([1000, 2000, 4000, 10000, 10000, 10000]);
    expect(Math.max(...waits)).toBe(10000);
    expect(BACKOFF_MS[0]).toBe(1000);
    // A frame clears the run of drops, so the next drop waits 1 s again.
    expect(nextBackoffMs(undefined)).toBe(1000);
    log.push("lib/stream.ts: the backoff table is 1 s, 2 s, 4 s, then 10 s at most");
  });

  it("names a close by the pair at `seq` 1 and never by the finish's name", () => {
    expect(closeReason("run.started", "run.finished")).toBe("terminal run.finished");
    expect(closeReason(IMPROVE_START, IMPROVE_TERMINAL)).toBe(`terminal ${IMPROVE_TERMINAL}`);
    // MUST rule 20: an improve session's inner eval finish is not its end, and a finish with no
    // start to pair with names nothing.
    expect(closeReason(IMPROVE_START, "eval.run.finished")).toBe(IDLE_CEILING);
    expect(closeReason(undefined, "run.finished")).toBe(IDLE_CEILING);
    expect(closeReason("memory.write", "run.finished")).toBe(IDLE_CEILING);
    log.push("lib/stream.ts: a terminal close is the pair at `seq` 1, never a finish's name");
  });

  it("says each state in the words the design names", () => {
    // `lib/stream.ts` is unchanged and still spells its own label; the panel no longer shows it.
    expect(statusLabel({ kind: "connecting" })).toBe("connecting");
    expect(statusLabel({ kind: "live" })).toBe("live");
    expect(statusLabel({ kind: "reconnecting", cursor: "41" })).toBe("reconnecting (cursor 41)");
    expect(statusLabel({ kind: "closed", reason: IDLE_CEILING })).toBe("closed: idle ceiling");
    expect(statusLabel({ kind: "refused", status: "400", body: "" })).toBe("refused 400");

    // What the panel says (FR-129): a word per state, and no cursor, reason or status code in it.
    expect(statusWord({ kind: "connecting" }, false)).toBe("Waiting for the first step");
    expect(statusWord({ kind: "connecting" }, true)).toBe("Live");
    expect(statusWord({ kind: "live" }, true)).toBe("Live");
    expect(statusWord({ kind: "reconnecting", cursor: "41" }, true)).toBe("Reconnecting");
    expect(statusWord({ kind: "closed", reason: IDLE_CEILING }, true)).toBe("Done");
    expect(statusWord({ kind: "closed", reason: "terminal run.finished" }, true)).toBe("Done");
    expect(statusWord({ kind: "refused", status: "400", body: "" }, false)).toBe(
      "Could not connect",
    );
    for (const word of [WAITING, LIVE, RECONNECTING, DONE, COULD_NOT_CONNECT]) {
      expect(word).not.toMatch(/[0-9]/);
    }
  });

  it("knows the log is at its last row from the browser's own offsets", () => {
    expect(atBottom({ scrollTop: 400, scrollHeight: 800, clientHeight: 400 })).toBe(true);
    expect(atBottom({ scrollTop: 0, scrollHeight: 800, clientHeight: 400 })).toBe(false);
  });
});

// ---- The handoff -------------------------------------------------------------------------------

describe("the handoff from the replay", () => {
  it("opens with the last replayed `seq` verbatim and repeats no row", async () => {
    episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
    const { container } = mount(
      <>
        <Replay />
        <StreamPanel
          sessionId={SESSION_ID}
          afterSeq={LAST_REPLAYED}
          startEvent={REPLAY_START}
        />
      </>,
    );
    await flushed();

    // The cursor is the leaf's own text, sent as it reads. Never `seq + 1`, never a number.
    expect(LAST_REPLAYED.text).toBe("8");
    expect(cursors).toEqual([LAST_REPLAYED.text]);
    expect(asked.every((call) => call.method === "GET")).toBe(true);
    expect(asked.every((call) => call.url.startsWith("/v1"))).toBe(true);
    expect(asked.some((call) => call.url === STREAM_ROUTE)).toBe(true);

    // The replay hands up that same leaf, and the `seq` 1 name that pairs the close.
    expect(handoffs.map((handoff) => handoff.afterSeq.text)).toEqual([LAST_REPLAYED.text]);
    expect(handoffs.map((handoff) => handoff.startEvent.text)).toEqual([REPLAY_START.text]);

    // Every recorded frame reads as a sentence of its own, so a row repeated or missed shows.
    const recorded = sentencesOf(RECORDED_FRAMES);
    expect(new Set(recorded).size).toBe(RECORDED_FRAMES.length);

    // 0 duplicated rows at the handoff: the replay holds 1..7 and the stream holds none of them.
    expect(replayedSentences()).toEqual(recorded);
    expect(streamedSentences()).toEqual([]);
    expect(statusText()).toBe(WAITING);
    // 0 cursors or `seq`s drawn (FR-129 AC2): no cursor line, and no handoff sentence naming one.
    expect(screen.queryByTestId("stream-cursor")).toBeNull();
    expect(screen.queryByTestId("replay-handoff")).toBeNull();
    expect(container.textContent).not.toContain(LAST_EVENT_ID);
    expect(figuresWithoutSource(container)).toEqual([]);
    log.push("StreamPanel: opens at the replay's last `seq` leaf, verbatim; 0 rows repeated");
  });

  it("renders the frames after the cursor and nothing at or before it", async () => {
    // The gateway resumes at the cursor; the panel is asserted on what it rendered, not on a
    // number it worked out: the first streamed row is the row after the last replayed one.
    const cut = cutAt("4");
    episodes = [{ kind: "stream", chunks: [cut.after.join("")], ending: "close" }];
    const { container } = mount(
      <>
        <Replay />
        <StreamPanel
          sessionId={SESSION_ID}
          afterSeq={replay.at(".events[3].seq")}
          startEvent={REPLAY_START}
        />
      </>,
    );
    await flushed();

    expect(cursors).toEqual(["4"]);
    expect(streamedSentences()).toEqual(sentencesOf(cut.after));
    expect(seqOf(cut.after)).toEqual(["5", "6", "7", "8"]);
    expect(new Set(streamedSentences()).size).toBe(streamedSentences().length);
    // Nothing at or before the cursor: none of the replayed 1..4 is in the stream's log.
    for (const before of sentencesOf(cut.upTo)) {
      expect(streamedSentences()).not.toContain(before);
    }
    expect(figuresWithoutSource(container)).toEqual([]);
    writeHtml(LIVE_HTML, container);
    log.push("StreamPanel: a cursor of 4 renders 5, 6, 7 - 0 duplicates and 0 gaps");
  });
});

// ---- The drop ----------------------------------------------------------------------------------

describe("a dropped connection", () => {
  it("resumes at its cursor and renders exactly the frames it had not seen", async () => {
    const cut = cutAt("4");
    episodes = [
      { kind: "stream", chunks: [cut.upTo.join("")], ending: "drop" },
      { kind: "stream", chunks: [cut.after.join("")], ending: "close" },
    ];
    mount(
      <StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} startEvent={REPLAY_START} />,
    );
    await flushed();
    expect(seqOf(cut.upTo)).toEqual(["1", "2", "3", "4"]);
    expect(streamedSentences()).toEqual(sentencesOf(cut.upTo));
    expect(statusText()).toBe(RECONNECTING);

    await settle(BACKOFF_MS[0]);
    await flushed();

    // `total - N` new rows, the first at N + 1, read off the rendered log.
    expect(cursors).toEqual([ZERO_CURSOR.text, "4"]);
    expect(streamedSentences()).toEqual(sentencesOf(RECORDED_FRAMES));
    expect(screen.getAllByTestId("trace-step")).toHaveLength(RECORDED_FRAMES.length);
    expect(new Set(streamedSentences()).size).toBe(RECORDED_FRAMES.length);
    log.push("StreamPanel: dropped at 4 of 7, it resumes at cursor 4 and renders 5, 6, 7");
  });

  it("waits 1 s, 2 s, 4 s and then 10 s, saying which cursor it is holding", async () => {
    episodes = [
      { kind: "stream", chunks: [RECORDED_FRAMES[0]], ending: "drop" },
      { kind: "stream", chunks: [], ending: "drop" },
      { kind: "stream", chunks: [], ending: "drop" },
      { kind: "stream", chunks: [], ending: "drop" },
      { kind: "stream", chunks: [], ending: "drop" },
      { kind: "stream", chunks: [], ending: "hold" },
    ];
    const { container } = mount(
      <StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} startEvent={REPLAY_START} />,
    );
    await flushed();
    expect(statusText()).toBe(RECONNECTING);
    expect(streamedSentences()).toEqual(sentencesOf([RECORDED_FRAMES[0]]));
    writeHtml(RECONNECT_HTML, container);

    // One attempt per wait, and nothing before the wait is over.
    const waited: number[] = [];
    for (const wait of [1000, 2000, 4000, 10000, 10000]) {
      const before = cursors.length;
      await settle(wait - 1);
      expect(cursors.length).toBe(before);
      await settle(1);
      await flushed();
      expect(cursors.length).toBe(before + 1);
      waited.push(wait);
    }
    expect(waited).toEqual([1000, 2000, 4000, 10000, 10000]);
    // Every retry carries the same cursor, because no frame has moved it since the first.
    expect(cursors.slice(1)).toEqual(["1", "1", "1", "1", "1"]);
    // Connecting again behind a row already on screen is still `Live`, never "Waiting".
    expect(statusText()).toBe(statusWord({ kind: "connecting" }, true));
    expect(statusText()).toBe(LIVE);
    expect(streamedSentences()).toEqual(sentencesOf([RECORDED_FRAMES[0]]));
    log.push("StreamPanel: repeated drops wait 1 s, 2 s, 4 s, 10 s, 10 s at cursor 1");
  });
});

// ---- The refusals ------------------------------------------------------------------------------

describe("a refusal", () => {
  // The gateway's own words for both refusals, read from the recorded corpus (decision 33).
  const BAD_CURSOR = BAD_CURSOR_BODY;
  const UNKNOWN_SESSION = STREAM_NOT_FOUND;

  it("stops retrying on a 400 and shows the body", async () => {
    episodes = [{ kind: "refused", status: 400, body: BAD_CURSOR }];
    const { container } = mount(
      <StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} startEvent={REPLAY_START} />,
    );
    await flushed();

    expect(statusText()).toBe(COULD_NOT_CONNECT);
    // The status word carries no status code; the body is quoted as the gateway sent it, never
    // summarised (decision 24).
    expect(statusText()).not.toContain("400");
    expect(screen.getByTestId("stream-refusal").textContent).toContain(BAD_CURSOR);
    writeHtml(REFUSED_HTML, container);

    // Half an hour of the fake clock, and it never asks again.
    await settle(1800000);
    expect(cursors).toHaveLength(1);

    // `Refresh` is the operator asking: exactly one more subscription, at the same cursor.
    fireEvent.click(within(screen.getByRole("main")).getByRole("button", { name: "Refresh" }));
    await flushed();
    expect(cursors).toEqual([ZERO_CURSOR.text, ZERO_CURSOR.text]);
    expect(statusText()).toBe(COULD_NOT_CONNECT);
    await settle(1800000);
    expect(cursors).toHaveLength(2);
    log.push("StreamPanel: a 400 is `Could not connect` with the body, and 0 retries in half an hour");
  });

  it("stops retrying on a 404 and shows the body", async () => {
    episodes = [{ kind: "refused", status: 404, body: UNKNOWN_SESSION }];
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);
    await flushed();

    expect(statusText()).toBe(COULD_NOT_CONNECT);
    expect(statusText()).not.toContain("404");
    expect(screen.getByTestId("stream-refusal").textContent).toContain(UNKNOWN_SESSION);
    await settle(1800000);
    expect(cursors).toHaveLength(1);
    log.push("StreamPanel: a 404 is `Could not connect` with the body, and 0 retries in half an hour");
  });

  it("treats any other status as a drop and retries it", async () => {
    episodes = [
      { kind: "stream", chunks: [], ending: "drop" },
      { kind: "stream", chunks: [RECORDED_FRAMES[0]], ending: "hold" },
    ];
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);
    await flushed();
    expect(statusText()).toBe(RECONNECTING);
    // No refusal shown and no `Refresh` offered: a drop retries on its own.
    expect(screen.queryByTestId("stream-refusal")).toBeNull();
    expect(within(screen.getByRole("main")).queryByRole("button", { name: "Refresh" })).toBeNull();
    await settle(BACKOFF_MS[0]);
    await flushed();
    expect(cursors).toEqual([ZERO_CURSOR.text, ZERO_CURSOR.text]);
    expect(statusText()).toBe(LIVE);
    log.push("StreamPanel: a cut connection is a retry, not a refusal");
  });
});

// ---- The close ---------------------------------------------------------------------------------

describe("a clean close", () => {
  it("names the terminal event when the finish pairs with the session's `seq` 1 start", async () => {
    episodes = [{ kind: "stream", chunks: [RECORDED_FRAMES.join("")], ending: "close" }];
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);
    await flushed();

    // No `startEvent` was handed in: the stream delivered `seq` 1 itself and that is the pair.
    const names = parseFrames(RECORDED_FRAMES.join("")).frames.map((frame) => frame.event);
    expect(closeReason(names[0], names.at(-1))).toBe("terminal run.finished");
    expect(streamedSentences()).toEqual(sentencesOf(RECORDED_FRAMES));
    // The word is `Done` and names no reason: the pairing is `closeReason`'s, proved above.
    expect(statusText()).toBe(DONE);
    expect(statusText()).not.toContain("run.finished");
    await settle(1800000);
    expect(cursors).toHaveLength(1);
    log.push("StreamPanel: a close after the paired finish is `Done`, and 0 retries after it");
  });

  it("says the idle ceiling when nothing pairs with the last frame", async () => {
    const cut = cutAt("4");
    episodes = [{ kind: "stream", chunks: [cut.after.join("")], ending: "close" }];
    // A resumed stream never saw `seq` 1 and was told no start event, so it names no terminal.
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={replay.at(".events[3].seq")} />);
    await flushed();

    const last = parseFrames(cut.after.join("")).frames.at(-1)?.event;
    expect(closeReason(undefined, last)).toBe(IDLE_CEILING);
    expect(streamedSentences()).toEqual(sentencesOf(cut.after));
    expect(statusText()).toBe(DONE);
    expect(statusText()).not.toContain(IDLE_CEILING);
    await settle(1800000);
    expect(cursors).toHaveLength(1);
    log.push("StreamPanel: a close with no pair to read is the idle ceiling, said as `Done`");
  });

  it("stays live past an improve session's inner `eval.run.finished`", async () => {
    // MUST rule 20, the reason the pairing exists: an improve session publishes its baseline,
    // candidate and holdout evals on its own bus.
    const upTo = IMPROVE_FRAMES.slice(0, 4);
    const rest = IMPROVE_FRAMES.slice(4);
    episodes = [{ kind: "stream", chunks: [upTo.join("")], ending: "hold" }];
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);
    await flushed();

    // The inner finish is on the log, as its own step, and the panel is still following.
    const names = parseFrames(upTo.join("")).frames.map((frame) => frame.event);
    expect(names).toContain("eval.run.finished");
    expect(streamedSentences()).toEqual(sentencesOf(upTo));
    expect(screen.getAllByTestId("trace-step")).toHaveLength(upTo.length);
    expect(closeReason(IMPROVE_START, names.at(-1))).toBe(IDLE_CEILING);
    expect(statusText()).toBe(LIVE);

    // The rest of the cycle arrives on a fresh subscription and the outer finish closes it.
    episodes = [{ kind: "stream", chunks: [rest.join("")], ending: "close" }];
    cleanup();
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} startEvent={IMPROVE_START_LEAF} />);
    await flushed();
    expect(closeReason(IMPROVE_START_LEAF.text, IMPROVE_TERMINAL)).toBe(`terminal ${IMPROVE_TERMINAL}`);
    expect(streamedSentences()).toEqual(sentencesOf(rest));
    expect(statusText()).toBe(DONE);
    log.push("StreamPanel: an improve stream stays live past its inner eval finish and closes at its own");
  });
});

// ---- The log ------------------------------------------------------------------------------------

/** What the panel's own polite region is saying, which is "" until the first interval fires. */
function announcementText(): string {
  return screen.getByTestId("stream-announcement").textContent ?? "";
}

/** The `event:` name of the last recorded frame, read off the recording rather than written here. */
const NEWEST_RECORDED = parseFrames(RECORDED_FRAMES[RECORDED_FRAMES.length - 1]).frames[0].event;

describe("the log", () => {
  it("puts each row in the DOM on the read it arrived on, not on the announcement interval", async () => {
    // Seven frames, seven separate reads, one gateway poll apart: the shape ticket 112 measured
    // 1037 to 1129 ms of lag over. The whole burst fits inside one `ANNOUNCE_INTERVAL_MS`, so a
    // row that waits for the interval is a row the test can see waiting.
    episodes = [
      { kind: "stream", chunks: RECORDED_FRAMES, ending: "hold", gapMs: GATEWAY_POLL_MS },
    ];
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);

    arrivalLog.push("after (ticket 120, seven recorded frames, one gateway poll apart):");
    const seqs = seqOf(RECORDED_FRAMES);
    const sentences = sentencesOf(RECORDED_FRAMES);
    const landed: string[] = [];
    for (const [index, seq] of seqs.entries()) {
      await settle(GATEWAY_POLL_MS);
      await flushed();
      landed.push(sentences[index]);

      // The row is in the DOM at the instant the chunk was enqueued: the delay is the panel's,
      // and it is measured against the fake clock rather than taken on trust.
      const arrived = enqueuedAt[index];
      const delay = Date.now() - arrived;
      expect(streamedSentences()).toEqual(landed);
      expect(delay).toBe(0);
      expect(delay).toBeLessThan(ANNOUNCE_INTERVAL_MS);
      arrivalLog.push(`  seq ${seq}: arrived ${arrived} ms, in the DOM ${delay} ms later`);
    }
    expect(streamedSentences()).toEqual(sentences);
    expect(enqueuedAt).toHaveLength(seqs.length);

    // Scrolled to the bottom, the log still follows the newest row.
    const list = screen.getByTestId("stream-log");
    expect(list.scrollTop).toBe(list.scrollHeight);
    log.push("StreamPanel: a row is in the DOM on the read it arrived on, 0 ms of batching delay");
  });

  it("lands a burst that arrives together all at once", async () => {
    // The other half of the same claim: seven reads inside one instant are seven rows in that
    // instant, with no clock moved at all between the arrival and the assertion.
    episodes = [{ kind: "stream", chunks: RECORDED_FRAMES, ending: "hold" }];
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);
    await flushed();

    expect(streamedSentences()).toEqual(sentencesOf(RECORDED_FRAMES));
    expect(announcementText()).toBe("");
    arrivalLog.push(
      `  a burst of ${seqOf(RECORDED_FRAMES).length} arriving together: all ${seqOf(RECORDED_FRAMES).length} rows in the DOM, 0 ms later`,
    );
    log.push("StreamPanel: a burst arriving together lands together, with no interval waited out");
  });

  it("still batches the announcement to one a second, however many frames arrived", async () => {
    // FR-59's cap is about flooding a screen reader, so it is the announcement that is counted
    // here and not the rows: seven frames inside one interval, one thing said.
    episodes = [
      { kind: "stream", chunks: RECORDED_FRAMES, ending: "hold", gapMs: GATEWAY_POLL_MS },
    ];
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);

    const said: string[] = [];
    const listen = (): void => {
      const text = announcementText();
      if (text !== "" && text !== said[said.length - 1]) said.push(text);
    };
    const sentences = sentencesOf(RECORDED_FRAMES);
    for (const sentence of sentences) {
      await settle(GATEWAY_POLL_MS);
      await flushed();
      // The row is already there; it is the saying of it that is being counted.
      expect(streamedSentences()).toContain(sentence);
      listen();
    }
    // Every frame is on the page and nothing has been announced yet: the interval has not come.
    expect(streamedSentences()).toEqual(sentences);
    expect(said).toEqual([]);

    await settle(ANNOUNCE_INTERVAL_MS);
    await flushed();
    listen();

    // How many arrived and the newest name, the name spelled as the frame spelled it (`D-7`).
    expect(said).toEqual([`8 events arrived on this stream, newest ${NEWEST_RECORDED}`]);

    // Nothing more arrives, so the chain stops rather than repeating itself.
    await settle(ANNOUNCE_INTERVAL_MS);
    await flushed();
    listen();
    expect(said).toHaveLength(1);
    arrivalLog.push(`  announcements over that burst: ${said.length} ("${said[0]}")`);
    log.push("StreamPanel: seven frames inside one interval are exactly one polite announcement");
  });

  it("carries that announcement in the one polite region, which is not the rows' container", async () => {
    // Rendered without `AppShell`, so "the page" is this panel and nothing else: the shell's
    // gateway pill has a polite region of its own, and it is not what this claim is about.
    episodes = [{ kind: "stream", chunks: RECORDED_FRAMES, ending: "hold" }];
    const { container } = render(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);
    await flushed();
    await settle(ANNOUNCE_INTERVAL_MS);
    await flushed();

    const list = screen.getByTestId("stream-log");
    const polite = Array.from(document.querySelectorAll('[aria-live="polite"]'));
    expect(polite).toHaveLength(1);
    expect(polite[0]).toBe(screen.getByTestId("stream-announcement"));
    expect(polite[0]).not.toBe(list);
    expect(list.contains(polite[0])).toBe(false);

    // The rows keep the `log` role the design gives them but announce no insertion of their own:
    // `role="log"` is a polite live region by default, so it is turned off by name.
    expect(list.getAttribute("role")).toBe("log");
    expect(list.getAttribute("aria-live")).toBe("off");
    expect(announcementText()).toBe(`8 events arrived on this stream, newest ${NEWEST_RECORDED}`);

    mkdirSync(dirname(LIVE_REGIONS_HTML), { recursive: true });
    writeFileSync(LIVE_REGIONS_HTML, container.innerHTML, "utf8");
    log.push("StreamPanel: one polite region, and it is not the `role=log` rows' container");
  });

  it("does not auto-scroll while the operator has scrolled up", async () => {
    // The connection is dropped rather than held, so the rows the operator has not seen really
    // do arrive while the log is scrolled away: a log that never grows proves nothing about
    // whether it would have been pulled back down.
    const cut = cutAt("4");
    episodes = [{ kind: "stream", chunks: [cut.upTo.join("")], ending: "drop" }];
    mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);
    await flushed();
    expect(streamedSentences()).toEqual(sentencesOf(cut.upTo));

    const list = screen.getByTestId("stream-log");
    Object.defineProperty(list, "scrollHeight", { value: 800, configurable: true });
    Object.defineProperty(list, "clientHeight", { value: 400, configurable: true });
    list.scrollTop = 0;
    fireEvent.scroll(list);

    episodes = [{ kind: "stream", chunks: [cut.after.join("")], ending: "hold" }];
    await settle(BACKOFF_MS[0]);
    await flushed();
    await settle(ANNOUNCE_INTERVAL_MS);
    await flushed();

    expect(streamedSentences()).toEqual(sentencesOf(RECORDED_FRAMES));
    expect(list.scrollTop).toBe(0);
    log.push("StreamPanel: a log the operator has scrolled up is not pulled back down");
  });

  it("says nothing once the operator has left the page", async () => {
    // A subscription outlives the render that opened it by one read at most. Whatever that read
    // carries reaches a panel that is gone, and it must be dropped rather than rendered or
    // retried: an unmounted panel that keeps reconnecting is a leak with a cursor in it.
    episodes = [{ kind: "stream", chunks: [RECORDED_FRAMES[0]], ending: "drop" }];
    const { unmount } = mount(
      <StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} startEvent={REPLAY_START} />,
    );
    unmount();
    await flushed();
    await settle(BACKOFF_MS[BACKOFF_MS.length - 1]);
    await flushed();

    expect(screen.queryAllByTestId("trace-step")).toEqual([]);
    // One subscription, and no retry after the unmount: the cursor list did not grow.
    expect(cursors.length).toBeLessThanOrEqual(1);
    log.push("StreamPanel: a panel the operator has left renders no frame and opens no retry");
  });
});

// ---- The reader, at the four ends a connection can come to ---------------------------------------

describe("the reader", () => {
  /** Every signal one subscription produced, with `fetch` scripted by the test itself. */
  async function signalsOf(cursor?: string): Promise<StreamSignal[]> {
    const seen: StreamSignal[] = [];
    await readStream(STREAM_ROUTE, cursor, (signal) => seen.push(signal));
    return seen;
  }

  it("sends no cursor at all where there is none to send", async () => {
    // `gateway/sse.py::parse_cursor` reads a missing header as `0`, so a subscription with no
    // cursor sends no header rather than a `0` this file made up (MUST rule 21).
    const sent: (RequestInit | undefined)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        sent.push(init);
        return Promise.resolve(sseResponse([], "close"));
      }),
    );

    expect(await signalsOf(undefined)).toEqual([{ kind: "closed" }]);
    expect(sent[0]?.headers).toBeUndefined();
    log.push("lib/stream.ts: a subscription with no cursor sends no `Last-Event-ID` at all");
  });

  it("reports what the browser said for itself, and names a failure that said nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject("the socket vanished")));
    expect(await signalsOf("4")).toEqual([{ kind: "dropped", cause: "the socket vanished" }]);

    // An `Error` with no message is reported by its name, never by an invented sentence.
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error(""))));
    expect(await signalsOf("4")).toEqual([{ kind: "dropped", cause: "Error" }]);
    log.push("lib/stream.ts: a drop is reported in the words the transport used, or by its name");
  });

  it("treats a status that is neither a refusal nor a stream as a drop", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("{}", { status: 500 }))),
    );
    expect(await signalsOf("4")).toEqual([
      { kind: "dropped", cause: "the gateway answered 500" },
    ]);
    log.push("lib/stream.ts: a 500 is a drop and is retried, because 400 and 404 alone refuse");
  });

  it("emits nothing for a read that ended inside a frame, and the frame once it is whole", async () => {
    const whole = RECORDED_FRAMES[0];
    const cut = whole.indexOf("data:");
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(sseResponse([whole.slice(0, cut), whole.slice(cut)], "close"))),
    );

    const seen = await signalsOf("0");
    const framed = seen.filter((signal) => signal.kind === "frames");
    expect(framed).toHaveLength(1);
    expect(framed[0].kind === "frames" && framed[0].frames.map((frame) => frame.seq.text)).toEqual([
      "1",
    ]);
    expect(seen.at(-1)).toEqual({ kind: "closed" });
    log.push("lib/stream.ts: a read that ends mid-frame emits nothing until the frame is whole");
  });
});

// ---- The page's two halves, refused together ------------------------------------------------------

describe("the page when the gateway will not serve it", () => {
  it("shows the replay's `UnreachableState` and keeps the stream reconnecting", async () => {
    // FR-43: with no gateway running, nothing crashes and nothing shows a stale figure. A
    // rejected browser `fetch` is the transport being down (MUST rule 23), and for the stream
    // that is a drop like any other, so it waits its first second holding the cursor it has.
    down = true;
    episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
    mount(
      <>
        <Replay />
        <StreamPanel sessionId={SESSION_ID} afterSeq={LAST_REPLAYED} startEvent={REPLAY_START} />
      </>,
    );
    await flushed();

    expect(replayedSentences()).toEqual([]);
    expect(handoffs).toEqual([]);
    expect(statusText()).toBe(RECONNECTING);
    // It holds the cursor it was handed, which is what the next attempt will send.
    await settle(BACKOFF_MS[0]);
    await flushed();
    expect(cursors).toEqual([]);
    down = false;
    await settle(BACKOFF_MS[1]);
    await flushed();
    expect(cursors).toEqual([LAST_REPLAYED.text]);
    log.push("the page: a gateway that is down is an unreachable replay and a stream still retrying");
  });

  it("shows the replay's 404 beside the stream's, both in the gateway's own words", async () => {
    replay404 = true;
    episodes = [{ kind: "refused", status: 404, body: STREAM_NOT_FOUND }];
    const { container } = mount(
      <>
        <Replay />
        <StreamPanel sessionId={SESSION_ID} afterSeq={LAST_REPLAYED} />
      </>,
    );
    await flushed();

    expect(container.textContent).toContain("SurfaceNotFound");
    expect(statusText()).toBe(COULD_NOT_CONNECT);
    expect(screen.getByTestId("stream-refusal").textContent).toContain(STREAM_NOT_FOUND);
    expect(replayedSentences()).toEqual([]);
    expect(handoffs).toEqual([]);
    log.push("the page: an unknown session id is a 404 replay and `Could not connect` on the stream");
  });
});

// ---- The replay ages; the subscription does not ---------------------------------------------------

describe("the replay beside a live stream", () => {
  /** The events panel's own element: the nested transcript panel repeats these test ids. */
  const outer = (id: string): HTMLElement => screen.getAllByTestId(id)[0] as HTMLElement;

  it("goes stale on the dashboard's clock while the stream stays live (`D-5`)", async () => {
    episodes = [{ kind: "stream", chunks: [RECORDED_FRAMES[0]], ending: "hold" }];
    mount(
      <>
        <Replay />
        <StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} startEvent={REPLAY_START} />
      </>,
    );
    await flushed();
    expect(outer("panel-announcement").textContent).toBe("");
    expect(outer("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(outer("stale-badge").getAttribute("data-warn")).toBe("false");

    // A minute short of the mark is not stale: `D-5` is five minutes, not almost five.
    await settle((STALE_AFTER_MINUTES - 1) * MINUTE_MS);
    await flushed();
    expect(outer("stale-badge").getAttribute("data-warn")).toBe("false");

    // The tick that lands on the mark is the one that crosses it.
    await settle(STALE_TICK_MS);
    await flushed();
    const crossed = outer("panel-announcement").textContent ?? "";
    expect(crossed).toMatch(/^Updated \d+ min ago$/);
    expect(outer("stale-badge").textContent).toBe(crossed);
    expect(outer("stale-badge").getAttribute("data-warn")).toBe("true");
    // The replay was fetched once and ages; the subscription is still open, so it does not.
    expect(statusText()).toBe(LIVE);

    // The badge keeps counting while the announcement, said once, does not interrupt again.
    await settle(2 * MINUTE_MS);
    await flushed();
    expect(outer("panel-announcement").textContent).toBe(crossed);
    expect(outer("stale-badge").textContent).not.toBe(crossed);
    log.push("the page: the replay goes stale on the dashboard clock while the stream stays live");
  });
});

// ---- Provenance -------------------------------------------------------------------------------------

/**
 * The last replayed step's own figure: `run.finished`'s `iterations`, which its sentence reads
 * ("Finished in 1 step"). No `seq` is drawn any more (FR-129 AC2), so this is the replayed figure
 * nearest the cursor that the operator can open.
 */
const LAST_STEP_FIGURE = ".events[7].payload.iterations";

describe("provenance", () => {
  it("opens the last replayed step's figure, marked in the raw bytes", async () => {
    episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
    mount(
      <>
        <Replay />
        <StreamPanel sessionId={SESSION_ID} afterSeq={LAST_REPLAYED} startEvent={REPLAY_START} />
      </>,
    );
    await flushed();

    // The cursor the panel sent is the last replayed row's own `seq` characters.
    expect(cursors).toEqual([sourceCharactersByPath(EVENTS_BODY).get(".events[7].seq")]);

    // Opening that row's figure is what proves the characters the step shows are the characters
    // that path holds in the body (ticket 102, restated by 108), found by its own
    // `data-route`/`data-path` hooks - the figure is its own trigger.
    const trigger = figureAt(EVENTS_ROUTE, LAST_STEP_FIGURE);
    const spelled = sourceCharactersByPath(EVENTS_BODY).get(LAST_STEP_FIGURE);
    expect(trigger.textContent).toBe(spelled);
    fireEvent.click(trigger);
    await settle();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByTestId("cited").textContent).toBe(spelled);
    expect(within(dialog).getByTestId("cited").textContent).toBe(replay.at(LAST_STEP_FIGURE).text);
    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    // No endpoint string anywhere in the dialog (decision 68, `D-9`): no route, no `curl` line.
    expect(dialog.textContent).not.toContain(EVENTS_ROUTE);
    expect(screen.queryByTestId("curl")).toBeNull();
    log.push("the page: the last replayed step's figure opens the bytes it was read from, marked in place");
  });
});

// ---- The vocabulary a payload wears -------------------------------------------------------------

describe("a payload's own shape", () => {
  it("reads as the sentence `lib/trace-steps.ts` gives its name, or its name verbatim", async () => {
    episodes = [{ kind: "stream", chunks: [VOCABULARY_FRAMES.join("")], ending: "hold" }];
    const { container } = mount(<StreamPanel sessionId={SESSION_ID} afterSeq={ZERO_CURSOR} />);
    await flushed();

    const frames = parseFrames(VOCABULARY_FRAMES.join("")).frames.map((raw) =>
      frameOf(raw, STREAM_ROUTE),
    );
    const steps = screen.getAllByTestId("trace-step");
    expect(steps).toHaveLength(VOCABULARY_FRAMES.length);

    // `eval.case.finished` is in no sentence table and no frozen taxonomy, so each reads as its
    // own characters, in mono, never a guessed sentence and never a pass or fail it did not say.
    const verbatim = screen.getAllByTestId("trace-verbatim").map((node) => node.textContent);
    expect(verbatim).toEqual(["eval.case.finished", "eval.case.finished"]);
    expect(frames[0].name.text).toBe("eval.case.finished");
    expect(screen.queryAllByTestId("pass-mark-word")).toEqual([]);

    // `run.finished` with no `iterations` is the bare word, with no figure in its sentence and
    // no stop reason in it: `max_tokens`, `end_turn` and a null stop reason all read "Finished".
    expect(streamedSentences()).toEqual([
      "eval.case.finished",
      "eval.case.finished",
      "Finished",
      "Finished",
      "Finished",
    ]);
    for (const step of steps.slice(2)) {
      const sentence = within(step).getByTestId("trace-sentence");
      expect(sentence.querySelectorAll("[data-figure]")).toHaveLength(0);
    }
    expect(container.textContent).not.toContain("max_tokens");
    expect(container.textContent).not.toContain("end_turn");

    // Expanded, every field is its own `Value`: the characters the `data:` line spelled, and a
    // `null` is an absence, rendered as the word and never as `null` or `0` (`D-2`) - a stop
    // reason the ledger never recorded included.
    for (const toggle of screen.getAllByRole("button", { name: "Expand" })) fireEvent.click(toggle);
    for (const [index, frame] of frames.entries()) {
      const spelled = sourceCharactersByPath(frame.data);
      const values = within(steps[index])
        .getByTestId("trace-step-fields")
        .querySelectorAll("dd [data-figure]");
      expect(Array.from(values, (node) => node.getAttribute("data-path"))).toEqual(frame.paths);
      for (const [at, path] of frame.paths.entries()) {
        const rendered = values[at].textContent;
        expect(rendered).toBe(frame.payload.at(path).kind === "null" ? "unknown" : spelled.get(path));
      }
    }
    expect(screen.getByText("max_tokens")).toBeTruthy();
    expect(screen.getByText("end_turn")).toBeTruthy();
    expect(screen.getAllByText("unknown")).toHaveLength(2);
    expect(screen.queryByText("null")).toBeNull();
    expect(figuresWithoutSource(container)).toEqual([]);
    log.push("TraceStep: an unknown name is verbatim, a bare finish is `Finished`, a null is `unknown`");
  });
});

// ---- The transport ------------------------------------------------------------------------------

describe("the transport", () => {
  it("tells a gateway that was never reached from one that answered (MUST rule 23)", async () => {
    replayTransportDown = true;
    episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
    mount(
      <>
        <Replay />
        <StreamPanel sessionId={SESSION_ID} afterSeq={LAST_REPLAYED} startEvent={REPLAY_START} />
      </>,
    );
    await flushed();

    // A rejected fetch, not a status, is what says the process is down: the replay read that
    // rejected is `UnreachableState`, and the cause is what the browser said.
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("Failed to fetch");
    expect(replayedSentences()).toEqual([]);
    log.push("the page: a rejected fetch, never a status, is what names an unreachable gateway");
  });

  it("documents no null and no empty state for these two routes, and claims no twin", () => {
    // FR-59's payloads are complete: a `seq`, a `name` and a `ts` are always recorded, so asking
    // for a reason beside one would print a sentence next to a figure the gateway did send.
    expect(nullReasonFor(EVENTS_ROUTE, ".events[].seq")).toBeUndefined();
    expect(nullReasonFor(STREAM_ROUTE, ".id")).toBeUndefined();
    expect(absentTextFor(EVENTS_ROUTE)).toBeUndefined();
    expect(emptyTextFor(EVENTS_ROUTE)).toBeUndefined();
    // The stream's twin (ticket 232) writes the same frames, so it is a documented twin.
    expect(cliTwinFor(STREAM_ROUTE)).toBe("harnessed sessions stream <session_id>");
    // Nor for the shorter route the shell polls beside them: a template of another length
    // describes no route, so a sentence written for one page cannot leak onto another.
    expect(emptyTextFor(HEALTH_ROUTE)).toBeUndefined();
    expect(absentTextFor(HEALTH_ROUTE)).toBeUndefined();
    log.push("the page: 0 documented nulls, 0 empty sentences and 0 CLI twins on either route");
  });
});

// ---- The drawer the cursor opens ------------------------------------------------------------------

describe("the drawer a replayed step's figure opens", () => {
  async function cursorChip(): Promise<HTMLElement> {
    episodes = [{ kind: "stream", chunks: [], ending: "hold" }];
    mount(
      <>
        <Replay />
        <StreamPanel sessionId={SESSION_ID} afterSeq={LAST_REPLAYED} startEvent={REPLAY_START} />
      </>,
    );
    await flushed();
    // The figure is its own trigger now (decision 68), found by its `data-route`/`data-path`
    // hooks rather than by a `SourceChip`'s title.
    return figureAt(EVENTS_ROUTE, LAST_STEP_FIGURE);
  }

  it("closes on Esc and hands focus back to the chip that opened it (FR-61)", async () => {
    const chip = await cursorChip();
    fireEvent.click(chip);
    await settle();
    expect(screen.getByRole("dialog")).toBeTruthy();

    fireEvent.keyDown(document, { key: "Escape" });
    await settle();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(chip);
    log.push("the page: the cursor's drawer closes on Esc and returns focus to its own chip");
  });

  it("opens on Enter as well as on a click, because a chip is a button", async () => {
    const chip = await cursorChip();
    // Arrowing across the row is not asking for the drawer: only Enter opens it (ticket 107).
    fireEvent.keyDown(chip, { key: "ArrowRight" });
    await settle();
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.keyDown(chip, { key: "Enter" });
    await settle();
    expect(screen.getByRole("dialog")).toBeTruthy();
    log.push("the page: a chip opens from the keyboard on Enter alone, not on any key");
  });

  it("keeps Tab inside itself at both edges and leaves every other key alone", async () => {
    // A modal that lets Tab walk out of it is a modal only to the eye (`D-4`, FR-61). Only the
    // two edges are steered, so Tab in the middle of the drawer behaves the way it always does.
    const chip = await cursorChip();
    fireEvent.click(chip);
    await settle();
    const dialog = screen.getByRole("dialog");
    const controls = within(dialog).getAllByRole("button");
    const first = controls[0];
    const last = controls[controls.length - 1];
    expect(first).not.toBe(last);

    first.focus();
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);

    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(first);

    // Not every key is the trap's business: a letter moves nothing and closes nothing.
    fireEvent.keyDown(dialog, { key: "a" });
    expect(document.activeElement).toBe(first);
    expect(screen.getByRole("dialog")).toBeTruthy();
    log.push("the page: the cursor's drawer cycles Tab at both edges and ignores other keys");
  });

  it("offers the events route's own CLI twin as its raw access line", async () => {
    const chip = await cursorChip();
    fireEvent.click(chip);
    await settle();
    const dialog = screen.getByRole("dialog");

    // `/v1/sessions/{id}/events` names its `--json` twin since ticket 232, and raw access is that
    // command - never a `curl` line standing in for it (decision 68, `D-9`).
    expect(cliTwinFor(EVENTS_ROUTE)).toBe("harnessed sessions events <session_id> --json");
    expect(screen.getByTestId("cli-twin").textContent).toBe(cliTwinFor(EVENTS_ROUTE));
    expect(screen.queryByTestId("curl")).toBeNull();
    expect(dialog.textContent).not.toContain(GATEWAY);
    log.push("the page: a route with no CLI twin offers no raw access line in its drawer");
  });

  it("opens without motion where the operator asked for none (design.md)", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
    const chip = await cursorChip();
    fireEvent.click(chip);
    await settle();
    expect(screen.getByRole("dialog").getAttribute("data-motion")).toBe("instant");
    log.push("the page: a reduced-motion setting is honoured by the cursor's drawer");
  });
});

// ---- The row -------------------------------------------------------------------------------------

/** One hand-written frame in `gateway/sse.py::frame`'s four lines, read as a row reads it. */
function writtenFrame(seq: string, name: string, data: string) {
  const [raw] = parseFrames(`id: ${seq}\nevent: ${name}\ndata: ${data}\n\n`).frames;
  return frameOf(raw, STREAM_ROUTE);
}

describe("TraceStep", () => {
  // `loop.route.decided`: the recorded frame whose sentence carries three figures of its own.
  const [raw] = parseFrames(RECORDED_FRAMES[1]).frames;
  const frame = frameOf(raw, STREAM_ROUTE);
  const spelled = sourceCharactersByPath(frame.data);

  it("reads as one sentence whose figures are the payload's own leaves, with the replay's time", () => {
    const ts = replay.at(".events[1].ts");
    const { container } = render(
      <ul>
        <TraceStep frame={frame} ts={ts} />
      </ul>,
    );
    const sentence = screen.getByTestId("trace-sentence");
    const figures = Array.from(sentence.querySelectorAll("[data-figure]"));
    expect(figures.map((node) => node.getAttribute("data-path"))).toEqual([
      ".handler",
      ".memory_kind",
      ".confidence_percent",
    ]);
    for (const node of figures) {
      const path = node.getAttribute("data-path") ?? "";
      expect(node.getAttribute("data-route")).toBe(STREAM_ROUTE);
      expect(node.textContent).toBe(spelled.get(path));
    }
    expect(sentence.textContent).toBe(
      `Jev sent this to ${spelled.get(".handler")} (${spelled.get(".memory_kind")}) - ` +
        `${spelled.get(".confidence_percent")} sure`,
    );
    // A known name is a sentence, not its own characters: 0 raw event names and 0 `seq`s drawn.
    expect(screen.queryByTestId("trace-verbatim")).toBeNull();
    expect(container.textContent).not.toContain(frame.name.text);
    expect(screen.getByTestId("step-icon")).toBeTruthy();
    // The replay's `ts` is said relative to the dashboard's clock, from that leaf alone.
    const time = screen.getByTestId("trace-step-time").textContent;
    expect(time).toBe(humanTimestamp(ts, now()).text);
    expect(time).not.toBe("");
    expect(figuresWithoutSource(container)).toEqual([]);
    log.push("TraceStep: a known name is one sentence of the payload's own leaves, and the replay's time");
  });

  it("renders a name no sentence is written for as its own characters, in mono", () => {
    // `eval.case.finished` is outside the frozen taxonomy; the `VERBATIM` names are inside it and
    // deliberately given no sentence. Each is shown exactly as the frame spelled it.
    const [unknown] = parseFrames(VOCABULARY_FRAMES[0]).frames;
    // `memory.write.committed` has a sentence since ticket 232 (TraceStep's own table).
    const names = [unknown.event, ...VERBATIM.filter((name) => name !== "memory.write.committed")];
    expect(names).toHaveLength(3);
    for (const [index, name] of names.entries()) {
      const shown = writtenFrame(String(index), name, "{}");
      render(
        <ul>
          <TraceStep frame={shown} />
        </ul>,
      );
      expect(screen.getByTestId("trace-verbatim").textContent).toBe(name);
      expect(screen.getByTestId("trace-sentence").textContent).toBe(name);
      cleanup();
    }
    const css = readFileSync(resolve(DASHBOARD, "components/TraceStep.module.css"), "utf8");
    const verbatim = css.slice(css.indexOf(".verbatim"));
    expect(verbatim.slice(0, verbatim.indexOf("}"))).toContain("var(--type-data)");
    log.push("TraceStep: a name with no sentence is its own characters, in the mono data face");
  });

  it("says `failed` beside a step whose own status is failed", () => {
    const raised = writtenFrame("2", "error.raised", '{"class":"HarnessError","stop_reason":"raised"}');
    render(
      <ul>
        <TraceStep frame={raised} />
      </ul>,
    );
    const sentence = screen.getByTestId("trace-sentence");
    expect(sentence.querySelector("[data-figure]")?.textContent).toBe("HarnessError");
    // The word is its own element after the sentence, so it is read as a word and not run on.
    expect(sentence.lastElementChild?.textContent).toBe("failed");
    expect(sentence.textContent).toBe("Error: HarnessErrorfailed");
    expect(screen.getByTestId("step-icon-frame").getAttribute("data-status")).toBe("failed");
    log.push("TraceStep: a failed step says `failed` in words, not in colour alone");
  });

  it("has no `ts` where the four-line frame carried none, and never invents one", () => {
    render(
      <ul>
        <TraceStep frame={frame} />
      </ul>,
    );
    // `gateway/sse.py::frame` sends `id`, `event` and `data` and no timestamp, so the cell is
    // empty rather than filled from the dashboard's own clock.
    expect(screen.getByTestId("trace-step-time").textContent).toBe("");
    log.push("TraceStep: a streamed frame carries no `ts`, so the time is empty and not the clock");
  });

  it("expands to every payload field, each its own leaf of the `data:` line", () => {
    const { container } = render(
      <ul>
        <TraceStep frame={frame} />
      </ul>,
    );
    const toggle = screen.getByRole("button", { name: "Expand" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("trace-step-fields")).toBeNull();
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    // Every path the `data:` line holds, in its order, each with the characters spelled there.
    const fields = screen.getByTestId("trace-step-fields");
    expect(frame.paths).toEqual([...spelled.keys()]);
    expect(Array.from(fields.querySelectorAll("dt"), (node) => node.textContent)).toEqual(
      frame.paths,
    );
    const values = Array.from(fields.querySelectorAll("dd"));
    expect(values).toHaveLength(frame.paths.length);
    for (const [index, path] of frame.paths.entries()) {
      const figure = values[index].querySelector("[data-figure]");
      expect(figure?.getAttribute("data-path")).toBe(path);
      expect(figure?.textContent).toBe(spelled.get(path));
    }
    expect(figuresWithoutSource(container)).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Collapse" }));
    expect(screen.queryByTestId("trace-step-fields")).toBeNull();

    // A payload with no fields says so, rather than drawing an empty list.
    cleanup();
    render(
      <ul>
        <TraceStep frame={writtenFrame("1", "run.started", "{}")} />
      </ul>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(screen.queryByTestId("trace-step-fields")).toBeNull();
    expect(screen.getByText("No payload fields.")).toBeTruthy();
    log.push("TraceStep: expanded, every payload field is listed with its own leaf, verbatim");
  });

  it("fades on arrival with opacity alone, and not at all under reduced motion", () => {
    // jsdom loads no stylesheet, so the claim is read off the component's own module CSS, as
    // ticket 104 established for the mono claim.
    const text = readFileSync(resolve(DASHBOARD, "components/TraceStep.module.css"), "utf8");
    expect(text).toContain("prefers-reduced-motion: reduce");
    // Opacity only: the fade moves nothing and recolours nothing while it runs.
    const keyframes = text.slice(text.indexOf("@keyframes"));
    expect(keyframes).toContain("opacity");
    expect(keyframes).not.toContain("transform");
    expect(keyframes).not.toContain("background");
    render(
      <ul>
        <TraceStep frame={frame} arrived />
      </ul>,
    );
    expect(screen.getByTestId("trace-step").getAttribute("data-arrived")).toBe("");
    cleanup();
    render(
      <ul>
        <TraceStep frame={frame} />
      </ul>,
    );
    expect(screen.getByTestId("trace-step").hasAttribute("data-arrived")).toBe(false);
    log.push("TraceStep: the 1 s arrival fade is opacity only and off under reduced motion");
  });
});

// ---- Helpers used above --------------------------------------------------------------------------

/**
 * The cursor of a subscription that starts at the beginning. `gateway/sse.py::parse_cursor`
 * reads no header as `0`, so this is a leaf of the recorded replay's own `seq` spelling.
 */
const ZERO_CURSOR = parseWire('{"seq":"0"}', EVENTS_ROUTE).at(".seq");

/** The improve session's start name, as a leaf, for the pairing the panel is handed. */
const IMPROVE_START_LEAF = parseWire(JSON.stringify({ name: IMPROVE_START }), STREAM_ROUTE).at(
  ".name",
);

/** Write one panel's rendered DOM where the ticket's evidence manifest says it goes. */
function writeHtml(path: string, container: ParentNode): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, (container as HTMLElement).innerHTML, "utf8");
}

// ---- Evidence --------------------------------------------------------------------------------------

describe("evidence", () => {
  it("writes the manifest this ticket declares", () => {
    expect(log.length).toBeGreaterThan(10);
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});
