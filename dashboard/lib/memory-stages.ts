/**
 * The Memory diagram's stage table: which boxes and edges each event name lights (FR-85, FR-128).
 *
 * Only the names the memory path publishes have a row; a name with no row lights nothing and
 * the status line still names it. Unlike `lib/stages.ts` this table is not exhaustive over the
 * taxonomy, so its drift test (`tests/unit/memory-diagram.test.tsx`) checks the other direction:
 * every name listed here is frozen in `src/harnessed/events/names.py`. Keep each row on one line,
 * a quoted name first.
 *
 * `retrieval.gate.failed_open` is the one `warn` row: the gate in the amber treatment, with the
 * word beside it. `memory.write.committed` is the Episodic row (ticket 232, decision 73): `run_cmd`
 * publishes it with `{kind: "episode", episode_id}` when a run writes its episode, ahead of
 * `run.finished`, so the write-back edge lights when the episode lands. `run.finished` keeps its
 * row, because a message that wrote no episode still finishes.
 */

/** The Memory diagram's boxes. */
export type MemoryNodeId =
  | "task"
  | "router"
  | "skill0"
  | "skill1"
  | "skill2"
  | "skill3"
  | "skill4"
  | "skill5"
  | "gate"
  | "semantic"
  | "episodic"
  | "retrieved"
  | "procedural"
  | "identity"
  | "system"
  | "model"
  | "improve";

/** The Memory diagram's edges that a stage can light. */
export type MemoryEdgeId =
  | "task-router"
  | "router-skills"
  | "router-gate"
  | "gate-semantic"
  | "gate-episodic"
  | "semantic-retrieved"
  | "episodic-retrieved"
  | "retrieved-model"
  | "procedural-system"
  | "identity-system"
  | "system-model"
  | "model-episodic"
  | "improve-procedural"
  | "episodic-semantic";

/** What one frame lights: boxes, edges, whether the treatment is `warn`, and its word. */
export type MemoryStage = {
  readonly nodes: readonly MemoryNodeId[];
  readonly edges: readonly MemoryEdgeId[];
  readonly warn: boolean;
  /** The word beside a `warn` box. Present only where `warn` is `true`. */
  readonly word?: string;
};

/** A name with no row: nothing lit. */
export const MEMORY_NOTHING: MemoryStage = { nodes: [], edges: [], warn: false };

const DECIDED: MemoryStage = { nodes: ["gate"], edges: ["router-gate"], warn: false };
const FAILED_OPEN: MemoryStage = {
  nodes: ["gate"],
  edges: ["router-gate"],
  warn: true,
  word: "failed open",
};
const ASSEMBLED: MemoryStage = {
  nodes: ["semantic", "episodic", "retrieved"],
  edges: ["gate-semantic", "gate-episodic", "semantic-retrieved", "episodic-retrieved", "retrieved-model"],
  warn: false,
};
const TRUNCATED: MemoryStage = { nodes: ["retrieved"], edges: [], warn: false };
const WRITE_BACK: MemoryStage = { nodes: ["model", "episodic"], edges: ["model-episodic"], warn: false };

const CONSOLIDATED: MemoryStage = { nodes: ["episodic", "semantic"], edges: ["episodic-semantic"], warn: false };

export const MEMORY_STAGE: Readonly<Record<string, MemoryStage>> = {
  "retrieval.gate.decided": DECIDED,
  "retrieval.gate.failed_open": FAILED_OPEN,
  "retrieval.assembled": ASSEMBLED,
  "retrieval.budget.truncated": TRUNCATED,
  "memory.write.committed": WRITE_BACK,
  "run.finished": WRITE_BACK,
  "memory.consolidation.finished": CONSOLIDATED,
};

/** What a frame lights on the Memory diagram: its name's row, or nothing. */
export function memoryStageFor(name: string): MemoryStage {
  return Object.hasOwn(MEMORY_STAGE, name) ? MEMORY_STAGE[name] : MEMORY_NOTHING;
}
