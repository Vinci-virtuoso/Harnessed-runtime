"use client";

/**
 * The architecture diagram: the harness's six regions, drawn from literals (FR-84, FR-124, UJ-15).
 *
 * `ux/design.md` calls it `ArchDiagram`; the architecture's props table names it `HarnessDiagram`,
 * and that table is the binding contract, so this is `HarnessDiagram` with every layout and state
 * detail `design.md` gives. **Every coordinate is a literal.** The no-derivation lint bans `+`,
 * `-` and `*` here as everywhere, so no layout engine can exist and the numbers are written out.
 *
 * **Two layers, one `viewBox`.** The picture - regions, captions, edges - is one `<svg
 * role="img">` with an accessible name. The boxes sit in a second `<svg>` stacked over it at the
 * same `viewBox`, each an `<a>` to its v3 page, grouped by region. They cannot live inside the
 * `img`: an `img`'s children are presentational, so a link there would be unreachable to a screen
 * reader and axe reports it (`nested-interactive`). The CLI and You boxes are the two boxes with
 * no page of their own.
 *
 * **Lighting.** `stage` is an event name; its `lib/stages.ts` row marks the boxes it lights `hot`
 * (or the gate or the router `warn`, with the word beside it) and the edges `live`. The status
 * line names the stage in text, so no state here depends on colour or motion.
 *
 * **Labels (FR-86, decision 42).** `labels` are the Overview panels' own `Leaf`s - the turn count
 * on the loop, facts on memory, the gate's split - passed down whole and rendered through
 * `<Value>`, so each names its `route` and `path` and matches the wire. `sources` is the response
 * each came from, keyed by route: a label whose route has one is wrapped in `ResponseSource`, so
 * its `SourceChip` opens the same `ProvenanceDrawer` over the same response as the tile's does. A
 * label the diagram is not handed is not drawn: nothing here remembers a figure. The labels sit
 * in each region's band below the return edge, in the linked layer, since a chip is a button and
 * the picture's children are presentational.
 *
 * **The router and the delegation region (FR-124).** `entries` is `/v1/agents`' own `.agent`
 * `Leaf`s, in served order, handed down whole exactly as `labels` are (a `Leaf` may travel as a
 * prop, decision 42): the delegation region draws one box per entry, in six literal slots, its
 * own text read straight into the box's `<text>` children (never a component prop, so decision
 * 42's lint does not reach it - the same reading `ActivityList`'s row already uses). A seventh
 * entry and beyond draw nothing; the region instead names "more on the Models page". A
 * `loop.route.decided` naming an agent route, or any `acp.*` frame, lights the entry whose name
 * matches the frame's own `.route` or `.agent` field, resolved here because `lib/stages.ts` never
 * reads the registry.
 *
 * **Frames (FR-85).** `frames` is the watched stream's arrivals, in the order the stream delivered
 * them, which is `seq` order. The first frame not yet shown is the one lit: its boxes and edges
 * take a highlight class whose CSS animation (opacity and stroke, `motion-highlight`) is the whole
 * of the timing. A hidden beat, keyed by the frame's `seq`, runs the same duration, and its
 * `animationend` is what marks the frame shown and lights the next, so a frame that lights no
 * region still holds its turn and this file owns no timer of its own for the highlight. The class
 * alternates between two names frame to frame, because the same class on the same box would not
 * restart its animation. Under `prefers-reduced-motion: reduce` nothing is lit and no beat runs;
 * the status line still names each arrival, in plain words.
 *
 * **The status line (FR-124).** "Idle" before any frame has ever arrived, else "Last step: <word>,
 * N min ago" - the newest arrived frame's own trace-step word (`lib/trace-steps.ts`'s
 * `STEP_KIND_LABEL`, the same nine words a `TraceStep` icon carries) and how long ago the browser
 * received it, on the dashboard's own clock (`D-5`; `lib/clock.ts::minutesAgo`, never the
 * ledger's time). This replaces the v4 sentence decision 30a's batching produced
 * (`lib/stream.ts::announcementFor`, `components/useAnnouncement.ts`): those are unchanged and
 * still what `StreamPanel` says, but the diagram's own status line reads the newest frame
 * directly, on every arrival, which is what "the status line still updates as plain text on every
 * frame" already asked of v4's reduced-motion path.
 */
import {
  GATE_DECIDED,
  ROUTE_DECIDED,
  type EdgeId,
  type GateDecision,
  type NodeId,
  type RouteDecision,
  type Stage,
  NOTHING,
  stageFor,
} from "../lib/stages";
import type { RawResponse } from "../lib/fetch-panel";
import type { StreamFrame } from "../lib/stream";
import type { Leaf } from "../lib/wire";

import { Box, type Lit } from "./diagram/Box";
import styles from "./diagram/diagram.module.css";
import { Labels } from "./diagram/Labels";
import { payloadField, statusText } from "./diagram/status";
import { useFrameBeat } from "./diagram/useFrameBeat";

/** The picture's accessible name: what the diagram is, in one sentence. */
export const DIAGRAM_NAME =
  "Harness architecture: entry, the router, the loop, memory behind its gate, evals, the flywheel, and delegation";

/** The labels each region draws, by key, in reading order (decision 42). */
const LOOP_LABELS = ["turns"] as const;
const MEMORY_LABELS = ["facts active", "skip", "retrieve", "failed open"] as const;

/** The six literal delegation slots, in served order (FR-124). */
const ENTRY_SLOTS: readonly NodeId[] = ["entry0", "entry1", "entry2", "entry3", "entry4", "entry5"];

type Props = {
  /** The event name whose stage is lit, or `null` at rest. */
  stage: string | null;
  /** The Overview's `Leaf`s for the box labels, by key (decision 42). */
  labels: Record<string, Leaf>;
  /** The response each label's route answered with, so its chip opens the tile's drawer. */
  sources?: Record<string, RawResponse>;
  /** `/v1/agents`' own `.agent` `Leaf`s, in served order (FR-124). */
  entries?: readonly Leaf[];
  /** The watched stream's frames as they arrived, each lit in turn (FR-85). */
  frames?: readonly StreamFrame[];
};

/** No frames: the diagram before its stream has said anything. One array, so it is one value. */
const NO_FRAMES: readonly StreamFrame[] = [];

/** No sources: labels still name where they came from, and offer no chip that opens nothing. */
const NO_SOURCES: Record<string, RawResponse> = {};

/** No entries: the delegation region draws nothing until `/v1/agents` answers. */
const NO_ENTRIES: readonly Leaf[] = [];

export function HarnessDiagram({
  stage,
  labels,
  sources = NO_SOURCES,
  entries = NO_ENTRIES,
  frames = NO_FRAMES,
}: Props) {
  const { current, flash, ended, lastFrame, lastAt } = useFrameBeat(frames);
  const flashing: Stage =
    (current === undefined ? undefined : stageFor(current.name.text, gateDecisionOf(current), routeDecisionOf(current))) ??
    NOTHING;
  const currentEntry = current === undefined ? undefined : entrySlotFor(current, entries);

  const lit: Stage = (stage === null ? undefined : stageFor(stage)) ?? NOTHING;
  const node = (id: NodeId): Lit => ({
    treatment: treatmentOf(lit, id) ?? (flashing.warn ? treatmentOf(flashing, id) : undefined),
    flash: flashing.nodes.includes(id) || id === currentEntry ? flash : undefined,
  });
  const warned = node("gate").treatment === "warn";
  const gateWord = wordFor(lit, "gate") ?? (flashing.warn ? wordFor(flashing, "gate") : undefined);
  const routedWord = wordFor(lit, "router") ?? (flashing.warn ? wordFor(flashing, "router") : undefined);
  const edge = (id: EdgeId, dashed = false) => ({
    "data-edge": id,
    "data-lit": lit.edges.includes(id) ? "live" : undefined,
    className: [
      styles.edge,
      dashed ? styles.dashed : "",
      lit.edges.includes(id) ? styles.live : "",
      flashing.edges.includes(id) ? flash : "",
    ].join(" "),
  });

  const overflow = entries.length > ENTRY_SLOTS.length;

  return (
    <div className={styles.frame}>
      <div className={styles.diagram}>
        <svg className={styles.picture} viewBox="0 0 1392 432" role="img" aria-label={DIAGRAM_NAME}>
          <defs>
            <marker
              id="harness-arrow"
              viewBox="0 0 8 8"
              refX="8"
              refY="4"
              markerWidth="8"
              markerHeight="8"
              orient="auto-start-reverse"
            >
              <path d="M0 0 L8 4 L0 8 Z" className={styles.arrow} />
            </marker>
          </defs>

          <g className={styles.region}>
            <rect x="16" y="16" width="168" height="400" rx="6" />
            <text x="28" y="40">Entry</text>
          </g>
          <g className={styles.region}>
            <rect x="208" y="16" width="168" height="400" rx="6" />
            <text x="220" y="40">Router</text>
          </g>
          <g className={styles.region}>
            <rect x="400" y="16" width="200" height="400" rx="6" />
            <text x="412" y="40">Loop</text>
          </g>
          <g className={styles.region}>
            <rect x="624" y="16" width="312" height="400" rx="6" />
            <text x="636" y="40">Memory</text>
          </g>
          <g className={styles.region}>
            <rect x="960" y="16" width="192" height="400" rx="6" />
            <text x="972" y="40">Evals and the flywheel</text>
          </g>
          <g className={styles.region}>
            <rect x="1176" y="16" width="200" height="400" rx="6" />
            <text x="1188" y="40">Delegation</text>
          </g>

          <path d="M160 88 H416 M160 208 H416" markerEnd="url(#harness-arrow)" {...edge("entry-loop")} />
          <path d="M416 104 H160 M416 224 H160" markerEnd="url(#harness-arrow)" {...edge("loop-entry")} />
          <path d="M360 96 H416" markerEnd="url(#harness-arrow)" {...edge("router-loop")} />
          <path d="M292 120 V184" markerEnd="url(#harness-arrow)" {...edge("router-you")} />
          <path d="M360 96 V300 H1176" markerEnd="url(#harness-arrow)" {...edge("router-delegation")} />
          <path d="M500 120 V184" markerEnd="url(#harness-arrow)" {...edge("model-tools", true)} />
          <path d="M584 96 H724" markerEnd="url(#harness-arrow)" className={styles.edge} />
          <path d="M780 132 L684 200" markerEnd="url(#harness-arrow)" {...edge("gate-procedural")} />
          <path d="M780 132 V200" markerEnd="url(#harness-arrow)" {...edge("gate-semantic")} />
          <path d="M780 132 L876 200" markerEnd="url(#harness-arrow)" {...edge("gate-episodic")} />
          <path d="M1056 120 V184" markerEnd="url(#harness-arrow)" className={styles.edge} />
        </svg>

        <svg className={styles.boxes} viewBox="0 0 1392 432">
          <g role="group" aria-label="Entry">
            <Box id="console" label="console" href="/console" lit={node("console")}>
              <rect x="40" y="72" width="120" height="48" rx="6" />
              <text x="100" y="100">console</text>
            </Box>
            <Box id="cli" label="CLI" href={null} lit={node("cli")}>
              <rect x="40" y="184" width="120" height="48" rx="6" />
              <text x="100" y="212">CLI</text>
            </Box>
          </g>

          <g role="group" aria-label="Router">
            <Box id="router" label="Jev routes" href="/models#router" lit={node("router")}>
              <rect x="224" y="72" width="136" height="48" rx="6" />
              <text x="292" y="100">Jev routes</text>
            </Box>
            {routedWord ? (
              <text x="368" y="100" className={styles.warnWord}>
                {routedWord}
              </text>
            ) : null}
            <Box id="you" label="You" href={null} lit={node("you")}>
              <rect x="260" y="184" width="64" height="40" rx="6" />
              <text x="292" y="208">You</text>
            </Box>
          </g>

          <g role="group" aria-label="Loop">
            <Box id="model" label="model" href="/traces" lit={node("model")}>
              <rect x="416" y="72" width="168" height="48" rx="6" />
              <text x="500" y="100">model</text>
            </Box>
            <Box id="tools" label="tools" href="/traces" lit={node("tools")}>
              <rect x="416" y="184" width="168" height="48" rx="6" />
              <text x="500" y="212">tools</text>
            </Box>
            <foreignObject x="408" y="300" width="184" height="112">
              <Labels keys={LOOP_LABELS} labels={labels} sources={sources} />
            </foreignObject>
          </g>

          <g role="group" aria-label="Memory">
            {/* [GAP G-27]: a diamond is one link, and the gate's is procedures. */}
            <Box id="gate" label="gate" href="/memory#procedures" lit={node("gate")}>
              <polygon points="780,60 836,96 780,132 724,96" />
              <text x="780" y="100">gate</text>
            </Box>
            {warned ? (
              <text x="844" y="100" className={styles.warnWord}>
                {gateWord}
              </text>
            ) : null}
            <Box id="procedural" label="procedural" href="/memory#procedures" lit={node("procedural")}>
              <rect x="640" y="200" width="88" height="48" rx="6" />
              <text x="684" y="228">procedural</text>
            </Box>
            <Box id="semantic" label="semantic" href="/memory#facts" lit={node("semantic")}>
              <rect x="736" y="200" width="88" height="48" rx="6" />
              <text x="780" y="228">semantic</text>
            </Box>
            <Box id="episodic" label="episodic" href="/memory#episodes" lit={node("episodic")}>
              <rect x="832" y="200" width="88" height="48" rx="6" />
              <text x="876" y="228">episodic</text>
            </Box>
            <foreignObject x="632" y="300" width="296" height="112">
              <Labels keys={MEMORY_LABELS} labels={labels} sources={sources} />
            </foreignObject>
          </g>

          <g role="group" aria-label="Evals and the flywheel">
            <Box id="eval" label="eval" href="/evals" lit={node("eval")}>
              <rect x="976" y="72" width="160" height="48" rx="6" />
              <text x="1056" y="100">eval</text>
            </Box>
            <Box id="improve" label="improve" href="/evals#improvement" lit={node("improve")}>
              <rect x="976" y="184" width="160" height="48" rx="6" />
              <text x="1056" y="212">improve</text>
            </Box>
          </g>

          <g role="group" aria-label="Delegation">
            {entries[0] === undefined ? null : (
              <Box id="entry0" entry={entries[0]} href="/models#router" lit={node("entry0")}>
                <rect x="1192" y="72" width="168" height="44" rx="6" />
                <text x="1204" y="98">{entries[0].text}</text>
              </Box>
            )}
            {entries[1] === undefined ? null : (
              <Box id="entry1" entry={entries[1]} href="/models#router" lit={node("entry1")}>
                <rect x="1192" y="128" width="168" height="44" rx="6" />
                <text x="1204" y="154">{entries[1].text}</text>
              </Box>
            )}
            {entries[2] === undefined ? null : (
              <Box id="entry2" entry={entries[2]} href="/models#router" lit={node("entry2")}>
                <rect x="1192" y="184" width="168" height="44" rx="6" />
                <text x="1204" y="210">{entries[2].text}</text>
              </Box>
            )}
            {entries[3] === undefined ? null : (
              <Box id="entry3" entry={entries[3]} href="/models#router" lit={node("entry3")}>
                <rect x="1192" y="240" width="168" height="44" rx="6" />
                <text x="1204" y="266">{entries[3].text}</text>
              </Box>
            )}
            {entries[4] === undefined ? null : (
              <Box id="entry4" entry={entries[4]} href="/models#router" lit={node("entry4")}>
                <rect x="1192" y="296" width="168" height="44" rx="6" />
                <text x="1204" y="322">{entries[4].text}</text>
              </Box>
            )}
            {entries[5] === undefined ? null : (
              <Box id="entry5" entry={entries[5]} href="/models#router" lit={node("entry5")}>
                <rect x="1192" y="352" width="168" height="44" rx="6" />
                <text x="1204" y="378">{entries[5].text}</text>
              </Box>
            )}
            {overflow ? (
              <text x="1192" y="408" className={styles.more}>more on the Models page</text>
            ) : null}
          </g>
        </svg>
      </div>

      <p className={styles.status} role="status" aria-live="polite">
        {statusText(lastFrame, lastAt)}
      </p>
      {current === undefined ? null : (
        <span
          key={current.seq.text}
          className={styles.beat}
          data-beat={current.seq.text}
          aria-hidden="true"
          onAnimationEnd={() => ended(current)}
        />
      )}
    </div>
  );
}

/** How a stage treats one box: `hot`, `warn`, or not at all. */
function treatmentOf(stage: Stage, id: NodeId): "hot" | "warn" | undefined {
  if (!stage.nodes.includes(id)) return undefined;
  return stage.warn ? "warn" : "hot";
}

/** A stage's own warn word for one box, present only where that box is the stage's `warn` one. */
function wordFor(stage: Stage, id: NodeId): string | undefined {
  return stage.warn && stage.nodes.includes(id) ? stage.word : undefined;
}

/**
 * The gate's decision as the frame's payload spells it, for the one v4 row a payload refines. A
 * payload that does not say is left to the name's own row rather than guessed.
 */
function gateDecisionOf(frame: StreamFrame): GateDecision | undefined {
  if (frame.name.text !== GATE_DECIDED) return undefined;
  if (payloadField(frame, ".failed_open") === "true") return "failed_open";
  const retrieve = payloadField(frame, ".retrieve");
  if (retrieve === undefined) return undefined;
  return retrieve === "true" ? "retrieve" : "skip";
}

/**
 * The router's decision as the frame's payload spells it, for v5's own row a payload refines
 * (FR-124). Keyed on `outcome` alone, except a `routed` outcome, which also reads `handler` to
 * tell a Groq route from an agent route.
 */
function routeDecisionOf(frame: StreamFrame): RouteDecision | undefined {
  if (frame.name.text !== ROUTE_DECIDED) return undefined;
  const outcome = payloadField(frame, ".outcome");
  if (outcome === "fallback") return "fallback";
  if (outcome === "uncertain") return "uncertain";
  if (outcome === "routed") return payloadField(frame, ".handler") === "agent" ? "routed_agent" : "routed_groq";
  return undefined;
}

/**
 * Which of the six delegation slots a frame names, from `loop.route.decided`'s `.route` or an
 * `acp.*` frame's `.agent`, matched against the entries the diagram was handed. `undefined` where
 * the frame names no entry, or names one beyond the six drawn slots.
 */
function entrySlotFor(frame: StreamFrame, entries: readonly Leaf[]): NodeId | undefined {
  const name =
    frame.name.text === ROUTE_DECIDED
      ? payloadField(frame, ".route")
      : frame.name.text.startsWith("acp.")
        ? payloadField(frame, ".agent")
        : undefined;
  if (name === undefined) return undefined;
  const index = entries.findIndex((entry) => entry.text === name);
  return index < 0 || index >= ENTRY_SLOTS.length ? undefined : ENTRY_SLOTS[index];
}
