"use client";

/**
 * The Memory page's architecture diagram: the memory layers and how they connect, drawn from
 * literals (FR-85, FR-128), in `HarnessDiagram`'s style and out of the same shared pieces
 * (`components/diagram/`). **Every coordinate is a literal**, as there: the no-derivation lint bans
 * `+`, `-` and `*`, so the numbers are written out.
 *
 * **Draw what exists.** Harnessed has no SOUL.md and treats skills as delegation targets, not
 * memory. The diagram draws the fixed base instruction where a SOUL.md would sit (no figure, no
 * link, captioned as such) and Skills as the installed `cli` agents the router delegates to,
 * captioned "not memory: delegated". `entries` is `/v1/agents`' own `.agent` `Leaf`s already
 * narrowed to `cli` (`useAgentEntries(..., "cli")`), so any other kind draws no slot.
 *
 * **How memory works, as drawn.** The router asks the gate; on retrieve, facts and episodes
 * assemble into the user turn's retrieved context under a token budget; active procedures go into
 * the system prompt, not through retrieval; every message is written back as an episode; an
 * improvement cycle's promotions add procedures.
 *
 * **Two layers, one `viewBox`**, as on the Overview: the picture (regions, captions, edges) is one
 * `<svg role="img">`, and the boxes sit in a second `<svg>` over it, each an `<a>` where it has a
 * page, since a link cannot live inside an `img`. The labels are the page's own panels' `Leaf`s
 * (decision 42), lent up with their responses and rendered through `Value`; a figure not handed
 * is not drawn.
 *
 * **Lighting.** `frames` is the watched stream's arrivals; `lib/memory-stages.ts` maps the name of
 * the first frame not yet shown to the boxes and edges it lights, and the CSS `motion-highlight`
 * animation is the whole of the timing (`useFrameBeat`). Under `prefers-reduced-motion` nothing
 * is lit; the status line still names each arrival.
 */
import { now } from "../lib/clock";
import type { RawResponse } from "../lib/fetch-panel";
import { humanAgo, pluralOf } from "../lib/humanize";
import { type MemoryStage, MEMORY_NOTHING, memoryStageFor } from "../lib/memory-stages";
import type { MemoryEdgeId, MemoryNodeId } from "../lib/memory-stages";
import type { StreamFrame } from "../lib/stream";
import type { Leaf } from "../lib/wire";

import { Box, type Lit } from "./diagram/Box";
import styles from "./diagram/diagram.module.css";
import { Labels } from "./diagram/Labels";
import { statusText } from "./diagram/status";
import { Value } from "./Value";
import { useFrameBeat } from "./diagram/useFrameBeat";

/** The picture's accessible name: what the diagram is, in one sentence. */
export const MEMORY_DIAGRAM_NAME =
  "Memory architecture: the router, the retrieval gate, semantic and episodic memory into retrieved context, procedural memory and the base instruction into the system prompt, the model, and improvement";

/** The labels each box draws, by key, in reading order (decision 42). */
const GATE_LABELS = ["skip", "retrieve", "failed open"] as const;
const SEMANTIC_LABELS = ["facts active", "facts archived"] as const;
const EPISODIC_LABELS = ["episodes"] as const;
const PROCEDURAL_LABELS = ["procedures active"] as const;

/** The six literal skill slots, in served order. */
const SLOTS = 6;

type Props = {
  /** The page panels' `Leaf`s for the box labels, by key (decision 42). */
  labels: Record<string, Leaf>;
  /** The response each label's route answered with, so its chip opens the panel's drawer. */
  sources?: Record<string, RawResponse>;
  /** The `cli` agents' own `.agent` `Leaf`s, in served order. */
  entries?: readonly Leaf[];
  /** The watched stream's frames as they arrived, each lit in turn (FR-85). */
  frames?: readonly StreamFrame[];
};

const NO_FRAMES: readonly StreamFrame[] = [];
const NO_SOURCES: Record<string, RawResponse> = {};
const NO_ENTRIES: readonly Leaf[] = [];

export function MemoryDiagram({
  labels,
  sources = NO_SOURCES,
  entries = NO_ENTRIES,
  frames = NO_FRAMES,
}: Props) {
  const { current, flash, ended, lastFrame, lastAt } = useFrameBeat(frames);
  const flashing: MemoryStage = current === undefined ? MEMORY_NOTHING : memoryStageFor(current.name.text);

  const node = (id: MemoryNodeId): Lit => ({
    treatment: flashing.warn && flashing.nodes.includes(id) ? "warn" : undefined,
    flash: flashing.nodes.includes(id) ? flash : undefined,
  });
  const edge = (id: MemoryEdgeId, dashed = false) => ({
    "data-edge": id,
    className: [styles.edge, dashed ? styles.dashed : "", flashing.edges.includes(id) ? flash : ""].join(" "),
  });
  const warned = node("gate").treatment === "warn";

  return (
    <div className={styles.frame}>
      <div className={styles.diagram}>
        <svg className={styles.picture} viewBox="0 0 1392 544" role="img" aria-label={MEMORY_DIAGRAM_NAME}>
          <defs>
            <marker
              id="memory-arrow"
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
            <rect x="16" y="16" width="152" height="496" rx="6" />
            <text x="28" y="40">Ask</text>
          </g>
          <g className={styles.region}>
            <rect x="192" y="16" width="184" height="496" rx="6" />
            <text x="204" y="40">Router and skills</text>
          </g>
          <g className={styles.region}>
            <rect x="400" y="16" width="420" height="496" rx="6" />
            <text x="412" y="40">Retrieval</text>
          </g>
          <g className={styles.region}>
            <rect x="844" y="16" width="244" height="496" rx="6" />
            <text x="856" y="40">Assembly</text>
          </g>
          <g className={styles.region}>
            <rect x="1112" y="16" width="264" height="496" rx="6" />
            <text x="1124" y="40">Prompt and model</text>
          </g>

          <text x="208" y="158" className={styles.more}>not memory: delegated</text>
          <text x="860" y="312" className={styles.more}>Fixed base instruction, no figure:</text>
          <text x="860" y="328" className={styles.more}>the place a SOUL.md would sit.</text>

          <path d="M152 96 H208" markerEnd="url(#memory-arrow)" {...edge("task-router")} />
          <path d="M352 120 V176" markerEnd="url(#memory-arrow)" {...edge("router-skills", true)} />
          <path d="M360 96 H384 V112 H408" markerEnd="url(#memory-arrow)" {...edge("router-gate")} />
          <path d="M464 152 V232 H600" markerEnd="url(#memory-arrow)" {...edge("gate-semantic")} />
          <path d="M464 152 V384 H600" markerEnd="url(#memory-arrow)" {...edge("gate-episodic")} />
          <path d="M792 232 H828 V376 H860" markerEnd="url(#memory-arrow)" {...edge("semantic-retrieved")} />
          <path d="M792 384 H828 V376 H860" markerEnd="url(#memory-arrow)" {...edge("episodic-retrieved")} />
          <path d="M1072 376 H1136" markerEnd="url(#memory-arrow)" {...edge("retrieved-model")} />
          <path d="M1072 176 H1136" markerEnd="url(#memory-arrow)" {...edge("procedural-system")} />
          <path d="M1072 268 H1136" markerEnd="url(#memory-arrow)" {...edge("identity-system")} />
          <path d="M1236 292 V352" markerEnd="url(#memory-arrow)" {...edge("system-model")} />
          <path d="M1336 376 H1360 V524 H780 V408" markerEnd="url(#memory-arrow)" {...edge("model-episodic")} />
          <path d="M600 384 H584 V232 H600" markerEnd="url(#memory-arrow)" {...edge("episodic-semantic")} />
          <text x="580" y="312" textAnchor="end" className={styles.more}>consolidates</text>
          <path d="M966 120 V152" markerEnd="url(#memory-arrow)" {...edge("improve-procedural")} />
        </svg>

        <svg className={styles.boxes} viewBox="0 0 1392 544">
          <g role="group" aria-label="Ask">
            <Box id="task" label="Task" href={null} lit={node("task")}>
              <rect x="32" y="72" width="120" height="48" rx="6" />
              <text x="92" y="100">Task</text>
            </Box>
          </g>

          <g role="group" aria-label="Router and skills">
            <Box id="router" label="Router (Jev)" href="/models#router" lit={node("router")}>
              <rect x="208" y="72" width="152" height="48" rx="6" />
              <text x="284" y="100">Router (Jev)</text>
            </Box>
            {entries[0] === undefined ? null : (
              <Box id="skill0" entry={entries[0]} href="/models#agents" lit={node("skill0")}>
                <rect x="208" y="184" width="152" height="44" rx="6" />
                <text x="284" y="210">{entries[0].text}</text>
              </Box>
            )}
            {entries[1] === undefined ? null : (
              <Box id="skill1" entry={entries[1]} href="/models#agents" lit={node("skill1")}>
                <rect x="208" y="236" width="152" height="44" rx="6" />
                <text x="284" y="262">{entries[1].text}</text>
              </Box>
            )}
            {entries[2] === undefined ? null : (
              <Box id="skill2" entry={entries[2]} href="/models#agents" lit={node("skill2")}>
                <rect x="208" y="288" width="152" height="44" rx="6" />
                <text x="284" y="314">{entries[2].text}</text>
              </Box>
            )}
            {entries[3] === undefined ? null : (
              <Box id="skill3" entry={entries[3]} href="/models#agents" lit={node("skill3")}>
                <rect x="208" y="340" width="152" height="44" rx="6" />
                <text x="284" y="366">{entries[3].text}</text>
              </Box>
            )}
            {entries[4] === undefined ? null : (
              <Box id="skill4" entry={entries[4]} href="/models#agents" lit={node("skill4")}>
                <rect x="208" y="392" width="152" height="44" rx="6" />
                <text x="284" y="418">{entries[4].text}</text>
              </Box>
            )}
            {entries[5] === undefined ? null : (
              <Box id="skill5" entry={entries[5]} href="/models#agents" lit={node("skill5")}>
                <rect x="208" y="444" width="152" height="44" rx="6" />
                <text x="284" y="470">{entries[5].text}</text>
              </Box>
            )}
            {entries.length > SLOTS ? (
              <text x="208" y="508" className={styles.more}>more on the Models page</text>
            ) : null}
          </g>

          <g role="group" aria-label="Retrieval">
            <Box id="gate" label="Retrieval gate" href={null} lit={node("gate")}>
              <polygon points="464,72 520,112 464,152 408,112" />
              <text x="464" y="108">Retrieval</text>
              <text x="464" y="124">gate</text>
            </Box>
            <foreignObject x="536" y="64" width="264" height="96">
              <Labels keys={GATE_LABELS} labels={labels} sources={sources} />
            </foreignObject>
            {warned && flashing.word !== undefined ? (
              <text x="476" y="184" className={styles.warnWord}>
                {flashing.word}
              </text>
            ) : null}
            <Box id="semantic" label="Semantic" href="#facts" lit={node("semantic")}>
              <rect x="600" y="208" width="192" height="48" rx="6" />
              <text x="696" y="236">Semantic</text>
            </Box>
            <foreignObject x="600" y="264" width="212" height="72">
              <Labels keys={SEMANTIC_LABELS} labels={labels} sources={sources} />
            </foreignObject>
            <Box id="episodic" label="Episodic" href="#episodes" lit={node("episodic")}>
              <rect x="600" y="360" width="192" height="48" rx="6" />
              <text x="696" y="388">Episodic</text>
            </Box>
            <foreignObject x="600" y="416" width="168" height="44">
              <Labels keys={EPISODIC_LABELS} labels={labels} sources={sources} />
            </foreignObject>
          </g>

          <g role="group" aria-label="Assembly">
            <Box id="improve" label="Improvement" href="/evals#improvement" lit={node("improve")}>
              <rect x="860" y="72" width="212" height="48" rx="6" />
              <text x="966" y="100">Improvement</text>
            </Box>
            <Box id="procedural" label="Procedural" href="#procedures" lit={node("procedural")}>
              <rect x="860" y="152" width="212" height="48" rx="6" />
              <text x="966" y="180">Procedural</text>
            </Box>
            <foreignObject x="852" y="204" width="228" height="32">
              <Labels keys={PROCEDURAL_LABELS} labels={labels} sources={sources} />
            </foreignObject>
            <Box id="identity" label="Identity: base instruction" href={null} lit={node("identity")}>
              <rect x="860" y="244" width="212" height="48" rx="6" />
              <text x="966" y="272">Identity: base instruction</text>
            </Box>
            <Box id="retrieved" label="Retrieved context" href={null} lit={node("retrieved")}>
              <rect x="860" y="352" width="212" height="48" rx="6" />
              <text x="966" y="380">Retrieved context</text>
            </Box>
          </g>

          <g role="group" aria-label="Prompt and model">
            <Box id="system" label="System prompt" href={null} lit={node("system")}>
              <rect x="1136" y="152" width="200" height="140" rx="6" />
              <text x="1236" y="226">System prompt</text>
            </Box>
            <Box id="model" label="Model" href={null} lit={node("model")}>
              <rect x="1136" y="352" width="200" height="48" rx="6" />
              <text x="1236" y="380">Model</text>
            </Box>
          </g>
        </svg>
      </div>

      <p className={styles.status} role="status" aria-live="polite">
        {statusText(lastFrame, lastAt)}
      </p>
      <p className={styles.status} data-testid="consolidation-status">
        <LastConsolidation labels={labels} />
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


/** The labels the Consolidation panel lends (decision 42): the newest batch's own leaves. */
const LAST_FACTS = "consolidation facts";
const LAST_EPISODES = "consolidation episodes";
const LAST_AT = "consolidation at";

/** "Last consolidation: 4 facts from 20 episodes, 2 h ago", or "No consolidation yet". */
function LastConsolidation({ labels }: { labels: Record<string, Leaf> }) {
  const facts = labels[LAST_FACTS];
  const episodes = labels[LAST_EPISODES];
  const at = labels[LAST_AT];
  if (facts === undefined || episodes === undefined || at === undefined) {
    return <>No consolidation yet</>;
  }
  return (
    <>
      Last consolidation: <Value leaf={facts} /> {pluralOf(facts, "fact")} from <Value leaf={episodes} />{" "}
      {pluralOf(episodes, "episode")}, <Value leaf={at} human={humanAgo(at, now())} />
    </>
  );
}
