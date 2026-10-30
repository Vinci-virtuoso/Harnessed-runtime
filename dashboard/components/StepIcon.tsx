/**
 * The nine trace-step glyphs (design.md Direction rule 8; decision 68, G-36 refused).
 *
 * `@phosphor-icons/react` is a 33 MB dependency for nine glyphs and was refused (`ux/architecture.md`
 * decision 68); this file draws the nine as inline SVG instead of adding it. **They are original,
 * simplified line glyphs drawn for this component, not Phosphor's own geometry** - this sandbox's
 * outbound network policy does not reach the registry Phosphor's assets are published to, so the
 * geometry could not be copied faithfully, and drawing an approximation while citing it as
 * Phosphor's own would misstate its source. A human who can reach that registry may replace these
 * paths with Phosphor's bold-weight geometry for the same nine names (`ArrowsSplit`, `Database`,
 * `ChatCircleText`, `Robot`, `Wrench`, `CheckCircle`, `Question`, `Warning`, `XCircle`) without
 * changing this file's props or the 24px bordered container `TraceStep` puts around it.
 *
 * Every glyph is `aria-hidden`: the word beside it (design.md's status column, and each
 * `TraceStep`'s own sentence) is what a screen reader hears, never the shape alone.
 */
import type { StepKind } from "../lib/trace-steps";

import styles from "./StepIcon.module.css";

type Props = {
  kind: StepKind;
};

/** One 24x24 stroke path per kind, in the design's Direction rule 8 order. */
const PATHS: Readonly<Record<StepKind, string>> = {
  // router (ArrowsSplit): one line forking into two.
  router: "M4 12h6m0 0-3-3m3 3-3 3m4-3h6l4-5m-4 5 4 5",
  // memory (Database): a stacked cylinder.
  memory: "M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3Zm0 0v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3",
  // chat_model (ChatCircleText): a speech bubble with three lines.
  chat_model: "M4 12c0-4.4 3.6-8 8-8s8 3.6 8 8-3.6 8-8 8c-1.1 0-2.1-.2-3-.6L5 20l1-3.4A7.9 7.9 0 0 1 4 12Zm4-1h8m-8 4h5",
  // agent (Robot): a rounded head with two eyes and an antenna.
  agent: "M12 3v3M6 8h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2Zm2 5h.01M16 13h.01M9 17h6",
  // agent_tool (Wrench): a wrench head and shaft.
  agent_tool: "M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.7 2.7-2-2 2.7-2.7Z",
  // finished (CheckCircle): a circle with a check.
  finished: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm-4-9 3 3 5-5",
  // uncertain (Question): a circle with a question mark.
  uncertain: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-5v-.5c0-1 .6-1.5 1.3-2 .7-.5 1.2-1 1.2-2a2.5 2.5 0 0 0-5 0M12 17h.01",
  // fell_back (Warning): a triangle with an exclamation mark.
  fell_back: "M12 4 2 20h20L12 4Zm0 6v4m0 3h.01",
  // failed (XCircle): a circle with an X.
  failed: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM9.5 9.5l5 5m0-5-5 5",
};

/** The word each glyph stands for, used only as an offscreen label a caller may cite. */
export const STEP_KIND_LABEL: Readonly<Record<StepKind, string>> = {
  router: "router",
  memory: "memory",
  chat_model: "chat model",
  agent: "agent",
  agent_tool: "agent tool",
  finished: "finished",
  uncertain: "uncertain",
  fell_back: "fell back",
  failed: "failed",
};

export function StepIcon({ kind }: Props) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={styles.glyph}
      data-testid="step-icon"
      data-kind={kind}
    >
      <path d={PATHS[kind]} />
    </svg>
  );
}
