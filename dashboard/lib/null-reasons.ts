/**
 * The sentences the dashboard says where the gateway said nothing (`D-2`, `D-3`).
 *
 * Static copy, keyed on route and jq path, taken verbatim from `ux/design.md` ("State ownership":
 * the null-reason sentences are the one piece of copy the client owns). Nothing here is computed,
 * nothing here is a value: a figure is always a `Leaf` of the response its panel fetched, and this
 * file only explains an absence the gateway itself sent.
 *
 * `D-2` is the null half. `D-3` is the other half of the same rule: a 404 and a 200 with zero rows
 * are different facts, so `/memory/episodes/{session_id}` has two sentences and not one (decision
 * 24). They live together because a panel picks exactly one of them for exactly one state.
 */

import type { Leaf } from "./wire";

/** One documented absence: the route that sent it, the path it sits at, and why it is unknown. */
export type NullReason = {
  /** A route template; `{id}` matches one path segment. Any query string is ignored. */
  route: string;
  /** A jq path with array indices written `[]`, matched against the leaf's own path. */
  path: string;
  /** The sentence, verbatim from `ux/design.md`. */
  reason: string;
};

const NO_DECISION = "no decision reached";
const NO_EVIDENCE = "no evidence: 0 cases";
const NEVER_RUN = "never run";
/* The three reasons below were build jargon (G-57, `V6-10`): the screen says them in the operator's
 * terms and the provenance drawer keeps the exact reason (`EXACT_REASONS`, `exactReasonFor`). */
const OLD_TURN =
  "recorded before Harnessed measured this, so a partial sum would understate the run";
const OLD_BUILD = "recorded before Harnessed measured this";
const NO_RETRIEVAL = "the gateway does not pass retrieval to the trace";
const SPAN_RECORDED =
  "the run's span is recorded, so no turn was excluded from its duration";
/** An agent that does not report its model (`agent_turns.model` null for Claude Code, ticket 232). */
const NO_AGENT_MODEL = "Claude Code does not report its model";
const BEFORE_010 = "recorded before Harnessed recorded the provider and model";
/** Also what zero stored prices mean on the Models page's prices panel (ticket 147). */
export const NO_PRICES = "no prices fetched: run `harnessed prices refresh`";
const NONE_PRICED = "no turn could be priced";
/** The v5 spend buckets (FR-115): each is null when no row reported a figure, never a zero. */
const NO_ROUTER_COST = "no decision reported a cost";
const NO_AGENT_COST = "no OpenRouter agent reported a cost";
const NO_API_EQUIVALENT =
  "no subscription agent reported an API-equivalent cost";
const NO_TIMESTAMPS = "no turn carries both timestamps";
const NEEDS_NO_KEY = "needs no key";
const NO_DEFAULT_MODEL =
  "no default is known for this provider: set `HARNESSED_MODEL`";
const NO_CACHE_RATE = "the catalog published no cache rate";
const NEVER_PROBED = "never probed: run `harnessed agents test`";
/** `/v1/router`'s own AC1 (ticket 186, FR-121): every probe field but `cost` is null only when
 * the live rung has never once run, never "the newest probe failed". */
const NEVER_PROBED_ROUTER = "never probed: run the live doctor rung";
/** `/v1/router`'s `cost` (ticket 186): null before any probe, and also null on a failed one
 * (`record_probe`'s own rule - a refusal reports no usage to round), so one sentence covers both. */
const NO_ROUTER_PROBE_COST = "no cost reported for this probe";
/** What zero declared agents mean on the Models page's agents panel (ticket 148, design.md). */
export const NO_AGENTS = "no agent CLIs configured: see `harnessed agents add`";
/** `design.md`'s `RegistryCard` row, verbatim: "missing `billing` ('not metered')". */
const NO_BILLING = "not metered";
/** `agents.toml`'s own comment on `produces`: "absent lets the served media type decide". */
const NO_PRODUCES = "no output kind declared: the served media type decides";
/** Decision 67's per-hop fields (FR-120): each null only where the hop did not reach that case. */
const NOT_A_FALLBACK = "the router reached a decision, not a fallback";
const NOT_BELOW_FLOOR = "confidence met the floor: the hop did not stop here";
const NO_OPTION_DESCRIPTION = "no description sent for this option";
/** Decision 64's `handlers[]` row (FR-120): a chat-model or router row carries no billing kind. */
const NOT_AN_AGENT_HANDLER =
  "not an agent entry: this handler carries no billing kind";
const NO_HANDLER_COST = "no cost reported for this handler";
const NO_HANDLER_API_EQUIVALENT =
  "no API-equivalent figure reported for this handler";
const HANDLER_NEEDS_NO_REASON = "a reported figure needs no explanation";
/** Decision 66's `title` (FR-128): the first message's task, null for a session that never
 * routed a message - the same "before the router" null every other v5 read carries. */
const NEVER_ROUTED = "this session never routed a message";
/** Decision 66's per-message fields (FR-128): each null only where nothing reached that case. */
const REPLY_IS_A_TURN =
  "the reply is a Groq turn, not a hop output, so it has no output id";
const TEXT_RECORDED = "text was recorded for this message; nothing to explain";
const REPLY_RECORDED =
  "a reply was recorded for this message; nothing to explain";
const OUTPUT_NAMES_NO_FILE =
  "position 0 is the delegation's own text result, naming no file";
/** A routed message whose decision reported no cost (a refused hop, a fallback). */
const NO_MESSAGE_COST = "no cost reported for this message";
const HOP_DID_NOT_FAIL = "this hop did not fail, so there is no reason";
const HOP_UNRECORDED = "recorded before Harnessed recorded a hop's result";
const NO_REPOSITORY_RECORD = "no repository record for this hop, and repository_reason says why";
/** An uncertain stop's compared figure (ticket 291, decision 101): null on a decision that acted. */
const ROUTER_ACTED = "the router acted";
/** `/v1/router`'s `routing_reason` (ticket 316): null only while routing is on. */
const ROUTING_IS_ON = "routing is on, so there is no reason";
const COMPARED_RECORDED = "a figure was compared, so there is no reason";
const REPOSITORY_RECORDED = "the repository was recorded, so there is no reason";
const NO_OUTPUT_CAPTION = "no caption declared for this output";
/** Decision 74's `permission_decisions` row (FR-130): each null only where nothing reached it. */
const PERMISSION_NO_DESCRIPTION = "the agent described nothing";
const PERMISSION_NO_CEILING =
  "the configuration answered at once, so no person was asked";
const PERMISSION_UNANSWERED = "nobody has answered yet";
const PERMISSION_NO_PERSON =
  "no person answered: nobody has yet, or the configuration, the ceiling or a shutdown did";
const PERMISSION_NO_EXCHANGE =
  "asked outside a routed message, or recorded before the exchange was kept";
const PERMISSION_NO_TASK = "the session is not a task's";
const PERMISSION_OLD = "recorded before Harnessed kept this";
const PERMISSION_NO_DETAIL = "the agent sent no details";
const PERMISSION_NO_WARNING = "no warning applied";
const PERMISSION_REASON_NEEDED = "a recorded value needs no explanation";

// A consolidation batch (ticket 249, FR-152, migration 023): each nullable column is null for its
// own reason, and its `_reason` sibling is null exactly when the column holds a value.
const CONSOLIDATION_NO_COST = "the provider reported no cost";
const CONSOLIDATION_NO_USAGE = "no call finished, or the provider reported none";
const CONSOLIDATION_NOT_FAILED = "the batch did not fail";
const CONSOLIDATION_NOT_COUNTED = "counted from v7 on";
const CONSOLIDATION_REASON_NEEDED = "a recorded value needs no explanation";

/** Every null the design documents. A null with no entry still renders `unknown`, unexplained. */
export const NULL_REASONS: readonly NullReason[] = [
  { route: "/v1/memory/consolidation", path: ".batches[].diagnostics", reason: "no parsed reply was recorded for this batch" },
  { route: "/v1/memory/consolidation", path: ".batches[].diagnostics.rejected[].fact.path", reason: "no file or repository path supplied" },
  { route: "/v1/memory/consolidation", path: ".batches[].diagnostics.rejected[].fact.replaces", reason: "the candidate replaces no existing fact" },
  { route: "/v1/runs/{id}", path: ".usage", reason: OLD_TURN },
  { route: "/v1/memory/consolidation", path: ".batches[].cost", reason: CONSOLIDATION_NO_COST },
  { route: "/v1/memory/consolidation", path: ".batches[].cost_nanos", reason: CONSOLIDATION_NO_COST },
  { route: "/v1/memory/consolidation", path: ".batches[].failure", reason: CONSOLIDATION_NOT_FAILED },
  { route: "/v1/memory/consolidation", path: ".batches[].facts_skipped", reason: CONSOLIDATION_NOT_COUNTED },
  { route: "/v1/memory/consolidation", path: ".batches[].input_tokens", reason: CONSOLIDATION_NO_USAGE },
  { route: "/v1/memory/consolidation", path: ".batches[].output_tokens", reason: CONSOLIDATION_NO_USAGE },
  { route: "/v1/memory/consolidation", path: ".batches[].session_id_reason", reason: CONSOLIDATION_REASON_NEEDED },
  { route: "/v1/memory/consolidation", path: ".batches[].failure_reason", reason: CONSOLIDATION_REASON_NEEDED },
  { route: "/v1/memory/consolidation", path: ".batches[].facts_skipped_reason", reason: CONSOLIDATION_REASON_NEEDED },
  { route: "/v1/memory/consolidation", path: ".batches[].model_reason", reason: CONSOLIDATION_REASON_NEEDED },
  { route: "/v1/memory/consolidation", path: ".batches[].input_tokens_reason", reason: CONSOLIDATION_REASON_NEEDED },
  { route: "/v1/memory/consolidation", path: ".batches[].output_tokens_reason", reason: CONSOLIDATION_REASON_NEEDED },
  { route: "/v1/memory/consolidation", path: ".batches[].cost_reason", reason: CONSOLIDATION_REASON_NEEDED },
  { route: "/v1/memory/consolidation", path: ".batches[].cost_nanos_reason", reason: CONSOLIDATION_REASON_NEEDED },
  { route: "/v1/memory/consolidation", path: ".batches[].finished_at_reason", reason: CONSOLIDATION_REASON_NEEDED },
  { route: "/v1/runs/{id}", path: ".stop_reason", reason: OLD_BUILD },
  { route: "/v1/runs/{id}", path: ".duration_reason", reason: SPAN_RECORDED },
  { route: "/v1/runs", path: ".runs[].stop_reason", reason: OLD_BUILD },
  { route: "/v1/runs/{id}/trace", path: "[].retrieval", reason: NO_RETRIEVAL },
  { route: "/v1/eval/runs/{id}", path: ".score", reason: NO_EVIDENCE },
  { route: "/v1/eval/runs", path: ".runs[].score", reason: NO_EVIDENCE },
  {
    route: "/v1/eval/suites",
    path: ".suites[].latest_score",
    reason: NEVER_RUN,
  },
  {
    route: "/v1/eval/suites",
    path: ".suites[].latest_eval_run_id",
    reason: NEVER_RUN,
  },
  {
    route: "/v1/eval/suites",
    path: ".suites[].latest_started_at",
    reason: NEVER_RUN,
  },
  {
    route: "/v1/eval/suites",
    path: ".suites[].latest_label",
    reason: NEVER_RUN,
  },
  { route: "/v1/eval/compare", path: ".before.score", reason: NO_EVIDENCE },
  { route: "/v1/eval/compare", path: ".after.score", reason: NO_EVIDENCE },
  {
    route: "/v1/improve/cycles/{id}",
    path: ".score_before",
    reason: NO_DECISION,
  },
  {
    route: "/v1/improve/cycles/{id}",
    path: ".score_after",
    reason: NO_DECISION,
  },
  {
    route: "/v1/improve/cycles/{id}",
    path: ".regressions",
    reason: NO_DECISION,
  },
  {
    route: "/v1/improve/cycles/{id}",
    path: ".soft_drops",
    reason: NO_DECISION,
  },
  {
    route: "/v1/improve/cycles/{id}",
    path: ".pass_count_before",
    reason: NO_DECISION,
  },
  {
    route: "/v1/improve/cycles/{id}",
    path: ".pass_count_after",
    reason: NO_DECISION,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".handlers[].model",
    reason: NO_AGENT_MODEL,
  },
  { route: "/v1/runs/{id}", path: ".provider", reason: BEFORE_010 },
  { route: "/v1/runs/{id}", path: ".model", reason: BEFORE_010 },
  { route: "/v1/eval/runs/{id}", path: ".provider", reason: BEFORE_010 },
  { route: "/v1/eval/runs/{id}", path: ".model", reason: BEFORE_010 },
  { route: "/v1/eval/runs", path: ".runs[].provider", reason: BEFORE_010 },
  { route: "/v1/eval/runs", path: ".runs[].model", reason: BEFORE_010 },
  { route: "/v1/eval/compare", path: ".before.provider", reason: BEFORE_010 },
  { route: "/v1/eval/compare", path: ".before.model", reason: BEFORE_010 },
  { route: "/v1/eval/compare", path: ".after.provider", reason: BEFORE_010 },
  { route: "/v1/eval/compare", path: ".after.model", reason: BEFORE_010 },
  { route: "/v1/improve/cycles/{id}", path: ".provider", reason: BEFORE_010 },
  { route: "/v1/improve/cycles/{id}", path: ".model", reason: BEFORE_010 },
  { route: "/v1/spend", path: ".total", reason: NONE_PRICED },
  { route: "/v1/spend", path: ".prices_fetched_at", reason: NO_PRICES },
  { route: "/v1/spend", path: ".router_cost", reason: NO_ROUTER_COST },
  { route: "/v1/spend", path: ".agent_cost", reason: NO_AGENT_COST },
  { route: "/v1/spend", path: ".api_equivalent", reason: NO_API_EQUIVALENT },
  {
    route: "/v1/runs/turn_seconds",
    path: ".average_seconds",
    reason: NO_TIMESTAMPS,
  },
  {
    route: "/v1/providers",
    path: ".providers[].key_var",
    reason: NEEDS_NO_KEY,
  },
  {
    route: "/v1/providers",
    path: ".providers[].default_model",
    reason: NO_DEFAULT_MODEL,
  },
  {
    route: "/v1/prices",
    path: ".prices[].cached_input",
    reason: NO_CACHE_RATE,
  },
  { route: "/v1/agents", path: ".agents[].status", reason: NEVER_PROBED },
  {
    route: "/v1/agents",
    path: ".agents[].fingerprint_matches",
    reason: NEVER_PROBED,
  },
  {
    route: "/v1/agents",
    path: ".agents[].protocol_version",
    reason: NEVER_PROBED,
  },
  { route: "/v1/agents", path: ".agents[].tested_at", reason: NEVER_PROBED },
  { route: "/v1/agents", path: ".agents[].detail", reason: NEVER_PROBED },
  { route: "/v1/agents", path: ".agents[].billing", reason: NO_BILLING },
  { route: "/v1/agents", path: ".agents[].produces", reason: NO_PRODUCES },
  {
    route: "/v1/agents",
    path: ".agents[].jail",
    reason: "an acp entry runs under its own settings, with no jail",
  },
  {
    route: "/v1/agents",
    path: ".agents[].confined_reason",
    reason: "confined, so there is nothing to explain",
  },
  { route: "/v1/router", path: ".model", reason: NEVER_PROBED_ROUTER },
  { route: "/v1/router", path: ".status", reason: NEVER_PROBED_ROUTER },
  {
    route: "/v1/router",
    path: ".latency_seconds",
    reason: NEVER_PROBED_ROUTER,
  },
  { route: "/v1/router", path: ".detail", reason: NEVER_PROBED_ROUTER },
  { route: "/v1/router", path: ".probed_at", reason: NEVER_PROBED_ROUTER },
  { route: "/v1/router", path: ".cost", reason: NO_ROUTER_PROBE_COST },
  // Routing on or off (ticket 316, decision 105): the reason is null while routing is on.
  { route: "/v1/router", path: ".routing_reason", reason: ROUTING_IS_ON },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].route",
    reason: "no route was chosen, and route_reason says why",
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].route_reason",
    reason: "a route was chosen, so there is no reason",
  },
  {
    route: "/v1/sessions/{id}/decisions",
    path: ".decisions[].fallback_cause",
    reason: NOT_A_FALLBACK,
  },
  {
    route: "/v1/sessions/{id}/decisions",
    path: ".decisions[].stop_reason",
    reason: NOT_BELOW_FLOOR,
  },
  {
    route: "/v1/sessions/{id}/decisions",
    path: ".decisions[].options[].description",
    reason: NO_OPTION_DESCRIPTION,
  },
  {
    route: "/v1/spend",
    path: ".handlers[].billing",
    reason: NOT_AN_AGENT_HANDLER,
  },
  { route: "/v1/spend", path: ".handlers[].cost", reason: NO_HANDLER_COST },
  {
    route: "/v1/spend",
    path: ".handlers[].api_equivalent",
    reason: NO_HANDLER_API_EQUIVALENT,
  },
  {
    route: "/v1/spend",
    path: ".handlers[].reason",
    reason: HANDLER_NEEDS_NO_REASON,
  },
  { route: "/v1/sessions", path: ".sessions[].title", reason: NEVER_ROUTED },
  {
    route: "/v1/messages",
    path: ".messages[].task_id",
    reason: "not a task's message",
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].task_message",
    reason: "not a task's message",
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".task",
    reason: "not a task's message",
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".task.unattended",
    reason: "the task has not started, so it is neither attended nor unattended",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].entry",
    reason: "not a scheduled task: it has no entry",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].finished_at",
    reason: "the task has not finished",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].finished_at_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_agent",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_confidence",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_confidence_percent",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_description",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_floor",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_floor_percent",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_reason",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_route",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_tool_kind",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].paused_at",
    reason: "the task is not paused",
  },
  {
    route: "/v1/schedules",
    path: ".schedules[].last_run_at_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/schedules",
    path: ".schedules[].next_due_at_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/schedules",
    path: ".schedules[].where_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].handler",
    reason: "no message has run yet, or its last message named no handler",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].handler_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].outcome",
    reason: "no message has run yet",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].outcome_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pending_permission_id",
    reason: "no permission is pending for this task",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].schedule_id",
    reason: "not started by a schedule",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].schedule_id_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].session_id",
    reason: "the task has not started a session",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].session_id_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].started_at",
    reason: "the task has not started",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].started_at_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].started_by",
    reason: "the task has not started",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].started_by_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].stop_reason",
    reason: "the task has not stopped",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].stop_reason_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].unattended",
    reason:
      "the task has not started, so it is neither attended nor unattended",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].unattended_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".entry",
    reason: "not a scheduled task: it has no entry",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".finished_at_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_agent",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_confidence",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_confidence_percent",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_description",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_floor",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_floor_percent",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_reason",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_route",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_tool_kind",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".paused_at",
    reason: "the task is not paused",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".handler",
    reason: "no message has run yet, or its last message named no handler",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".handler_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".outcome",
    reason: "no message has run yet",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".outcome_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pending_permission_id",
    reason: "no permission is pending for this task",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".schedule_id",
    reason: "not started by a schedule",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".schedule_id_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".session_id_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".started_at_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".started_by_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".stop_reason_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".unattended_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].text_reason",
    reason: TEXT_RECORDED,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].reply.output_id",
    reason: REPLY_IS_A_TURN,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].reply",
    reason: "no reply was recorded for this message; its reply reason says why",
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].reply_reason",
    reason: REPLY_RECORDED,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].hops[].outputs[].name",
    reason: OUTPUT_NAMES_NO_FILE,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].hops[].outputs[].caption",
    reason: NO_OUTPUT_CAPTION,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].handlers[].billing",
    reason: NOT_AN_AGENT_HANDLER,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].handlers[].cost",
    reason: NO_HANDLER_COST,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].handlers[].api_equivalent",
    reason: NO_HANDLER_API_EQUIVALENT,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].handlers[].reason",
    reason: HANDLER_NEEDS_NO_REASON,
  },
  // The recent-activity row and the History row (ticket 232): a message or a session that never
  // routed has nothing to name; a routed one with no cost reports none.
  { route: "/v1/messages", path: ".messages[].cost", reason: NO_MESSAGE_COST },
  {
    route: "/v1/sessions",
    path: ".sessions[].last_handler",
    reason: NEVER_ROUTED,
  },
  {
    route: "/v1/sessions",
    path: ".sessions[].last_outcome",
    reason: NEVER_ROUTED,
  },
  {
    route: "/v1/sessions",
    path: ".sessions[].last_cost",
    reason: NO_MESSAGE_COST,
  },
  { route: "/v1/sessions", path: ".sessions[].last_at", reason: NEVER_ROUTED },
  // A hop's own result (ticket 232, migration 018): a success has no reason to give, and a hop
  // recorded before the result existed has neither.
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].hops[].reason",
    reason: HOP_DID_NOT_FAIL,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].hops[].result",
    reason: HOP_UNRECORDED,
  },
  // A hop's repository record (ticket 244, migration 021): null exactly when `repository_reason`
  // holds the reason, and the reverse.
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].hops[].repository",
    reason: NO_REPOSITORY_RECORD,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].hops[].repository_reason",
    reason: REPOSITORY_RECORDED,
  },
  // A message whose decision stopped uncertain names no handler (ticket 291's recorded stops).
  {
    route: "/v1/messages",
    path: ".messages[].handler",
    reason: "no handler answered this message: a hop stopped before any did",
  },
  {
    route: "/v1/sessions/{id}/decisions",
    path: ".decisions[].handler",
    reason: "no handler answered this decision: it stopped before any did",
  },
  // An uncertain stop's compared figure (ticket 291, decision 101).
  {
    route: "/v1/sessions/{id}/decisions",
    path: ".decisions[].compared",
    reason: ROUTER_ACTED,
  },
  {
    route: "/v1/sessions/{id}/decisions",
    path: ".decisions[].compared_reason",
    reason: COMPARED_RECORDED,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].hops[].compared",
    reason: ROUTER_ACTED,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].hops[].compared_reason",
    reason: COMPARED_RECORDED,
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_compared",
    reason: ROUTER_ACTED,
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_compared_reason",
    reason: COMPARED_RECORDED,
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_compared",
    reason: ROUTER_ACTED,
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_compared_reason",
    reason: COMPARED_RECORDED,
  },
  // A permission request (ticket 234, migration 019): each column is null for its own reason, and
  // its `_reason` sibling is null exactly when the column holds a value.
  {
    route: "/v1/permissions",
    path: ".permissions[].description",
    reason: PERMISSION_NO_DESCRIPTION,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].ceiling_at",
    reason: PERMISSION_NO_CEILING,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].answer",
    reason: PERMISSION_UNANSWERED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].rule",
    reason: PERMISSION_UNANSWERED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].answered_at",
    reason: PERMISSION_UNANSWERED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].answered_via",
    reason: PERMISSION_NO_PERSON,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].description_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].ceiling_at_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].answer_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].rule_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].answered_at_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].answered_via_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].message_seq",
    reason: PERMISSION_NO_EXCHANGE,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].hop",
    reason: PERMISSION_NO_EXCHANGE,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].task_id",
    reason: PERMISSION_NO_TASK,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].message_seq_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].hop_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].task_id_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].task_hash",
    reason: PERMISSION_OLD,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].task_hash_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].detail",
    reason: PERMISSION_NO_DETAIL,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].detail_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].warning",
    reason: PERMISSION_NO_WARNING,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].warning_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].waiter_pid",
    reason: PERMISSION_OLD,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].waiter_pid_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].waiter_started_at",
    reason: PERMISSION_OLD,
  },
  {
    route: "/v1/permissions",
    path: ".permissions[].waiter_started_at_reason",
    reason: PERMISSION_REASON_NEEDED,
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].text",
    reason: "no decision holds this message's text, and text_reason says why",
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].outcome",
    reason: "no decision was made for this message, so it has no outcome",
  },
  {
    route: "/v1/sessions/{id}/messages",
    path: ".messages[].handler",
    reason: "no handler answered this message: a hop stopped before any did",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_agent_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_confidence_percent_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_confidence_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_description_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_floor_percent_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_floor_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_reason_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_route_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pause_tool_kind_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].paused_at_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks",
    path: ".tasks[].pending_permission_id_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".finished_at",
    reason: "the task has not finished",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".stop_reason",
    reason: "the task has not stopped",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".unattended",
    reason: "the task has not started, so it is neither attended nor unattended",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_agent_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_confidence_percent_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_confidence_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_description_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_floor_percent_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_floor_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_reason_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_route_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pause_tool_kind_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".paused_at_reason",
    reason: "a value was recorded; nothing to explain",
  },
  {
    route: "/v1/tasks/{id}",
    path: ".pending_permission_id_reason",
    reason: "a value was recorded; nothing to explain",
  },
];

/**
 * Why `/v1/spend` served `total: null`: the two sentences `ux/design.md` gives it, one per cause.
 * An empty `prices` table is the one state that also serves `prices_fetched_at: null`, so the
 * choice is made on that leaf's kind and nothing is read out of its characters.
 */
export function spendTotalReason(pricesFetchedAt: Leaf): string {
  return pricesFetchedAt.kind === "null" ? NO_PRICES : NONE_PRICED;
}

/** `D-3`'s two sentences, one per state, for the routes whose 404 and empty mean different things. */
const ABSENT_TEXT: readonly { route: string; text: string }[] = [
  {
    route: "/v1/memory/episodes/{id}",
    text: "the events table never held this session id",
  },
];

const EMPTY_TEXT: readonly { route: string; text: string }[] = [
  {
    route: "/v1/memory/episodes/{id}",
    text: "the session exists and wrote no episode",
  },
];

/** A concrete route, without the query string a template never spells. */
function pathOf(route: string): string {
  const [before] = route.split("?");
  return before ?? route;
}

/** Does a route template describe this route? `{id}` stands for exactly one segment. */
function routeMatches(template: string, route: string): boolean {
  const wanted = template.split("/");
  const asked = pathOf(route).split("/");
  if (wanted.length !== asked.length) return false;
  return wanted.every(
    (segment, index) => segment === "{id}" || segment === asked[index],
  );
}

/** A leaf's jq path with its array indices flattened, so one entry covers every row of a list. */
function pathTemplate(path: string): string {
  return path.replaceAll(/\[\d+\]/g, "[]");
}

/** What each operator-terms reason above is exactly, for the drawer (`V6-10`: "the exact reason stays
 * in the drawer"). */
const EXACT_REASONS: ReadonlyMap<string, string> = new Map([
  [
    OLD_TURN,
    "a turn predates migration 006, so a partial sum would understate the run",
  ],
  [OLD_BUILD, "written by a build older than migration 007"],
  [BEFORE_010, "recorded before migration 010"],
]);

/** Why the gateway sent nothing here, or `undefined` where the design documents no reason. */
export function nullReasonFor(route: string, path: string): string | undefined {
  const template = pathTemplate(path);
  return NULL_REASONS.find(
    (entry) => entry.path === template && routeMatches(entry.route, route),
  )?.reason;
}

/** The exact reason behind a null's operator-terms sentence, where the sentence is a softened one. */
export function exactReasonFor(
  route: string,
  path: string,
): string | undefined {
  const reason = nullReasonFor(route, path);
  return reason === undefined ? undefined : EXACT_REASONS.get(reason);
}

/** What a 404 on this route means, where a 404 means more than "not found". */
export function absentTextFor(route: string): string | undefined {
  return ABSENT_TEXT.find((entry) => routeMatches(entry.route, route))?.text;
}

/** What zero rows on this route mean, where they mean something a panel's caller has not said. */
export function emptyTextFor(route: string): string | undefined {
  return EMPTY_TEXT.find((entry) => routeMatches(entry.route, route))?.text;
}
