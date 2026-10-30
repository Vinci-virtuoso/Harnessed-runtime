/**
 * `HUMAN_WORDS` (`D-7`, decision 68): one static table keyed on `field:word`, holding every word
 * the ledger can emit for the fields this table covers, in `ux/design.md`'s own words where it
 * gives one and in the same voice where the source can emit more than `design.md` lists (this
 * ticket's own "For the architecture node" note records the source citations). A word not in the
 * table renders verbatim in mono - the fallback is deliberate, never a missing row hidden as one.
 *
 * Keyed on the *dashboard's* field name, not always the wire's: the turn-level `stop_reason` (the
 * provider's vocabulary, `end_turn` / `tool_use` / `max_tokens` / `raised` / `stop_sequence` /
 * `content_filter` / `other`) and the router's own uncertain-stop reason (`below_floor` /
 * `needs_human`) ride the *same* wire key, `stop_reason`, in two different responses - `design.md`
 * already draws them as two rows ("`stop_reason`" and "stop reason (uncertain)"), so this table
 * keeps them as two keys, `stop_reason` and `decision_stop_reason`, and the component reading a
 * decision's stop reason asks the second. The same split covers delegation: an ACP entry's
 * `status` (`succeeded` / `failed` / `error` / `timed_out` / `version_mismatch`) and a `cli`
 * entry's failure-only `reason` (`timeout` / `wrote_files`, plus `quota_refused` once ticket 194
 * lands it) are two different wire keys, so they are two table keys, `delegation_status` and
 * `delegation_reason`.
 */

/** One row: the field a component reads, the word the ledger sent, and the words to show. */
export type HumanWordRow = { field: string; word: string; text: string };

export const HUMAN_WORDS: readonly HumanWordRow[] = [
  // stop_reason (providers/types.py::StopReason, plus cli/run_cmd.py's RAISED)
  { field: "stop_reason", word: "end_turn", text: "Finished" },
  { field: "stop_reason", word: "tool_use", text: "Stopped to use a tool" },
  { field: "stop_reason", word: "max_tokens", text: "Cut off at the token limit" },
  { field: "stop_reason", word: "raised", text: "Stopped by an error" },
  { field: "stop_reason", word: "stop_sequence", text: "Stopped at a stop sequence" },
  { field: "stop_reason", word: "content_filter", text: "Blocked by content filter" },
  { field: "stop_reason", word: "other", text: "Stopped" },

  // decision_stop_reason (router/route.py::StopReason - an uncertain outcome's own reason)
  { field: "decision_stop_reason", word: "below_floor", text: "Below its confidence floor" },
  { field: "decision_stop_reason", word: "needs_human", text: "Needs you" },

  // guardrail (loop/guardrails.py)
  { field: "guardrail", word: "no_more_tool_calls", text: "Done, no more tools needed" },
  { field: "guardrail", word: "iteration_limit", text: "Stopped at the step limit" },

  // outcome (router/route.py::Outcome)
  { field: "outcome", word: "routed", text: "Routed" },
  { field: "outcome", word: "uncertain", text: "Not sure, stopped" },
  { field: "outcome", word: "fallback", text: "Router fell back" },
  { field: "outcome", word: "failed", text: "Failed" },

  // delegation_status (acp/delegate.py's ACP-kind status)
  { field: "delegation_status", word: "succeeded", text: "Finished" },
  { field: "delegation_status", word: "failed", text: "Failed" },
  { field: "delegation_status", word: "error", text: "Failed" },
  { field: "delegation_status", word: "timed_out", text: "Timed out" },
  { field: "delegation_status", word: "version_mismatch", text: "Version mismatch" },
  { field: "delegation_status", word: "quota_refused", text: "Plan limit reached" },

  // delegation_reason (acp/cli_runner.py's cli-kind failure reason; quota_refused per design.md,
  // ahead of ticket 194)
  { field: "delegation_reason", word: "timeout", text: "Timed out" },
  { field: "delegation_reason", word: "wrote_files", text: "Wrote files instead of answering" },
  { field: "delegation_reason", word: "quota_refused", text: "Plan limit reached" },
  { field: "delegation_reason", word: "dirty_tree", text: "Working tree had uncommitted changes" },

  // hop_result (cli/routed.py::HOP_RESULTS - how a hop's own handler settled, migration 018)
  { field: "hop_result", word: "succeeded", text: "Finished" },
  { field: "hop_result", word: "failed", text: "Failed" },

  // hop_reason (cli/routed.py::HOP_REASONS - why a hop failed)
  { field: "hop_reason", word: "timeout", text: "Timed out" },
  { field: "hop_reason", word: "wrote_files", text: "Wrote files instead of answering" },
  { field: "hop_reason", word: "error", text: "Stopped by an error" },
  { field: "hop_reason", word: "quota_refused", text: "Plan limit reached" },
  { field: "hop_reason", word: "dirty_tree", text: "Working tree had uncommitted changes" },
  { field: "hop_reason", word: "version_mismatch", text: "Version mismatch" },
  { field: "hop_reason", word: "raised", text: "Stopped by an error" },

  // billing (acp/config.py::BILLINGS)
  { field: "billing", word: "oauth", text: "Subscription" },
  { field: "billing", word: "openrouter", text: "OpenRouter, metered" },

  // kind (acp/config.py::KINDS)
  { field: "kind", word: "acp", text: "Live agent" },
  { field: "kind", word: "cli", text: "Command-line skill" },

  // memory_kind (router/questions.py::MEMORY_KINDS)
  { field: "memory_kind", word: "semantic", text: "Facts" },
  { field: "memory_kind", word: "episodic", text: "Past sessions" },
  { field: "memory_kind", word: "procedural", text: "Procedures" },

  // key_kind (loop/runs.py::KeyKind)
  { field: "key_kind", word: "session", text: "Chat" },
  { field: "key_kind", word: "case_run", text: "Eval case" },

  // route (router/questions.py's two fixed route options; a registry entry's own name renders
  // verbatim, since the registry is operator-configured and this table cannot know its entries)
  { field: "route", word: "groq_chat", text: "Groq" },
  { field: "route", word: "groq_memory", text: "Groq with memory" },

  // route_option (UncertainStop's option list only: the two terminal route options as words in a
  // list of choices; RouteLine keeps drawing its own "then done" sentence, so these are not `route` rows)
  { field: "route_option", word: "needs_human", text: "Needs you" },
  { field: "route_option", word: "done", text: "Finished" },

  // route_memory_clause (RouteLine's own parenthetical, ticket 188; router/questions.py's
  // memory_kind options, in the lower-case form a mid-sentence clause reads as)
  { field: "route_memory_clause", word: "semantic", text: "facts" },
  { field: "route_memory_clause", word: "episodic", text: "past sessions" },
  { field: "route_memory_clause", word: "procedural", text: "procedures" },

  // permission_state, permission_rule, permission_answer, answered_via, tool_kind
  // (acp/pending.py::STATES, RULES, ANSWERS, ANSWERED_VIA; acp's ToolKind - ticket 234)
  { field: "permission_state", word: "pending", text: "Waiting for you" },
  { field: "permission_state", word: "approved", text: "You approved it" },
  { field: "permission_state", word: "denied", text: "You said no" },
  { field: "permission_state", word: "expired", text: "Nobody answered in time, so it was refused" },
  { field: "permission_state", word: "allowed", text: "Allowed by the agent's settings" },
  { field: "permission_state", word: "refused", text: "Refused by the agent's settings" },
  { field: "permission_rule", word: "operator", text: "You answered" },
  { field: "permission_rule", word: "ceiling", text: "Nobody answered in time" },
  { field: "permission_rule", word: "allowlist", text: "The agent's allow list" },
  { field: "permission_rule", word: "deny_list", text: "The agent's deny list" },
  { field: "permission_answer", word: "approve", text: "Approved" },
  { field: "permission_answer", word: "deny", text: "Denied" },
  { field: "answered_via", word: "console", text: "from the console" },
  { field: "answered_via", word: "cli", text: "from the terminal" },

  // task_state, task_stop_reason (tasks/store.py::STATES, STOP_REASONS - ticket 239)
  { field: "task_state", word: "queued", text: "Queued" },
  { field: "task_state", word: "running", text: "Running" },
  { field: "task_state", word: "needs_human", text: "Needs you" },
  { field: "task_state", word: "done", text: "Done" },
  { field: "task_state", word: "failed", text: "Failed" },
  { field: "task_state", word: "cancelled", text: "Cancelled" },
  { field: "task_stop_reason", word: "finished", text: "Finished" },
  { field: "task_stop_reason", word: "budget", text: "Stopped at its budget" },
  { field: "task_stop_reason", word: "ceiling", text: "Nobody answered in time" },
  { field: "task_stop_reason", word: "dirty_tree", text: "Working tree had uncommitted changes" },
  { field: "task_stop_reason", word: "orphaned", text: "Its process went away" },
  { field: "task_stop_reason", word: "raised", text: "Stopped by an error" },
  { field: "task_stop_reason", word: "cancelled", text: "Cancelled" },
  // task_pause_reason, task_started_by, budget_source (tasks/store.py - ticket 253)
  { field: "task_pause_reason", word: "permission", text: "Waiting for your answer to an agent's request" },
  { field: "task_pause_reason", word: "uncertain", text: "Paused: the router was not sure" },
  { field: "task_started_by", word: "hand", text: "By hand" },
  { field: "task_started_by", word: "schedule", text: "Schedule" },
  // task_started_by_phrase: the same two words inside a sentence ("started by a schedule", "Task started (a schedule)")
  { field: "task_started_by_phrase", word: "hand", text: "you, by hand" },
  { field: "task_started_by_phrase", word: "schedule", text: "a schedule" },
  { field: "budget_source", word: "default", text: "the default" },
  { field: "budget_source", word: "given", text: "the one you gave" },
  // schedule_where (tasks/installer.py::WHERES - ticket 319): the v7 backends a schedule install is recorded under
  { field: "schedule_where", word: "launchd", text: "launchd" },
  { field: "schedule_where", word: "systemd", text: "systemd user timer" },
  // reply_reason (loop/messages.py::REPLY_TASK_DONE - ticket 273): a served reason worded as a finish
  { field: "reply_reason", word: "the task was judged finished", text: "The task finished" },
  { field: "reply_reason", word: "the task is finished", text: "The task finished" },
  // consolidation_status, consolidation_failure (memory/consolidation.py::STATUSES, FAILURES - ticket 249)
  { field: "consolidation_status", word: "running", text: "Consolidating" },
  { field: "consolidation_status", word: "done", text: "Consolidated" },
  { field: "consolidation_status", word: "failed", text: "Failed" },
  { field: "consolidation_failure", word: "provider_error", text: "The model could not be reached" },
  { field: "consolidation_failure", word: "timeout", text: "The model took too long" },
  { field: "consolidation_failure", word: "unparseable", text: "The model's answer could not be read" },

  // compared (router/decisions.py::COMPARED_WORDS - the figure an uncertain stop compared, decision 101)
  { field: "compared", word: "done_probability", text: "the chance it was finished" },
  { field: "compared", word: "confidence", text: "the router's confidence" },
  // routing (router/decisions.py::ROUTING_WORDS - whether a message is routed, ticket 316, decision 105)
  { field: "routing", word: "on", text: "Routing is on" },
  { field: "routing", word: "off", text: "Routing is off" },
  { field: "tool_kind", word: "read", text: "Read a file" },
  { field: "tool_kind", word: "edit", text: "Edit a file" },
  { field: "tool_kind", word: "delete", text: "Delete a file" },
  { field: "tool_kind", word: "move", text: "Move a file" },
  { field: "tool_kind", word: "search", text: "Search" },
  { field: "tool_kind", word: "execute", text: "Run a command" },
  { field: "tool_kind", word: "think", text: "Think" },
  { field: "tool_kind", word: "fetch", text: "Fetch from the web" },
  { field: "tool_kind", word: "switch_mode", text: "Switch mode" },
  { field: "tool_kind", word: "other", text: "Something else" },

  // suite_kind (evals/suite.py::KINDS) and case_verdict (evals/suite.py::VERDICTS; a trait verdict
  // is one row, its `{trait}` filled by the component), ticket 235
  { field: "suite_kind", word: "work", text: "Work suite" },
  { field: "suite_kind", word: "smoke", text: "Smoke" },
  { field: "suite_kind", word: "holdout", text: "Holdout" },
  { field: "suite_kind", word: "routing", text: "Routing" },
  { field: "suite_kind", word: "memory", text: "Memory" },
  { field: "case_verdict", word: "accept", text: "Accept" },
  { field: "case_verdict", word: "reject", text: "Reject" },
  { field: "case_verdict", word: "pending", text: "Awaiting a verdict" },
  { field: "case_verdict", word: "traits", text: "Must contain \"{trait}\"" },

  // handler (loop/runs.py::HandlerRow.handler; the one chat provider this milestone ships)
  { field: "handler", word: "groq", text: "Groq" },

  // agent (acp/config.py's registered names, in the words the operator knows them by)
  { field: "agent", word: "claude-code", text: "Claude Code" },
];

/** The display words for one field, else `undefined` - a word this table does not know. */
export function humanWord(field: string, word: string): string | undefined {
  return HUMAN_WORDS.find((row) => row.field === field && row.word === word)?.text;
}
