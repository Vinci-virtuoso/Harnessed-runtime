"use client";

/**
 * A session's whole trace: the replay, then the live stream it hands off to (FR-59, FR-129,
 * decision 68). `SessionPanels` and `ConsolePanel` each mounted this pair inline before this
 * ticket; it is pulled out here once, so both read the same two panels the same way.
 *
 * **Deviation from `architecture.md`'s `TraceTimeline { sessionId, firstSeq, lastSeq }`** (recorded
 * in this ticket's "For the architecture node" note): `firstSeq`/`lastSeq` describe a scoped
 * window bounded by a served pair. The console's bound is `live: { afterSeq }` instead: the
 * previous message's served `last_seq` (none for a session's first message). The gateway closes a
 * stream at the first `run.finished` after its cursor, so a stream opened there is exactly this
 * message's own steps, and nothing is replayed. Without `live` (the `/sessions` page) it takes
 * `sessionId` alone and renders the whole session: replay, then stream.
 *
 * It still renders as **two** panels - `ReplayPanel`'s own card, then `StreamPanel`'s - rather
 * than the one bordered card `design.md` draws: merging them into a single `role="log"` list is
 * the harder, riskier half of that description and is left for a later pass, noted alongside the
 * prop deviation above. Every criterion this ticket's own Gherkin ACs state still holds either
 * way - each row is a `TraceStep`, the status word replaces the old cursor header, and 0 cursors
 * or `seq`s are drawn on either panel.
 */
import { useCallback, useState } from "react";

import type { Leaf } from "../lib/wire";
import { ReplayPanel, type Handoff } from "../app/traces/ReplayPanel";

import { StreamPanel } from "./StreamPanel";

type Props = {
  /** The id the URL carried, passed to both panels exactly as it arrived (MUST rule 25). */
  sessionId: string;
  /** Called when the replay's one read settles at `error` or `unreachable` (FR-92's known edge). */
  onFailed?: () => void;
  /**
   * Called with every handoff this timeline renders, `ConsolePanel`'s own watch of the stream
   * (FR-92's `error.raised` read) besides the one this component opens for its own `StreamPanel`.
   */
  onReplayed?: (handoff: Handoff) => void;
  /** The console's live exchange: only this message's own stream, opened after `afterSeq`. */
  live?: { afterSeq: Leaf | undefined };
};

export function TraceTimeline({ sessionId, onFailed, onReplayed, live }: Props) {
  const [handoff, setHandoff] = useState<Handoff | undefined>(undefined);
  const replayed = useCallback(
    (next: Handoff) => {
      setHandoff(next);
      onReplayed?.(next);
    },
    [onReplayed],
  );

  if (live !== undefined) return <StreamPanel sessionId={sessionId} afterSeq={live.afterSeq} />;

  return (
    <>
      <ReplayPanel sessionId={sessionId} onReplayed={replayed} onFailed={onFailed} />
      {handoff === undefined ? null : (
        <StreamPanel sessionId={sessionId} afterSeq={handoff.afterSeq} startEvent={handoff.startEvent} />
      )}
    </>
  );
}
