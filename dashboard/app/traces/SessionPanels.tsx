"use client";

/**
 * One session's trace, from `TraceTimeline` (FR-59, FR-129, UJ-11).
 *
 * The replay is a request and the stream is a subscription, so they are two panels and not one,
 * meeting at exactly one value - the last replayed `seq` - which is `TraceTimeline`'s own job to
 * know (decision 68); this page mounts it and nothing else.
 */
import { TraceTimeline } from "../../components/TraceTimeline";

type Props = {
  /** The id the URL carried, passed to both routes exactly as it arrived (MUST rule 25). */
  sessionId: string;
};

export function SessionPanels({ sessionId }: Props) {
  return <TraceTimeline sessionId={sessionId} />;
}
