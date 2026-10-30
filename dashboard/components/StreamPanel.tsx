"use client";

/**
 * The live half of a session page: one subscription, and a log that loses nothing (FR-59, UJ-11).
 *
 * It opens `GET /v1/sessions/{id}/stream` through `lib/stream.ts` with `Last-Event-ID` equal to
 * `afterSeq`'s own characters - the last `seq` the replay rendered - so the first frame it shows
 * is the one after it. The cursor moves only when a frame arrives and is only ever a frame's own
 * `seq` text: this component computes nothing, which is why the backoff table, the frame parser
 * and the scroll predicate all live in `lib/stream.ts`, the file decision 32 allowlists.
 *
 * **A drop is a wait and then the same cursor again**: 1 s, 2 s, 4 s, then 10 s at most, saying
 * `reconnecting (cursor N)` while it waits, and the gateway replays from N, so nothing is lost
 * and nothing is repeated. A 400 or 404 is the gateway refusing this subscription rather than
 * failing to serve it, so it stops and shows the body it was refused with.
 *
 * **A close is named by pairing, never by a name** (decision 17, MUST rule 20). `startEvent` is
 * the replay's `seq` 1 name where the caller has one; where the stream itself delivers `seq` 1,
 * that frame is the pair. With neither, a clean close is `closed: idle ceiling`, because this
 * panel may not decide for itself which finish ended the session.
 *
 * **A row lands when it arrives; only the announcement batches** (decision 30a, ticket 120). The
 * rows go into the DOM on the read that carried them, so a frame never waits out a cooldown it
 * missed the start of, and the batching that FR-59 asks for is applied to what is *said*: a
 * visually-hidden polite region beside the log carries one summary per `ANNOUNCE_INTERVAL_MS`,
 * naming how many frames this panel received and the newest one's name as the frame spelled it.
 * That batching is `useAnnouncement`'s, the same one the architecture diagram says its frames with.
 * The rows' scroll container keeps the `log` role and announces nothing itself; the rows
 * themselves sit in a plain `<ul>` one level in, so the `<li>` steps still read as a list to an
 * axe/WCAG check (a `role="log"` element is not itself list-owning). The log follows the newest
 * row only while the operator is already at the bottom of it.
 *
 * **Each row is a `TraceStep`** (FR-129, decision 68), and the header names a status word instead
 * of the cursor `lib/stream.ts::statusLabel` would print: **0 cursors or `seq`s are drawn**
 * (FR-129 AC2). `statusWord` below reads the same `StreamStatus` `lib/stream.ts` already computes
 * (the file is reused unchanged, decision 30a) and says it in the words this ticket's own Context
 * names - "Live", "Done", "Reconnecting", "Could not connect" - plus "Waiting for the first step"
 * before anything has arrived (design.md's `TraceTimeline` states column). A refusal still stops
 * every retry, as it always has; `Refresh` is a **manual** reconnect the operator can still ask
 * for, a new affordance this ticket adds where none stood before.
 */
import { useEffect, useRef, useState } from "react";

import {
  FIRST_SEQ,
  type StreamFrame,
  type StreamStatus,
  atBottom,
  closeReason,
  nextBackoffMs,
  readStream,
  streamRoute,
} from "../lib/stream";
import type { Leaf } from "../lib/wire";

import styles from "./StreamPanel.module.css";
import { TraceStep } from "./TraceStep";
import { useAnnouncement } from "./useAnnouncement";

/**
 * The status word (FR-129, this ticket's own Context and design.md's `TraceTimeline` states),
 * read off the same `StreamStatus` `lib/stream.ts` computes and never off `statusLabel`, which
 * still spells a cursor into `reconnecting (cursor N)` (that file is scope_out and unchanged).
 * `everShown` tells "nothing has landed yet" (waiting) from "a retry is quietly under way behind
 * rows already on screen" (still `Live`), which `status.kind === "connecting"` alone cannot.
 */
export function statusWord(status: StreamStatus, everShown: boolean): string {
  if (status.kind === "connecting") return everShown ? "Live" : "Waiting for the first step";
  if (status.kind === "live") return "Live";
  if (status.kind === "reconnecting") return "Reconnecting";
  if (status.kind === "closed") return "Done";
  return "Could not connect";
}

type Props = {
  sessionId: string;
  /** The last `seq` already rendered. Its text is the cursor, sent verbatim; absent starts at `seq` 1. */
  afterSeq?: Leaf;
  /** The replay's `seq` 1 name, where the caller replayed one (decision 17's start half). */
  startEvent?: Leaf;
};

export function StreamPanel({ sessionId, afterSeq, startEvent }: Props) {
  const route = streamRoute(sessionId);
  const [rows, setRows] = useState<readonly StreamFrame[]>([]);
  const [arrived, setArrived] = useState<ReadonlySet<string>>(() => new Set());
  /** The batched summary, and the only thing on this panel a screen reader is interrupted for. */
  const { announced, arrive } = useAnnouncement();
  const [status, setStatus] = useState<StreamStatus>({ kind: "connecting" });
  // A fresh token is what a reconnect is: a new subscription, not an increment of an old one.
  const [attempt, setAttempt] = useState<object>({});

  const cursor = useRef(afterSeq?.text);
  const start = useRef<string | undefined>(startEvent?.text);
  const last = useRef<string | undefined>(undefined);
  const waited = useRef<number | undefined>(undefined);
  const log = useRef<HTMLDivElement | null>(null);
  const following = useRef(true);

  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    let retry: ReturnType<typeof setTimeout> | undefined;

    void readStream(
      route,
      cursor.current,
      (signal) => {
        if (!live) return;
        if (signal.kind === "frames") {
          const landing = signal.frames;
          for (const frame of landing) {
            cursor.current = frame.seq.text;
            last.current = frame.name.text;
            if (frame.seq.text === FIRST_SEQ) start.current = frame.name.text;
          }
          arrive(landing);
          // The read is the update: these rows are in the DOM now, and the fade marks exactly
          // the ones that just landed rather than everything a cooldown had collected.
          setRows((seen) => [...seen, ...landing]);
          setArrived(new Set(landing.map((frame) => frame.seq.text)));
          waited.current = undefined;
          setStatus({ kind: "live" });
          return;
        }
        if (signal.kind === "refused") {
          setStatus({ kind: "refused", status: signal.status, body: signal.body });
          return;
        }
        if (signal.kind === "closed") {
          setStatus({ kind: "closed", reason: closeReason(start.current, last.current) });
          return;
        }
        const wait = nextBackoffMs(waited.current);
        waited.current = wait;
        // `StreamStatus` wants a string; this panel draws no cursor, and none exists before a first frame.
        setStatus({ kind: "reconnecting", cursor: cursor.current ?? "" });
        retry = setTimeout(() => {
          if (!live) return;
          setStatus({ kind: "connecting" });
          setAttempt({});
        }, wait);
      },
      controller.signal,
    );

    return () => {
      live = false;
      controller.abort();
      if (retry !== undefined) clearTimeout(retry);
    };
  }, [route, attempt, arrive]);

  /* The one place the log is scrolled, and only where the operator had not scrolled away. */
  useEffect(() => {
    const list = log.current;
    if (list === null || !following.current) return;
    list.scrollTop = list.scrollHeight;
  }, [rows]);

  return (
    <section className={styles.panel} aria-label="Session stream">
      <header className={styles.header}>
        <span className={styles.status} data-testid="stream-status">
          {statusWord(status, rows.length > 0)}
        </span>
        {status.kind === "refused" ? (
          <button type="button" className={styles.refresh} onClick={() => setAttempt({})}>
            Refresh
          </button>
        ) : null}
      </header>
      {status.kind === "refused" ? (
        <div className={styles.refusal} data-testid="stream-refusal">
          <p className={styles.refusalLine}>
            The gateway refused this subscription and will refuse the next one until asked to try
            again.
          </p>
          <pre className={styles.refusalBody} data-testid="raw-body">
            {status.body}
          </pre>
        </div>
      ) : null}
      <p
        className={styles.announcement}
        role="status"
        aria-live="polite"
        data-testid="stream-announcement"
      >
        {announced}
      </p>
      <div
        className={styles.log}
        role="log"
        // `role="log"` is a polite live region by default, so the rows say so themselves unless
        // told not to: the batched summary above is the one thing on this panel that announces.
        // The rows sit in a plain `<ul>` one level down: a `role="log"` element is not itself a
        // list per WCAG's listitem containment (axe's `listitem` rule), so the log role and the
        // list role live on two different elements rather than one overriding the other.
        aria-live="off"
        aria-label="events"
        data-testid="stream-log"
        ref={log}
        onScroll={(event) => {
          following.current = atBottom(event.currentTarget);
        }}
      >
        <ul className={styles.list}>
          {rows.map((frame) => (
            <TraceStep key={frame.seq.text} frame={frame} arrived={arrived.has(frame.seq.text)} />
          ))}
        </ul>
      </div>
    </section>
  );
}
