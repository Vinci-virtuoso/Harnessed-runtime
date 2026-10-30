import { minutesAgo, now } from "../../lib/clock";
import type { StreamFrame } from "../../lib/stream";
import { type StepKind, traceStepFor } from "../../lib/trace-steps";
import { STEP_KIND_LABEL } from "../StepIcon";

/** The status line before any frame has ever arrived. */
const IDLE = "Idle";

/** A payload field a frame may or may not carry, read as text without the wire's throw. */
export function payloadField(frame: StreamFrame, path: string): string | undefined {
  return frame.paths.includes(path) ? frame.payload.at(path).text : undefined;
}

/** The status line's text: "Idle" before any frame, else the newest frame's word and how long ago
 * the browser received it, on the dashboard's own clock (D-5, FR-124). */
export function statusText(lastFrame: StreamFrame | undefined, lastAt: Date | undefined): string {
  if (lastFrame === undefined || lastAt === undefined) return IDLE;
  return `Last step: ${stepWord(lastFrame)}, ${minutesAgo(lastAt, now())}`;
}

/** A frame's own trace-step word, capitalized; a name with no trace-step row reads verbatim. */
function stepWord(frame: StreamFrame): string {
  const kind: StepKind | undefined = traceStepFor(frame.name.text)?.kind(frame.payload);
  if (kind === undefined) return frame.name.text;
  const label = STEP_KIND_LABEL[kind];
  return `${label.slice(0, 1).toUpperCase()}${label.slice(1)}`;
}
