"use client";

/**
 * The ledger half of a session page: every event already recorded, in the gateway's order
 * (FR-38, FR-59, UJ-11).
 *
 * One panel, one request to `/v1/sessions/{id}/events`, and a table of what came back. The rows
 * are the response's rows and their order is the response's order; nothing here sorts, filters or
 * counts (`D-1`, MUST rule 21). A session id the events table never held is this panel's 404,
 * quoted from the gateway - and a session that exists with no events is the *same* 404, because
 * `events/read.py` keys that table by the very rows it would be empty of. So there is no "this
 * session is empty" to render, and the zero-row guard below is the panel refusing to open a
 * stream at a cursor no response gave it.
 *
 * **Each row is a `TraceStep`, the same as a live one** (FR-129, decision 68). `lib/wire.ts` hands
 * out one leaf at a time and no container (decision 32), so this file reads a row's payload keys
 * structurally - `JSON.parse` of the response's own text, read for its *shape* only, the way
 * `RecordView.tsx` already reads a served record structurally rather than deriving one - and then
 * reads every value that shape names back out through `wire.at`, which is the one path that keeps
 * decision 31's exact characters. `scopedRow` below is what turns one `.events[i]` row into the
 * `StreamFrame` shape `TraceStep` already knows how to render, a live row's own shape with no `ts`
 * carried on the frame itself. No cursor or `seq` is drawn on this panel (FR-129 AC2); the replay
 * says what happened and when, the stream below says what is happening now.
 *
 * The last replayed `seq` is handed up as its own `Leaf`, so `StreamPanel` opens with
 * `Last-Event-ID` equal to those characters and the first live row is the next one: 0 rows shown
 * twice at the handoff. The `seq` 1 name goes with it, because a close is named by the pair that
 * starts at `seq` 1 and never by a finish's name alone (decision 17, MUST rule 20).
 *
 * `onFailed` is optional and separate from `onReplayed`: a caller that treats "the replay is not
 * open" as its own at-rest signal (`ConsolePanel`, FR-92's replay-after-a-202 edge, ticket 139's
 * known edge) is told when this read settles at `error` or `unreachable` and never gets a
 * handoff. A 404 is not in that set - decision 49 guarantees one never follows a 202 - so a
 * session the events table never held keeps its own `AbsentState` unchanged.
 */
import { useCallback, useEffect, type ReactNode } from "react";

import { sessionMessagesRoute } from "../../components/Conversation";
import { OutputView } from "../../components/OutputView";
import { PanelFrame, ResponseSource, usePanel } from "../../components/PanelFrame";
import { ReplyCaption } from "../../components/ReplyCaption";
import { TraceStep } from "../../components/TraceStep";
import { Value } from "../../components/Value";
import type { PanelResult } from "../../lib/fetch-panel";
import type { StreamFrame } from "../../lib/stream";
import type { Leaf, Wire } from "../../lib/wire";
import { leafIfSent, rowsOf } from "../../lib/rows";

import { eventsRoute } from "./limit";
import styles from "./sessions.module.css";

/** The panel's own name, exported so a test names the region rather than guessing at it. */
export const REPLAY_TITLE = "Replay";

/** The nested messages read's own name, so its state (a 404, an unreachable gateway) is labelled. */
export const TRANSCRIPT_TITLE = "Transcript";

/** Static copy for the two labels that bracket each turn (ticket 201): input, steps, response. */
const USER_INPUT = "user input:";
const RESPONSE = "response:";

/** What the replay hands the stream: the cursor to open at, and the name that will close it. */
export type Handoff = {
  /** The last replayed `seq`, as the leaf that carried it. Its text is the cursor, verbatim. */
  afterSeq: Leaf;
  /** The `seq` 1 name, which is the start half of decision 17's terminal pair. */
  startEvent: Leaf;
};

type Props = {
  /** The id the URL carried, passed to the gateway exactly as it arrived (MUST rule 25). */
  sessionId: string;
  /** Called with the handoff once a replay has rendered, and again after every refetch. */
  onReplayed: (handoff: Handoff) => void;
  /** Called when this read settles at `error` or `unreachable` instead of a handoff. */
  onFailed?: () => void;
};

export function ReplayPanel({ sessionId, onReplayed, onFailed }: Props) {
  const route = eventsRoute(sessionId);
  const onSettled = useCallback(
    (result: PanelResult) => {
      if (onFailed !== undefined && (result.state === "error" || result.state === "unreachable")) {
        onFailed();
      }
    },
    [onFailed],
  );

  return (
    <PanelFrame
      route={route}
      title={REPLAY_TITLE}
      sourceLabel="Ledger: this session"
      onSettled={onSettled}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.events[${index}].seq`);
        const last = rows.at(-1);
        // The gateway answers 404 rather than 200 with an empty list, so this is the response
        // that cannot arrive - and if it did, there would be no `seq` to stream from.
        if (last === undefined) return null;
        return (
          <div className={styles.body}>
            <Transcript sessionId={sessionId} eventsWire={wire} rows={rows} />
            <HandedOver
              afterSeq={wire.at(`.events[${last}].seq`)}
              startEvent={wire.at(".events[0].name")}
              onReplayed={onReplayed}
            />
          </div>
        );
      }}
    </PanelFrame>
  );
}

/**
 * The replay as a transcript: each served message's input, the events between its served
 * `first_seq` and the next message's, then its reply (ticket 201). Grouping is by string equality
 * on the served `first_seq` alone - no comparison, no arithmetic, no inference (decision 32); an
 * event before any match renders first, ungrouped, and every event renders exactly once.
 *
 * The events `PanelFrame` is captured here (the `Conversation` pattern) and re-opened around each
 * step, so a step's figure keeps the events response as its source while the transcript's own
 * figures cite the messages response this nested frame fetched.
 */
function Transcript({
  sessionId,
  eventsWire,
  rows,
}: {
  sessionId: string;
  eventsWire: Wire;
  rows: number[];
}) {
  const eventsPanel = usePanel();

  return (
    <PanelFrame
      route={sessionMessagesRoute(sessionId)}
      title={TRANSCRIPT_TITLE}
      sourceLabel="Ledger: this session"
      refresh={eventsPanel.response}
    >
      {(messagesWire) => {
        const messageRows = rowsOf(messagesWire, (row) => `.messages[${row}].message_seq`);
        const starts = new Map<string, number>();
        for (const row of messageRows) starts.set(messagesWire.at(`.messages[${row}].first_seq`).text, row);

        const items: ReactNode[] = [];
        let current: number | undefined;
        const close = () => {
          if (current !== undefined) {
            items.push(<ResponseRow key={`response-${current}`} messages={messagesWire} row={current} />);
          }
        };
        for (const row of rows) {
          const started = starts.get(eventsWire.at(`.events[${row}].seq`).text);
          if (started !== undefined) {
            close();
            current = started;
            items.push(<InputRow key={`input-${started}`} messages={messagesWire} row={started} />);
          }
          items.push(
            <ResponseSource
              key={`step-${row}`}
              response={eventsPanel.response}
              method="GET"
              sourceLabel={eventsPanel.sourceLabel}
            >
              <TraceStep frame={frameOf(eventsWire, row)} ts={eventsWire.at(`.events[${row}].ts`)} />
            </ResponseSource>,
          );
        }
        close();

        return (
          <div className={styles.log} role="log" aria-label="steps" data-testid="replay-log">
            <ul className={styles.list}>{items}</ul>
          </div>
        );
      }}
    </PanelFrame>
  );
}

/** The operator's own words, or the served reason there are none, in the muted absent style. */
function InputRow({ messages, row }: { messages: Wire; row: number }) {
  const text = messages.at(`.messages[${row}].text`);
  const reason = messages.at(`.messages[${row}].text_reason`);
  return (
    <li className={styles.turn} data-testid="transcript-input">
      <span className={styles.label}>{USER_INPUT}</span>
      {text.kind === "null" ? (
        <span className={styles.absent}>{reason.kind === "null" ? null : <Value leaf={reason} />}</span>
      ) : (
        <Value leaf={text} />
      )}
    </li>
  );
}

/** The served reply through `OutputView`, or the served `reply_reason` where there is none. */
function ResponseRow({ messages, row }: { messages: Wire; row: number }) {
  const prefix = `.messages[${row}]`;
  const message = scopedWire(messages, prefix);
  const kind = leafIfSent(message, ".reply.output_kind");
  const text = leafIfSent(message, ".reply.text");
  const outputId = leafIfSent(message, ".reply.output_id");
  const reason = message.at(".reply_reason");
  return (
    <li className={styles.turn} data-testid="transcript-response">
      <span className={styles.label}>{RESPONSE}</span>
      {kind === undefined ? (
        <span className={styles.absent}>{reason.kind === "null" ? null : <Value leaf={reason} />}</span>
      ) : (
        <>
          <OutputView
            kind={kind}
            output={text === undefined || text.kind === "null" ? null : text}
            outputId={outputId === undefined || outputId.kind === "null" ? undefined : outputId}
          />
          <ReplyCaption message={message} />
        </>
      )}
    </li>
  );
}

/**
 * Every leaf path a plain object or array holds, read structurally and never for a value: the
 * same reading `RecordView.tsx` already does of a served record, applied here to find *where*
 * `wire.at` should be asked, never to answer what is there (decision 31 stays with `wire.at`).
 */
function leafPaths(node: unknown, prefix: string, into: string[]): void {
  if (Array.isArray(node)) {
    for (const [index, item] of node.entries()) leafPaths(item, `${prefix}[${index}]`, into);
    return;
  }
  if (node !== null && typeof node === "object") {
    for (const [key, item] of Object.entries(node as Record<string, unknown>)) {
      leafPaths(item, `${prefix}.${key}`, into);
    }
    return;
  }
  into.push(prefix);
}

/** A `Wire` scoped to one jq prefix: `.at(path)` asks the real wire at `prefix + path`. */
function scopedWire(wire: Wire, prefix: string): Wire {
  return {
    route: wire.route,
    text: wire.text,
    byteLength: wire.byteLength,
    at: (path: string) => wire.at(`${prefix}${path}`),
  };
}

/** One replayed row, shaped the way `lib/stream.ts::frameOf` shapes a live one, so `TraceStep`
 * renders either the same way. No `ts` rides on the frame itself - a replayed row is handed its
 * own `ts` leaf beside the frame, exactly as a live row is handed none. */
function frameOf(wire: Wire, index: number): StreamFrame {
  const prefix = `.events[${index}].payload`;
  const paths: string[] = [];
  const payload = (JSON.parse(wire.text) as { events: { payload: unknown }[] }).events[index]?.payload;
  leafPaths(payload, "", paths);
  return {
    seq: wire.at(`.events[${index}].seq`),
    name: wire.at(`.events[${index}].name`),
    payload: scopedWire(wire, prefix),
    paths,
    data: "",
  };
}

/**
 * The handoff, said out loud and passed up. Never rendered (FR-129 AC2: 0 cursors or `seq`s are
 * drawn) - it is a component rather than a call in the render body because handing the cursor to
 * a sibling panel is an effect: the replay renders, and only then does a subscription open at the
 * `seq` it ended on. A refetch replaces the response, so it replaces the handoff too, and the
 * stream below reopens at whatever the new replay ended on - never at a remembered cursor (`D-6`).
 */
function HandedOver({ afterSeq, startEvent, onReplayed }: Handoff & Pick<Props, "onReplayed">) {
  useEffect(() => {
    onReplayed({ afterSeq, startEvent });
  }, [afterSeq, startEvent, onReplayed]);

  return null;
}
