"use client";

/**
 * The console: `Conversation` over the run's own messages, `Composer` pinned beneath it (FR-122,
 * FR-128, UJ-16; design.md "The console in v5"). `ConsolePanel` owns nothing of what is said - it
 * only knows a session id and whether a run is in flight on it, both of which every figure below
 * still reads from a gateway response, never from state this component invented.
 *
 * **A run is watched by its own stream, opened at the previous message's served `last_seq`**
 * (decision 17, MUST rule 20): the gateway closes a stream at the first `run.finished` after its
 * cursor, so a watch opened there ends on **this** message's finish. The first message of a
 * session has no cursor and starts at `seq` 1. The watch opens once `Conversation` has reported
 * what it served (`onServed`); a report with no live message means the session is at rest. This is a
 * second, independent subscription to the same session's stream `Exchange`'s own live
 * `TraceTimeline` opens for its steps (ticket 187's own precedent: decision 14 rules out one
 * stream shared *across pages*, not a page holding two subscriptions to the session it watches for
 * two different reasons - here, "is the run still going" and "the rendered trace").
 *
 * A frame named `loop.route.decided`, `error.raised` or `run.finished`, and the watch's own end,
 * bump a refetch signal `Conversation` is handed as `refresh`, so the conversation's own reads (`/v1/sessions/{id}/messages`, `/v1/sessions/{id}/decisions`) catch up
 * as the run progresses (the Overview's "SSE-for-change, fetch-to-read" pattern, FR-75) - a frame
 * is a signal, never a figure (MUST rule 21). The one frame this watch reads for its own payload is
 * `error.raised` (FR-92): a run that raised wrote no turn, so the messages read alone cannot show
 * why, and `RaisedState` renders in `Conversation`'s place until the next send.
 *
 * **The side rail** (ticket 189, design.md "The console in v5"): "This session" is a `CostLine`
 * over `GET /v1/spend?session=`, shown once a session is open. It is rendered here, beside the
 * conversation, rather than lifted to `app/console/page.tsx`, because the page only ever knows the
 * session the URL named - the id a brand-new send opens lives in this component's own `opened`
 * state until the URL catches up, and the side rail has to track the same id the conversation
 * does or it would show nothing through a run the operator can already see answered.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { nextBackoffMs, readStream, streamRoute, type StreamFrame } from "../lib/stream";
import type { Leaf, Wire } from "../lib/wire";

import {
  Composer,
  COMPOSER_LABEL,
  IN_FLIGHT_REASON,
  type Opened,
  PLACEHOLDER,
  READING,
  SEND,
  SENDING,
  taskNote,
} from "./Composer";
import { type Arrival, Conversation, Sidechat, type Served } from "./Conversation";
import { CostLine } from "./CostLine";
import { ErrorState } from "./ErrorState";
import { PanelFrame } from "./PanelFrame";
import { TaskHeader } from "./TaskHeader";
import { TaskRail, type TaskRow } from "./TaskRail";
import styles from "./ConsolePanel.module.css";

export { COMPOSER_LABEL, IN_FLIGHT_REASON, PLACEHOLDER, READING, SEND, SENDING };

const SIDE_RAIL_TITLE = "This session";

/** `.handlers`, scoped so `CostLine`'s `[N]` addresses one row (decision 42, `Conversation.tsx`'s
 * own `rowWire` precedent, repeated here since the two files share no import for it). */
function rowWire(wire: Wire, prefix: string): Wire {
  return {
    route: wire.route,
    text: wire.text,
    byteLength: wire.byteLength,
    at: (path: string) => wire.at(`${prefix}${path}`),
  };
}

/** The session's own spend read, its id placed verbatim (MUST rule 25). */
function spendSessionRoute(sessionId: string): string {
  return `/v1/spend?session=${encodeURIComponent(sessionId)}`;
}

/** Who answers a raised run's own error: the composition, not a gateway read (MUST rule 23). */
const CONTROL_APP = "control app";

/** `run_cmd.settle_raised`'s first publish (FR-92): the one frame name this watch reads for itself. */
const ERROR_RAISED = "error.raised";

/** The recorded frame a pending request announces itself with, naming its exchange by `message_seq`. */
const PERMISSION_REQUEST = "acp.permission.request";

/** The other frames whose arrival is a signal to read the messages again (a frame is never a figure). */
const REFRESHING_FRAMES: ReadonlySet<string> = new Set(["loop.route.decided", ERROR_RAISED, "run.finished", PERMISSION_REQUEST]);

/** `error.raised`'s own fields, `settle_raised` publishes verbatim (decision 49 as amended). */
const RAISED_CLASS_PATH = ".class";
const RAISED_MESSAGE_PATH = ".message";
const RAISED_STOP_REASON_PATH = ".stop_reason";

type Props = {
  /** A session to open at rest, taken verbatim from a response or the URL (MUST rule 25). */
  sessionId: string | null;
};

export function ConsolePanel({ sessionId }: Props) {
  const [opened, setOpened] = useState<Opened | undefined>(undefined);
  // The run this page is watching; `undefined` once its stream has ended, which is "at rest".
  const [watching, setWatching] = useState<object | undefined>(() => (sessionId === null ? undefined : {}));
  // Set from the one frame named `error.raised`, if the watched stream carried one (FR-92).
  const [arrivals, setArrivals] = useState<readonly Arrival[]>([]);
  const [raised, setRaised] = useState<Wire | undefined>(undefined);
  // The cursor the watch opens at, from what `Conversation` served; `undefined` until it reports.
  const [cursor, setCursor] = useState<{ seq: string | undefined } | undefined>(undefined);
  // A new object on every refreshing frame and at the watch's end: `Conversation`'s own refetch signal.
  const [refresh, setRefresh] = useState<object>({});
  // One key per accepted send: a follow-up remounts the conversation and the watch alike.
  const [watch, setWatch] = useState("opened");
  // Sidechat's open state lives here so it survives the `key={watch}` remount (ticket 210).
  const [sidechatOpen, setSidechatOpen] = useState(false);
  // Whether the conversation's read has landed in this mount, and whether it served any rows (ticket 216).
  const [settled, setSettled] = useState(false);
  const [hasRows, setHasRows] = useState(false);
  // The task this session belongs to, from the conversation's own read, and what the task's own
  // read last said (FR-154): the header, the rail, the composer and the repository card use them.
  const [taskId, setTaskId] = useState<Leaf | undefined>(undefined);
  const [taskRow, setTaskRow] = useState<TaskRow | undefined>(undefined);

  const session = opened === undefined ? sessionId : opened.wire.at(".session_id").text;
  const inFlight = watching !== undefined;
  const atRest = useCallback(() => {
    setWatching(undefined);
    setRefresh({});
  }, []);
  const onFrame = useCallback((frame: StreamFrame) => {
    if (frame.name.text === ERROR_RAISED) setRaised(frame.payload);
    if (frame.name.text === PERMISSION_REQUEST) {
      // Placed on its exchange now; the figures stay the permissions read's, which this frame also asks for.
      const arrival = { permissionId: frame.payload.at(".permission_id").text, messageSeq: frame.payload.at(".message_seq").text };
      setArrivals((previous) => [...previous.filter((entry) => entry.permissionId !== arrival.permissionId), arrival]);
    }
    if (REFRESHING_FRAMES.has(frame.name.text)) setRefresh({});
  }, []);
  const onSettled = useCallback(() => setSettled(true), []);
  const onServed = useCallback((served: Served) => {
    setHasRows(true);
    setTaskId((previous) => (previous?.text === served.task?.text ? previous : served.task));
    if (!served.live) {
      setWatching(undefined);
      return;
    }
    const seq = served.afterSeq?.text;
    setCursor((previous) => (previous !== undefined && previous.seq === seq ? previous : { seq }));
  }, []);

  useRunWatch(session, watching, cursor, atRest, onFrame);

  // An accepted permission answer is a signal to read again, like a frame: never a figure (FR-75).
  const onAnswered = useCallback(() => setRefresh({}), []);

  function onSent(result: Opened): void {
    setOpened(result);
    setWatching({});
    setCursor(undefined);
    setRaised(undefined);
    setArrivals([]);
    setRefresh({});
    setSidechatOpen(true);
    setSettled(false);
    setWatch(crypto.randomUUID());
  }

  const note = taskNote(taskRow);
  const sidechat = (id: string) => ({
    session: id,
    task: taskRow,
    // With no rows served (loading, error, empty) there is nothing to continue from but the
    // composer itself, so it is open; the button is for a served conversation (ticket 216).
    open: sidechatOpen || !hasRows || note !== undefined,
    onOpen: () => setSidechatOpen(true),
    // Before the first read lands (or a send), a page opened on a session has not shown a run:
    // only a served message or an accepted send makes it "working" rather than "reading".
    inFlight: inFlight && (hasRows || opened !== undefined),
    reading: !settled && !(inFlight && opened !== undefined),
    onSent,
  });

  return (
    <div className={styles.layout}>
      {/* A `section`, so the main column is one panel beside the side rail's: Tab reads the
          column top to bottom, then the rail, the way the eye reads the two (FR-61). */}
      <section className={styles.console}>
        {session === null ? null : (
          <div className={styles.conversationArea} key={watch}>
            {taskId === undefined ? null : <TaskHeader taskId={taskId} refresh={refresh} />}
            {raised !== undefined ? (
              <RaisedState payload={raised} />
            ) : (
              <Conversation
                sessionId={session}
                refresh={refresh}
                onServed={onServed}
                onSettled={onSettled}
                taskCwd={taskRow?.cwd}
                taskEntry={taskRow?.entry}
                taskQueued={taskRow?.created}
                taskState={taskRow}
                onAnswered={onAnswered}
                arrivals={arrivals}
              />
            )}
            <Sidechat {...sidechat(session)} />
          </div>
        )}
        {session === null ? <Composer session={null} inFlight={inFlight} onSent={onSent} /> : null}
      </section>
      {session === null ? null : (
        <SideRail sessionId={session} refresh={refresh} taskId={taskId} onTaskRow={setTaskRow} />
      )}
    </div>
  );
}

/** design.md's "This session": a `CostLine` over the session's own spend, refetched on the same
 * signal the conversation reads again on (`refresh`, FR-75's SSE-for-change pattern). `PanelFrame`
 * itself is the one heading ("This session"), so the `aside` wrapping it names nothing twice. */
function SideRail({
  sessionId,
  refresh,
  taskId,
  onTaskRow,
}: {
  sessionId: string;
  refresh: object;
  taskId: Leaf | undefined;
  onTaskRow: (row: TaskRow) => void;
}) {
  return (
    <aside className={styles.sideRail}>
      {taskId === undefined ? null : <TaskRail taskId={taskId} refresh={refresh} onRow={onTaskRow} />}
      <PanelFrame
        route={spendSessionRoute(sessionId)}
        title={SIDE_RAIL_TITLE}
        sourceLabel="Ledger: this session"
        refresh={refresh}
      >
        {(wire) => (
          <CostLine
            handlers={rowWire(wire, ".handlers")}
            total={wire.at(".total")}
            apiEquivalent={wire.at(".api_equivalent")}
          />
        )}
      </PanelFrame>
    </aside>
  );
}

// ---- A run that raised (FR-92) --------------------------------------------------------------

/**
 * `error.raised`'s three fields, read once outside any component and handed on as plain strings -
 * the same verbatim-quote extraction `control-client.ts` and `fetch-panel.ts::errorBody` already
 * do for a decision 24 body, applied to a frame instead of a response (decision 42: a `Leaf`'s
 * text may travel as a component's prop only through a function that is not one).
 */
function raisedOf(payload: Wire): { status: string; error: string; message: string } {
  return {
    status: payload.at(RAISED_STOP_REASON_PATH).text,
    error: payload.at(RAISED_CLASS_PATH).text,
    message: payload.at(RAISED_MESSAGE_PATH).text,
  };
}

/**
 * The conversation's place for a run that raised (FR-92): `error.raised`'s own `class` and
 * `message`, verbatim, in place of a reply the loop journal never wrote. There is no `Refetch` - a
 * raise is not read again, it is sent again.
 */
function RaisedState({ payload }: { payload: Wire }) {
  const { status, error, message } = raisedOf(payload);
  return <ErrorState status={status} error={error} message={message} answeredBy={CONTROL_APP} />;
}

/**
 * One watch of the session's own stream, opened at `cursor` (the previous message's served
 * `last_seq`, or none for a session's first message) and not before `Conversation` has reported it
 * (decision 17): the gateway closes the stream at this message's `run.finished`. A drop is
 * `lib/stream.ts`'s backoff and the last seen cursor again; a clean close or a refusal is the end
 * of the watch. `onFrame` sees every frame this watch reads, verbatim.
 */
function useRunWatch(
  session: string | null,
  watching: object | undefined,
  cursor: { seq: string | undefined } | undefined,
  atRest: () => void,
  onFrame: (frame: StreamFrame) => void,
): void {
  const last = useRef<string | undefined>(undefined);
  const waited = useRef<number | undefined>(undefined);
  const [attempt, setAttempt] = useState<object>({});

  useEffect(() => {
    last.current = cursor?.seq;
    waited.current = undefined;
  }, [session, watching, cursor]);

  useEffect(() => {
    if (session === null || watching === undefined || cursor === undefined) return undefined;
    const controller = new AbortController();
    let live = true;
    let retry: ReturnType<typeof setTimeout> | undefined;

    void readStream(
      streamRoute(session),
      last.current,
      (signal) => {
        if (!live) return;
        if (signal.kind === "frames") {
          for (const frame of signal.frames) {
            last.current = frame.seq.text;
            onFrame(frame);
          }
          waited.current = undefined;
          return;
        }
        if (signal.kind === "dropped") {
          const wait = nextBackoffMs(waited.current);
          waited.current = wait;
          retry = setTimeout(() => {
            if (live) setAttempt({});
          }, wait);
          return;
        }
        atRest();
      },
      controller.signal,
    );

    return () => {
      live = false;
      controller.abort();
      if (retry !== undefined) clearTimeout(retry);
    };
  }, [session, watching, cursor, attempt, atRest, onFrame]);
}
