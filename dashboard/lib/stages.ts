/**
 * The `STAGE` table: which diagram boxes and edges each event name lights (FR-84, FR-124, UJ-15).
 *
 * **Every** name in `src/harnessed/events/names.py::NAMES` has a row here, including the names
 * that light nothing, which map to `NOTHING` explicitly rather than being left out. The taxonomy
 * is frozen and this file only reads it (MUST rule 4): `tests/test_dashboard_stages.py` fails the
 * build when a frozen name has no row or a row names an unfrozen one, so adding an event name
 * costs a diagram decision. Keep each row on one line, a quoted name first: that is the shape the
 * drift test reads.
 *
 * The mapping follows `ux/design.md`'s stage table. Two rows light more than the table can say
 * from a name alone, and light every candidate rather than guess: `run.started` and `run.finished`
 * light both front doors (which one opened the run is not in the name), and the retrieval and
 * memory-write rows light all three pillars. `retrieval.gate.decided` and `loop.route.decided` are
 * the two rows a payload refines, through `GATE_DECISION` and `ROUTE_DECISION`: `retrieve` lights
 * the gate and the three pillars, `skip` the gate alone, and a failed-open decision the gate in the
 * `warn` treatment; a routed Groq or agent verdict lights the router toward the loop or the
 * delegation region, and an uncertain or fallback verdict lights it `warn` with a word beside it.
 *
 * `loop.route.decided`'s and every `acp.*` row's `nodes` never name a specific registry entry: the
 * six delegation boxes are drawn from `/v1/agents`, a list this file never reads (FR-124), so
 * `components/HarnessDiagram.tsx` resolves which entry's box lights, from the frame's own `.route`
 * or `.agent` field matched against the entries it was handed, layered on top of this table's rows.
 */

/** The diagram's boxes, as `components/HarnessDiagram.tsx` draws them. */
export type NodeId =
  | "console"
  | "cli"
  | "router"
  | "you"
  | "model"
  | "tools"
  | "gate"
  | "procedural"
  | "semantic"
  | "episodic"
  | "eval"
  | "improve"
  | "entry0"
  | "entry1"
  | "entry2"
  | "entry3"
  | "entry4"
  | "entry5";

/** The diagram's edges that a stage can light. */
export type EdgeId =
  | "entry-loop"
  | "loop-entry"
  | "router-loop"
  | "router-delegation"
  | "router-you"
  | "model-tools"
  | "gate-procedural"
  | "gate-semantic"
  | "gate-episodic";

/** What one frame lights: boxes, edges, whether the treatment is `warn`, and its word. */
export type Stage = {
  readonly nodes: readonly NodeId[];
  readonly edges: readonly EdgeId[];
  readonly warn: boolean;
  /** The word beside a `warn` box (design.md's "failed open", "uncertain", "fell back"). Present
   * only where `warn` is `true`. */
  readonly word?: string;
};

/** A row for a name that lights no region. The status line still names it in text. */
export const NOTHING: Stage = { nodes: [], edges: [], warn: false };

const ENTRY_IN: Stage = { nodes: ["console", "cli"], edges: ["entry-loop"], warn: false };
const ENTRY_OUT: Stage = { nodes: ["console", "cli"], edges: ["loop-entry"], warn: false };
const ITERATION: Stage = { nodes: ["model"], edges: ["model-tools"], warn: false };
const MODEL: Stage = { nodes: ["model"], edges: [], warn: false };
const TOOLS: Stage = { nodes: ["tools"], edges: ["model-tools"], warn: false };
const GATE: Stage = { nodes: ["gate"], edges: [], warn: false };
const GATE_WARN: Stage = { nodes: ["gate"], edges: [], warn: true, word: "failed open" };
const PILLARS: Stage = { nodes: ["procedural", "semantic", "episodic"], edges: [], warn: false };
const RETRIEVE: Stage = {
  nodes: ["gate", "procedural", "semantic", "episodic"],
  edges: ["gate-procedural", "gate-semantic", "gate-episodic"],
  warn: false,
};
const EVALS: Stage = { nodes: ["eval"], edges: [], warn: false };
const FLYWHEEL: Stage = { nodes: ["improve"], edges: [], warn: false };
/** `acp.*`: the router already sent this to the delegation region; which entry's box also lights
 * is resolved outside this table (see the module docstring). */
const DELEGATION: Stage = { nodes: [], edges: ["router-delegation"], warn: false };

/** `loop.route.decided` with no payload to refine it: the router lit, no edge decided yet. */
const ROUTER: Stage = { nodes: ["router"], edges: [], warn: false };
const ROUTE_GROQ: Stage = { nodes: ["router"], edges: ["router-loop"], warn: false };
const ROUTE_AGENT: Stage = { nodes: ["router"], edges: ["router-delegation"], warn: false };
const ROUTE_UNCERTAIN: Stage = {
  nodes: ["router"],
  edges: ["router-you"],
  warn: true,
  word: "uncertain",
};
const ROUTE_FALLBACK: Stage = {
  nodes: ["router"],
  edges: ["router-loop"],
  warn: true,
  word: "fell back",
};

/** The gate's decision event, the one v4 row a payload refines. */
export const GATE_DECIDED = "retrieval.gate.decided";

/** The three ways the gate decides, as `/v1/events/gate` buckets them. */
export type GateDecision = "retrieve" | "skip" | "failed_open";

/** What `retrieval.gate.decided` lights, by decision. */
export const GATE_DECISION: Readonly<Record<GateDecision, Stage>> = {
  retrieve: RETRIEVE,
  skip: GATE,
  failed_open: GATE_WARN,
};

/** The router's decision event, v5's own row a payload refines (FR-124). */
export const ROUTE_DECIDED = "loop.route.decided";

/** The four ways a route can be decided, as `ux/design.md`'s v5 stage table gives them. */
export type RouteDecision = "routed_groq" | "routed_agent" | "uncertain" | "fallback";

/** What `loop.route.decided` lights, by decision. */
export const ROUTE_DECISION: Readonly<Record<RouteDecision, Stage>> = {
  routed_groq: ROUTE_GROQ,
  routed_agent: ROUTE_AGENT,
  uncertain: ROUTE_UNCERTAIN,
  fallback: ROUTE_FALLBACK,
};

/** Every frozen event name, and what it lights. One row per name, one line per row. */
export const STAGE: Readonly<Record<string, Stage>> = {
  "run.started": ENTRY_IN,
  "run.finished": ENTRY_OUT,
  "loop.iteration.started": ITERATION,
  "loop.iteration.finished": MODEL,
  "loop.guardrail.tripped": NOTHING,
  "loop.route.decided": ROUTER,
  "provider.request.sent": MODEL,
  "provider.response.received": MODEL,
  "tool.call.started": TOOLS,
  "tool.call.finished": TOOLS,
  "tool.call.failed": TOOLS,
  "retrieval.gate.decided": GATE,
  "retrieval.gate.failed_open": GATE_WARN,
  "retrieval.assembled": PILLARS,
  "retrieval.budget.truncated": PILLARS,
  "memory.write.committed": PILLARS,
  "memory.consolidation.finished": NOTHING,
  "eval.run.started": EVALS,
  "eval.case.started": EVALS,
  "eval.case.scored": EVALS,
  "eval.run.finished": EVALS,
  "improve.run.started": FLYWHEEL,
  "improve.step.recorded": FLYWHEEL,
  "improve.reflection.written": FLYWHEEL,
  "improve.candidate.created": FLYWHEEL,
  "improve.promotion.decided": FLYWHEEL,
  "improve.run.finished": FLYWHEEL,
  "acp.delegation.started": DELEGATION,
  "acp.permission.request": DELEGATION,
  "acp.permission.decision": DELEGATION,
  "acp.delegation.finished": DELEGATION,
  "acp.delegation.cached": DELEGATION,
  "acp.tool.called": DELEGATION,
  "acp.tool.finished": DELEGATION,
  "task.started": NOTHING,
  "task.paused": NOTHING,
  "task.finished": NOTHING,
  "error.raised": NOTHING,
  "observer.failed": NOTHING,
};

/**
 * What a frame lights: its name's row, refined by the gate's decision when the name is
 * `retrieval.gate.decided`, or by the router's decision when the name is `loop.route.decided`.
 * `undefined` for a name the table does not hold.
 */
export function stageFor(name: string, gate?: GateDecision, route?: RouteDecision): Stage | undefined {
  if (name === GATE_DECIDED && gate !== undefined) return GATE_DECISION[gate];
  if (name === ROUTE_DECIDED && route !== undefined) return ROUTE_DECISION[route];
  return Object.hasOwn(STAGE, name) ? STAGE[name] : undefined;
}
