/**
 * One session id's SSE stream, read through `fetch` and a `ReadableStream` (decision 34, FR-59).
 *
 * `EventSource` cannot send the first `Last-Event-ID`, and that header is the whole point: the
 * replay renders the ledger up to `seq` N and the stream must start at N + 1, so the handoff
 * loses nothing and repeats nothing. So the four-line frame of decision 20 - `id:`, `event:`,
 * `data:`, blank - is parsed here, off a byte reader, and the cursor is carried in a header.
 *
 * **This file is one of the two the decision 32 lint allowlists by name, and it never does
 * arithmetic on a gateway value.** The only numbers it owns are its own reconnect delays and the
 * scroll offsets the browser reports for the log element - the dashboard's own machine, exactly
 * as `lib/clock.ts` is. A `seq` is a `Leaf`'s text and is passed on verbatim: the cursor a
 * reconnect sends is the characters the last rendered frame spelled, never `seq + 1` (MUST rule
 * 21). The gateway is the one that knows what comes after N.
 *
 * **A close is read by pairing, never by a name** (decision 17, MUST rule 20). A session ends on
 * the finish paired with the start at its `seq` 1, so an improve session's inner
 * `eval.run.finished` frames do not end it; where this panel never saw `seq` 1 and was told no
 * start event, a clean close is the idle ceiling, because nothing here may guess which finish
 * was the session's own.
 */
import { parseWire, type Leaf, type Wire } from "./wire";

/** The cursor header decision 20 reads and `EventSource` cannot set on its first request. */
export const LAST_EVENT_ID = "Last-Event-ID";

/** The `seq` the gateway stamps on a session's first event: the start half of decision 17's pair. */
export const FIRST_SEQ = "1";

/** The route table's spelling, with the session id placed in it verbatim (MUST rule 25). */
export function streamRoute(sessionId: string): string {
  return `/v1/sessions/${sessionId}/stream`;
}

/**
 * The reconnect delays, in order, the last one repeating: 1 s, 2 s, 4 s, then 10 s at most
 * (FR-59, decision 34). Written out rather than doubled, so the cap is a value in a table and
 * not a `Math.min` over a product.
 */
export const BACKOFF_MS: readonly number[] = [1000, 2000, 4000, 10000];

/**
 * How long arrivals are collected into one polite announcement (FR-59, decision 30a).
 *
 * It batches what is *said*, never when a row lands: the rows go in on arrival, and this is the
 * cooldown on the summary beside them. Still 1000 - this is not a configurable knob.
 */
export const ANNOUNCE_INTERVAL_MS = 1000;

/** The wait after a drop, given the wait before it. An unbroken run of drops ends at the cap. */
export function nextBackoffMs(previous?: number): number {
  const first = BACKOFF_MS[0];
  const last = BACKOFF_MS[BACKOFF_MS.length - 1];
  if (previous === undefined) return first;
  return BACKOFF_MS[BACKOFF_MS.indexOf(previous) + 1] ?? last;
}

/**
 * The start event at a session's `seq` 1 and the finish that ends that session, as
 * `events/read.py::TERMINAL_PAIRS` spells them. A finish that is not this session's start's
 * partner is an ordinary frame.
 */
export const TERMINAL_PAIRS: ReadonlyMap<string, string> = new Map([
  ["run.started", "run.finished"],
  ["eval.run.started", "eval.run.finished"],
  ["improve.run.started", "improve.run.finished"],
  ["task.finished", "task.finished"],
  ["memory.consolidation.finished", "memory.consolidation.finished"],
]);

/** What a clean close is called when nothing pairs: `gateway/sse.py`'s `IDLE_CEILING_S`. */
export const IDLE_CEILING = "idle ceiling";

/** The statuses that mean the gateway refused this subscription and will refuse the next one. */
const REFUSING: ReadonlySet<string> = new Set(["400", "404"]);

/** The frame field a `seq` figure cites, so the path is spelled in one place. */
export const SEQ_PATH = ".id";
/** The frame field a `name` figure cites. */
export const NAME_PATH = ".event";

const ID_FIELD = "id";
const EVENT_FIELD = "event";
const DATA_FIELD = "data";

/** A frame ends at a blank line, which is a second newline (decision 20). */
const FRAME_SEPARATOR = "\n\n";
const LINE_SEPARATOR = "\n";

/** One frame as its four lines spelled it, before any of it is read as a figure. */
export type RawFrame = { id: string; event: string; data: string };

/** One frame as a row renders it: two leaves of its own, and its payload's leaves by path. */
export type StreamFrame = {
  /** The `id:` line, verbatim. The cursor a reconnect sends is this text. */
  seq: Leaf;
  /** The `event:` line, verbatim, from `events/names.py`. */
  name: Leaf;
  /** The `data:` line, parsed, so every figure in the preview is a `Leaf` (MUST rule 21). */
  payload: Wire;
  /** Every jq path the payload holds a primitive at, in the order the line spells them. */
  paths: readonly string[];
  /** The `data:` line's own characters, for the expanded `JsonBlock`. */
  data: string;
};

/** Where a stream is, in the words `design.md` gives `StreamPanel`. */
export type StreamStatus =
  | { kind: "connecting" }
  | { kind: "live" }
  | { kind: "reconnecting"; cursor: string }
  | { kind: "closed"; reason: string }
  | { kind: "refused"; status: string; body: string };

/**
 * What one read of the stream can say. Exactly one of these ends a connection.
 *
 * Frames come a read at a time rather than one at a time, because a read is what arrived: a
 * chunk holding six frames is six rows in one update, landing together because they landed
 * together.
 */
export type StreamSignal =
  | { kind: "frames"; frames: readonly StreamFrame[] }
  | { kind: "refused"; status: string; body: string }
  | { kind: "closed" }
  | { kind: "dropped"; cause: string };

/** The sentence the panel header shows and announces, for each state. */
export function statusLabel(status: StreamStatus): string {
  if (status.kind === "reconnecting") return `reconnecting (cursor ${status.cursor})`;
  if (status.kind === "closed") return `closed: ${status.reason}`;
  if (status.kind === "refused") return `refused ${status.status}`;
  return status.kind;
}

/**
 * What the panel announces after a batch of arrivals, in words an operator can act on.
 *
 * Two things: how many frames arrived, and the newest one's name. The count is a tally of what
 * this panel itself received - the dashboard's own number, like a scroll offset or a fetch time,
 * said as the panel's own ("arrived on this stream") and never dressed as a field the gateway
 * sent. The name is the frame's `event:` line character for character, because `D-7` shows the
 * ledger's word and never a friendlier one.
 *
 * An empty batch says nothing at all: a live region repeating itself with no news is the
 * flooding FR-59's cap exists to stop.
 */
export function announcementFor(frames: readonly StreamFrame[]): string {
  const newest = frames.at(-1);
  if (newest === undefined) return "";
  const arrivals = frames.length === 1 ? "1 event" : `${frames.length} events`;
  return `${arrivals} arrived on this stream, newest ${newest.name.text}`;
}

/**
 * Why a clean close happened: the session's own terminal event, named, or the idle ceiling.
 *
 * `startName` is the name at `seq` 1 - from the replay the panel handed off from, or from the
 * `seq` 1 frame where the stream itself delivered it. Without one there is no pair, so there is
 * no terminal: naming a finish from its name alone is what MUST rule 20 forbids.
 */
export function closeReason(startName: string | undefined, lastName: string | undefined): string {
  const terminal = startName === undefined ? undefined : TERMINAL_PAIRS.get(startName);
  if (terminal !== undefined && lastName === terminal) return `terminal ${terminal}`;
  return IDLE_CEILING;
}

/** The value of one `field:` line, with the single optional space after the colon removed. */
function fieldValue(line: string, field: string): string | undefined {
  const head = `${field}:`;
  if (!line.startsWith(head)) return undefined;
  const raw = line.slice(head.length);
  return raw.startsWith(" ") ? raw.slice(" ".length) : raw;
}

/** One `id`/`event`/`data` block, or `undefined` where a block is not a whole frame. */
function frameOfBlock(block: string): RawFrame | undefined {
  let id: string | undefined;
  let event: string | undefined;
  let data: string | undefined;
  for (const line of block.split(LINE_SEPARATOR)) {
    id = id ?? fieldValue(line, ID_FIELD);
    event = event ?? fieldValue(line, EVENT_FIELD);
    data = data ?? fieldValue(line, DATA_FIELD);
  }
  if (id === undefined || event === undefined || data === undefined) return undefined;
  return { id, event, data };
}

/**
 * Every whole frame in `buffer`, and the characters after the last blank line.
 *
 * A read can end anywhere, including inside a `data:` line, so the tail is handed back rather
 * than parsed: a half-arrived frame is not a frame, and the next read finishes it.
 */
export function parseFrames(buffer: string): { frames: readonly RawFrame[]; rest: string } {
  const blocks = buffer.split(FRAME_SEPARATOR);
  const rest = blocks.pop() ?? "";
  const frames: RawFrame[] = [];
  for (const block of blocks) {
    const found = frameOfBlock(block);
    if (found !== undefined) frames.push(found);
  }
  return { frames, rest };
}

/** Every jq path holding a primitive in `text`, in the order the body spells them. */
export function payloadPaths(text: string): readonly string[] {
  const found: string[] = [];
  try {
    collectPaths(JSON.parse(text) as unknown, "", found);
  } catch {
    return [];
  }
  return found;
}

function collectPaths(node: unknown, path: string, into: string[]): void {
  if (Array.isArray(node)) {
    for (const [index, item] of node.entries()) collectPaths(item, `${path}[${index}]`, into);
    return;
  }
  if (typeof node === "object" && node !== null) {
    for (const [key, item] of Object.entries(node)) collectPaths(item, `${path}.${key}`, into);
    return;
  }
  into.push(path);
}

/** The frame as leaves: the two lines that are not JSON, and the payload parsed as a `Wire`. */
export function frameOf(raw: RawFrame, route: string): StreamFrame {
  return {
    seq: { kind: "string", text: raw.id, path: SEQ_PATH, route },
    name: { kind: "string", text: raw.event, path: NAME_PATH, route },
    payload: parseWire(raw.data, route),
    paths: payloadPaths(raw.data),
    data: raw.data,
  };
}

/** What a rejected `fetch` or a broken reader can say for itself, reported as itself. */
function causeOf(thrown: unknown): string {
  if (!(thrown instanceof Error)) return String(thrown);
  return thrown.message ? thrown.message : thrown.name;
}

/**
 * Read one subscription to its end, reporting every frame and the way it ended.
 *
 * @param route the gateway route, as `streamRoute` spells it
 * @param cursor the last `seq` already rendered, sent verbatim as `Last-Event-ID`
 * @param emit called once per read that held whole frames, and once for the end of the connection
 * @param signal aborts the read when the panel unmounts or reconnects
 */
export async function readStream(
  route: string,
  cursor: string | undefined,
  emit: (signal: StreamSignal) => void,
  signal?: AbortSignal,
): Promise<void> {
  let received: Response;
  try {
    received = await fetch(route, {
      cache: "no-store",
      headers: cursor === undefined ? undefined : { [LAST_EVENT_ID]: cursor },
      signal,
    });
  } catch (thrown) {
    emit({ kind: "dropped", cause: causeOf(thrown) });
    return;
  }

  const status = String(received.status);
  if (REFUSING.has(status)) {
    emit({ kind: "refused", status, body: await received.text() });
    return;
  }

  const body = received.body;
  if (!received.ok || body === null) {
    emit({ kind: "dropped", cause: `the gateway answered ${status}` });
    return;
  }

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const arrived = await reader.read();
      if (arrived.done) break;
      buffer = `${buffer}${decoder.decode(arrived.value, { stream: true })}`;
      const read = parseFrames(buffer);
      buffer = read.rest;
      if (read.frames.length === 0) continue;
      emit({ kind: "frames", frames: read.frames.map((raw) => frameOf(raw, route)) });
    }
  } catch (thrown) {
    emit({ kind: "dropped", cause: causeOf(thrown) });
    return;
  }
  emit({ kind: "closed" });
}

/** What the browser reports about the log element, so the predicate below can be tested. */
export type ScrollBox = { scrollTop: number; scrollHeight: number; clientHeight: number };

/** A row's worth of rounding slack, so a fractional scroll offset is still "at the bottom". */
const SCROLL_SLACK_PX = 2;

/**
 * Is the log scrolled to its last row? The dashboard's own layout and not a gateway value, which
 * is why the subtraction is in this file and not in the component (decision 32).
 */
export function atBottom(box: ScrollBox): boolean {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= SCROLL_SLACK_PX;
}
