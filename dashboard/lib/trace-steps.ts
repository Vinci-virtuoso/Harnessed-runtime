/**
 * FR-129: one plain sentence per event, on every page (decision 68, design.md's `TraceStep`
 * sentence table).
 *
 * `TRACE_STEPS` is keyed on `events/names.py::NAMES`'s own spelling. A name with a row renders
 * that row's sentence: a list of parts, each either plain text this table wrote or a jq path
 * `TraceStep.tsx` reads through `Value` (never a number computed here - `D-1`, decision 32, and
 * never JSX, since this module is one of the plain `.ts` files `TraceStep.tsx` renders from). A
 * name in `VERBATIM` renders as its own characters instead, in mono, because no one-line sentence
 * is honest for it (`provider.request.sent`, `provider.response.received` and
 * `memory.write.committed` are reserved names no publisher under `src/` has ever used - see this
 * ticket's "For the architecture node" note). A name in neither renders the same way, verbatim,
 * which is the fallback `tests/test_dashboard_vocabulary.py` proves fires.
 *
 * A payload field this table reads that a particular frame did not carry is read through
 * `optional`, never `payload.at` directly: `Wire.at` throws on a path the response never held
 * (MUST rule 21), and `run.finished` alone has four shapes across `run_cmd.py`'s call sites, not
 * all of which carry `iterations`.
 */
import { humanWord } from "./human-words";
import type { Leaf, Wire } from "./wire";

/** The nine glyphs `StepIcon` draws, one per trace-step kind (design.md Direction rule 8). */
export type StepKind =
  | "router"
  | "memory"
  | "chat_model"
  | "agent"
  | "agent_tool"
  | "finished"
  | "uncertain"
  | "fell_back"
  | "failed";

/** The four words a step's own status can be (design.md's `TraceTimeline` states column). */
export type StepStatus = "step" | "running" | "ok" | "failed";

/** One piece of a sentence: this table's own words, or a jq path to a figure `Value` renders. */
export type SentencePart = { text: string } | { figure: string };

export type TraceStepRow = {
  kind: (payload: Wire) => StepKind;
  status: (payload: Wire) => StepStatus;
  sentence: (payload: Wire) => readonly SentencePart[];
};

/** A payload field a frame may or may not carry, read without `Wire.at`'s throw. */
export function optional(payload: Wire, path: string): Leaf | undefined {
  try {
    return payload.at(path);
  } catch {
    return undefined;
  }
}

/** Reads as `true` only where the wire spelled `true`; a comparison of characters, never a parse. */
function isTrue(leaf: Leaf | undefined): boolean {
  return leaf?.text === "true";
}

const text = (value: string): SentencePart => ({ text: value });
const figure = (path: string): SentencePart => ({ figure: path });

const kindOf =
  (value: StepKind) =>
  (): StepKind =>
    value;
const statusOf =
  (value: StepStatus) =>
  (): StepStatus =>
    value;

/** `run.finished`'s sentence: "Finished in N step(s)" where an `iterations` field was sent. */
function runFinishedSentence(payload: Wire): readonly SentencePart[] {
  const iterations = optional(payload, ".iterations");
  if (iterations === undefined) return [text("Finished")];
  const word = iterations.text === "1" ? "step" : "steps";
  return [text("Finished in "), figure(".iterations"), text(` ${word}`)];
}

/** `acp.delegation.finished`'s sentence: "{agent} finished" or "{agent} failed[: {reason}]". */
function delegationFinishedSentence(payload: Wire): readonly SentencePart[] {
  const succeeded = optional(payload, ".status")?.text === "succeeded";
  const branch = optional(payload, ".branch");
  if (succeeded && branch !== undefined) {
    return [figure(".agent"), text(" finished - changed files on "), figure(".branch")];
  }
  if (succeeded) return [figure(".agent"), text(" finished")];
  const reason = optional(payload, ".reason");
  return reason === undefined
    ? [figure(".agent"), text(" failed")]
    : [figure(".agent"), text(" failed: "), figure(".reason")];
}

/** `eval.run.finished`'s sentence: "Finished {suite}: N of M passed", or bare "Finished" where a
 * frame carries fewer fields than `evals/runner.py`'s own publisher always sends (ticket 185's own
 * hand-written `IMPROVE_FRAMES` fixture is exactly such a frame, deliberately narrower). */
function evalRunFinishedSentence(payload: Wire): readonly SentencePart[] {
  const suite = optional(payload, ".suite");
  const passCount = optional(payload, ".pass_count");
  const caseCount = optional(payload, ".case_count");
  if (suite === undefined || passCount === undefined || caseCount === undefined) {
    return [text("Finished")];
  }
  return [
    text("Finished "),
    figure(".suite"),
    text(": "),
    figure(".pass_count"),
    text(" of "),
    figure(".case_count"),
    text(" passed"),
  ];
}


/** A table word in the lower case a mid-sentence clause reads as ("Run a command" -> "run a command"). */
function lowered(words: string): string {
  return `${words.charAt(0).toLowerCase()}${words.slice(1)}`;
}

/** The words for a served value in one `HUMAN_WORDS` field, or the figure itself where unknown. */
function wordsOr(payload: Wire, path: string, field: string): readonly SentencePart[] {
  const leaf = optional(payload, path);
  if (leaf === undefined) return [];
  const words = humanWord(field, leaf.text);
  return words === undefined ? [figure(path)] : [text(lowered(words))];
}

/** `V6-5`: the singular is chosen from the served string "1" (a lookup, not arithmetic). */
function noun(payload: Wire, path: string, singular: string): string {
  return optional(payload, path)?.text === "1" ? singular : `${singular}s`;
}

/** `acp.permission.request`'s sentence: "{agent} asks to {tool kind words}", then "waiting". */
function permissionRequestSentence(payload: Wire): readonly SentencePart[] {
  const kind = optional(payload, ".tool_kind") ?? optional(payload, ".tool");
  const path = optional(payload, ".tool_kind") === undefined ? ".tool" : ".tool_kind";
  const words = kind === undefined ? undefined : humanWord("tool_kind", kind.text);
  if (words !== undefined) return [figure(".agent"), text(` asks to ${lowered(words)}`)];
  return [figure(".agent"), text(" asks to use "), figure(path)];
}

/** `acp.permission.decision`'s sentence: who or what decided, in the table's words. */
function permissionDecisionSentence(payload: Wire): readonly SentencePart[] {
  const allowed = isTrue(optional(payload, ".allowed"));
  const rule = optional(payload, ".rule")?.text;
  if (rule === "operator") return [text(allowed ? "Approved by you" : "Denied by you")];
  if (rule === "ceiling") return [text("Denied: no answer in time")];
  if (rule === "allowlist") return [text("Allowed by its settings")];
  if (rule === "deny_list") return [text("Refused by its settings")];
  return [
    figure(".agent"),
    text(allowed ? " allowed to use " : " not allowed to use "),
    figure(".tool"),
  ];
}

/** `task.paused`'s sentence: "Paused: {pause reason words}" (the table's own "Paused: " is not said twice). */
function taskPausedSentence(payload: Wire): readonly SentencePart[] {
  const reason = optional(payload, ".pause_reason");
  const words = reason === undefined ? undefined : humanWord("task_pause_reason", reason.text);
  if (words === undefined) return [text("Paused: "), ...wordsOr(payload, ".pause_reason", "task_pause_reason")];
  return [text(`Paused: ${lowered(words.replace(/^Paused: /, ""))}`)];
}

/** `task.finished`'s sentence: "Task done", "Task failed: {stop reason words}". */
function taskFinishedSentence(payload: Wire): readonly SentencePart[] {
  const state = optional(payload, ".state")?.text;
  if (state === "done") return [text("Task done")];
  if (state !== "failed") return [text("Task "), figure(".state")];
  const reason = wordsOr(payload, ".stop_reason", "task_stop_reason");
  return reason.length === 0 ? [text("Task failed")] : [text("Task failed: "), ...reason];
}

/** What follows "Memory consolidated" where the frame carries its counts: ": 4 facts from 20 episodes". */
function consolidationCounts(payload: Wire): readonly SentencePart[] {
  if (optional(payload, ".facts_written") === undefined || optional(payload, ".episode_count") === undefined) {
    return [];
  }
  return [
    text(": "),
    figure(".facts_written"),
    text(` ${noun(payload, ".facts_written", "fact")} from `),
    figure(".episode_count"),
    text(` ${noun(payload, ".episode_count", "episode")}`),
  ];
}

/** A batch that failed wrote nothing, and its row says so instead of "consolidated". */
function consolidationFailed(payload: Wire): boolean {
  return optional(payload, ".status")?.text === "failed";
}

export const TRACE_STEPS: Readonly<Record<string, TraceStepRow>> = {
  "run.started": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: () => [text("Run started")],
  },
  "run.finished": {
    kind: (payload) => (optional(payload, ".stop_reason")?.text === "raised" ? "failed" : "finished"),
    status: (payload) => (optional(payload, ".stop_reason")?.text === "raised" ? "failed" : "ok"),
    sentence: runFinishedSentence,
  },
  "loop.iteration.started": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: () => [text("Step "), figure(".iteration"), text(" started")],
  },
  "loop.iteration.finished": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: () => [text("Step "), figure(".iteration"), text(" finished")],
  },
  "loop.guardrail.tripped": {
    kind: kindOf("fell_back"),
    status: statusOf("ok"),
    sentence: () => [figure(".guardrail")],
  },
  "loop.route.decided": {
    kind: kindOf("router"),
    status: statusOf("step"),
    sentence: () => [
      text("Jev sent this to "),
      figure(".handler"),
      text(" ("),
      figure(".memory_kind"),
      text(") - "),
      figure(".confidence_percent"),
      text(" sure"),
    ],
  },
  "tool.call.started": {
    kind: kindOf("agent_tool"),
    status: statusOf("step"),
    sentence: () => [text("Used "), figure(".name")],
  },
  "tool.call.finished": {
    kind: kindOf("agent_tool"),
    status: statusOf("ok"),
    sentence: () => [text("Finished using "), figure(".name")],
  },
  "tool.call.failed": {
    kind: kindOf("failed"),
    status: statusOf("failed"),
    sentence: () => [figure(".name"), text(" failed: "), figure(".error")],
  },
  "retrieval.gate.decided": {
    kind: kindOf("memory"),
    status: statusOf("ok"),
    sentence: (payload) => [
      text(`Checked memory: ${isTrue(optional(payload, ".retrieve")) ? "needed" : "not needed"}`),
    ],
  },
  "retrieval.gate.failed_open": {
    kind: kindOf("fell_back"),
    status: statusOf("ok"),
    sentence: () => [text("Memory check failed, looked anyway")],
  },
  "retrieval.assembled": {
    kind: kindOf("memory"),
    status: statusOf("ok"),
    sentence: () => [text("Found "), figure(".kept"), text(" memories ("), figure(".tokens"), text(" tokens)")],
  },
  "retrieval.budget.truncated": {
    kind: kindOf("fell_back"),
    status: statusOf("ok"),
    sentence: () => [
      text("Memory kept "),
      figure(".kept"),
      text(", dropped "),
      figure(".dropped"),
      text(" over budget"),
    ],
  },
  "eval.run.started": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: () => [text("Started suite "), figure(".suite")],
  },
  "eval.case.started": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: () => [text("Started case "), figure(".case_id")],
  },
  "eval.case.scored": {
    kind: (payload) => (isTrue(optional(payload, ".passed")) ? "finished" : "failed"),
    status: (payload) => (isTrue(optional(payload, ".passed")) ? "ok" : "failed"),
    sentence: (payload) => [
      text("Case "),
      figure(".case_id"),
      text(isTrue(optional(payload, ".passed")) ? " passed" : " failed"),
    ],
  },
  "eval.run.finished": {
    kind: kindOf("finished"),
    status: statusOf("ok"),
    sentence: evalRunFinishedSentence,
  },
  "improve.run.started": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: () => [text("Started an improvement cycle")],
  },
  "improve.step.recorded": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: () => [text("Recorded step "), figure(".step")],
  },
  "improve.reflection.written": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: () => [text("Reflected on the failing cases")],
  },
  "improve.candidate.created": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: () => [text("Wrote a new candidate")],
  },
  "improve.promotion.decided": {
    kind: (payload) => (isTrue(optional(payload, ".promoted")) ? "finished" : "uncertain"),
    status: (payload) => (isTrue(optional(payload, ".promoted")) ? "ok" : "step"),
    sentence: (payload) =>
      isTrue(optional(payload, ".promoted"))
        ? [text("Promoted the candidate")]
        : [text("Did not promote: "), figure(".rule")],
  },
  "improve.run.finished": {
    kind: (payload) => (isTrue(optional(payload, ".promoted")) ? "finished" : "uncertain"),
    status: (payload) => (isTrue(optional(payload, ".promoted")) ? "ok" : "step"),
    sentence: (payload) => [
      text(isTrue(optional(payload, ".promoted")) ? "Cycle finished, promoted" : "Cycle finished, no promotion"),
    ],
  },
  "acp.delegation.started": {
    kind: kindOf("agent"),
    status: statusOf("step"),
    sentence: () => [figure(".agent"), text(" started")],
  },
  "acp.permission.request": {
    kind: kindOf("agent"),
    status: statusOf("running"),
    sentence: permissionRequestSentence,
  },
  "acp.permission.decision": {
    kind: kindOf("agent"),
    status: statusOf("ok"),
    sentence: permissionDecisionSentence,
  },
  "acp.delegation.finished": {
    kind: (payload) => (optional(payload, ".status")?.text === "succeeded" ? "finished" : "failed"),
    status: (payload) => (optional(payload, ".status")?.text === "succeeded" ? "ok" : "failed"),
    sentence: delegationFinishedSentence,
  },
  "acp.delegation.cached": {
    kind: kindOf("agent"),
    status: statusOf("ok"),
    sentence: () => [text("Reused a cached delegation ("), figure(".status"), text(")")],
  },
  "acp.tool.called": {
    kind: kindOf("agent_tool"),
    status: statusOf("step"),
    sentence: () => [figure(".agent"), text(": "), figure(".title")],
  },
  "acp.tool.finished": {
    kind: kindOf("agent_tool"),
    status: statusOf("ok"),
    sentence: () => [figure(".agent"), text(": "), figure(".title"), text(" finished")],
  },
  "task.started": {
    kind: kindOf("chat_model"),
    status: statusOf("step"),
    sentence: (payload) => {
      const by = wordsOr(payload, ".started_by", "task_started_by_phrase");
      return by.length === 0 ? [text("Task started")] : [text("Task started ("), ...by, text(")")];
    },
  },
  "task.paused": {
    kind: kindOf("router"),
    status: statusOf("step"),
    sentence: taskPausedSentence,
  },
  "task.finished": {
    kind: (payload) => (optional(payload, ".state")?.text === "failed" ? "failed" : "finished"),
    status: (payload) => (optional(payload, ".state")?.text === "failed" ? "failed" : "ok"),
    sentence: taskFinishedSentence,
  },
  "memory.consolidation.finished": {
    kind: kindOf("memory"),
    status: (payload) => (consolidationFailed(payload) ? "failed" : "ok"),
    sentence: (payload) =>
      consolidationFailed(payload)
        ? [text("Memory consolidation failed: wrote nothing")]
        : [text("Memory consolidated"), ...consolidationCounts(payload)],
  },
  "error.raised": {
    kind: kindOf("failed"),
    status: statusOf("failed"),
    sentence: () => [text("Error: "), figure(".class")],
  },
  "observer.failed": {
    kind: kindOf("failed"),
    status: statusOf("failed"),
    sentence: () => [text("An observer failed: "), figure(".observer")],
  },
};

/**
 * The names this table deliberately gives no sentence: three reserved names no publisher has ever
 * used (this ticket's "For the architecture node" note), so a one-line sentence for them would be
 * an invented behaviour rather than a described one.
 */
export const VERBATIM: readonly string[] = [
  "provider.request.sent",
  "provider.response.received",
  "memory.write.committed",
];

/** The row for a name, if this table has one. */
export function traceStepFor(name: string): TraceStepRow | undefined {
  return TRACE_STEPS[name];
}
